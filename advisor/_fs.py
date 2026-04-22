"""Small filesystem helpers shared across the CLI and the optional web UI.

Kept deliberately minimal — this is an internal utility module, not a
kitchen-sink `utils.py`. If you're tempted to add a helper here, consider
whether it really has more than one caller first.
"""

from __future__ import annotations

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
        iterator = target.rglob(pattern)
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
