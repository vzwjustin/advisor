"""Small filesystem helpers shared across the CLI and the optional web UI.

Kept deliberately minimal — this is an internal utility module, not a
kitchen-sink `utils.py`. If you're tempted to add a helper here, consider
whether it really has more than one caller first.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .rank import CONTENT_SCAN_LIMIT


def read_head(path: str, limit: int = CONTENT_SCAN_LIMIT) -> str:
    """Return the first ``limit`` characters of ``path`` or ``""`` on any OS error.

    Streams from disk rather than ``Path.read_text()[:limit]`` so large files
    aren't fully loaded into memory just to be sliced. Decoding errors are
    ignored — the result is a best-effort peek used for keyword scanning,
    not round-trippable content.
    """
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            return f.read(limit)
    except OSError:
        return ""


def safe_rglob_paths(target: Path, pattern: str) -> list[str]:
    """Recursively list files under ``target`` matching ``pattern``.

    Returns an empty list on a malformed pattern or filesystem error.
    Callers that need to surface the error message to the user should call
    ``target.rglob`` directly and handle the exception themselves — this
    helper is for the "best-effort listing" case.
    """
    try:
        resolved_target = target.resolve()
        iterator = resolved_target.rglob(pattern)
    except (OSError, ValueError):
        return []

    results: list[str] = []
    # Resolve each entry in its own try so a single ELOOP / permission /
    # stale-symlink error doesn't wipe the whole listing.
    for p in iterator:
        try:
            if p.is_file() and p.resolve().is_relative_to(resolved_target):
                results.append(str(p))
        except (OSError, ValueError):
            continue
    return results


def atomic_write_text(
    target: Path,
    text: str,
    *,
    reject_symlink: bool = False,
    mode: int | None = None,
) -> None:
    """Write ``text`` to ``target`` atomically via a same-dir tmpfile + rename.

    A SIGINT / disk-full mid-write leaves the original file untouched —
    ``os.replace`` is atomic on POSIX and Windows when source and target
    sit on the same filesystem. We write to a tmpfile in the target's
    parent directory to guarantee that property, fsync the file + parent
    directory, and clean up the tmp on any error.

    Hardening knobs:

    * ``reject_symlink`` (default ``False``) — raise :class:`OSError` if
      the target path is a symlink. Callers writing under shared
      directories (``~/.claude``) use this to refuse TOCTOU attacks
      where an attacker swaps a benign path for a symlink to a sensitive
      file mid-write. Most advisor callers write under the user's own
      target directory and do not enable this.
    * ``mode`` (default ``None``) — ``chmod`` the tmpfile to this mode
      before the rename so the final file lands with it.
      :func:`tempfile.mkstemp` defaults to ``0o600`` which would
      otherwise carry through; pass ``0o644`` to match the conventional
      readable-by-tools default. A chmod failure (Windows, FAT, some
      container mounts) is non-fatal — the write still succeeds, just
      with the tempfile's default mode.
    """
    if reject_symlink and target.is_symlink():
        raise OSError(f"refusing to write through symlink: {target} -> {os.readlink(target)}")

    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(parent))
    tmp = Path(tmp_name)
    try:
        fh = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        with fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            try:
                os.chmod(tmp, mode)
            except OSError:
                # Best-effort: Windows / restricted fs may refuse. The
                # write still succeeds; mode just stays at the tempfile
                # default.
                pass
        os.replace(tmp, target)
        # Best-effort directory fsync so the rename survives an abrupt
        # power loss. Windows refuses to open a directory for fsync —
        # silently skip there.
        try:
            dir_fd = os.open(str(parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def normalize_path(path: str) -> str:
    """Normalize a file path for batch/drift-detection comparison.

    Strips surrounding whitespace, backticks, leading ``./``, converts
    backslashes to forward slashes, and strips a trailing ``:line`` or
    ``:line:col`` suffix. Findings conventionally encode the offending
    location as ``src/auth.py:42``; batch membership keys and scope
    anchors use filenames only, so without the suffix strip every
    finding would look like scope drift. Does NOT resolve symlinks or
    make the path absolute — callers operate on repo-relative POSIX
    paths as emitted by the explore phase and echoed back in findings.

    The line-suffix strip is **capped at 2 iterations** so a pathological
    input like ``file:42:43:44:45`` does not strip past the line/col
    pair into the actual path segments, and so Windows drive letters
    (``C:\\Users\\...`` → ``C:/Users/...``) are never treated as a
    line-number tail (``C`` is not all digits).
    """
    p = path.strip().strip("`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    for _ in range(2):
        if ":" not in p:
            break
        head, _sep, tail = p.rpartition(":")
        if tail.isdigit() and head:
            p = head
        else:
            break
    return p
