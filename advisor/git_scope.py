"""Git-incremental file scoping — limit a plan to changed files.

Supports three selection modes:

* ``--since REF``   : files changed between ``REF`` and ``HEAD`` (inclusive of
  working-tree changes). Equivalent to ``git diff --name-only REF``.
* ``--staged``       : files currently in the Git index but not yet committed
  (``git diff --name-only --cached``).
* ``--branch REF``   : files changed in the current branch relative to ``REF``
  (typically ``main`` or ``master``). Uses ``git diff --name-only REF...HEAD``
  so the scope matches what a PR would touch.

These modes are mutually exclusive; the CLI enforces that. A missing ``git``
binary, a non-Git directory, or a bad ref raises :class:`GitScopeError` —
callers render a friendly error and exit non-zero rather than scanning the
full tree silently.

Return values are always **absolute paths** filtered to files that still
exist on disk, so callers can feed them straight into ``rank_files``.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import BinaryIO


class GitScopeError(Exception):
    """Raised when a git-scoped selection cannot be resolved."""


def _require_git() -> None:
    if shutil.which("git") is None:
        raise GitScopeError("git is not on PATH; --since/--staged/--branch require a git checkout")


# Git commands occasionally hang — e.g. a ``diff`` against a ref that
# triggers credential-manager prompt on a misconfigured machine. Bound
# every invocation so the CLI cannot wedge indefinitely; 30 s is the
# tradeoff between "works on a large repo" and "bails quickly on a hang".
_GIT_TIMEOUT_SECONDS = 30

# Hard ceiling on git stdout bytes. ``git diff --name-only`` returns one
# path per line — typical: a few KB; pathological monorepo with 50k+
# files changed: tens of MB. 50 MiB matches advisor's other pipe-data
# ceilings (``__main__._STDIN_LIMIT``) and is well above any realistic
# repo's name-only diff while still bounding worst-case allocation if
# something runs away.
_GIT_MAX_STDOUT_BYTES = 50 * 1024 * 1024
_GIT_MAX_STDERR_BYTES = 1024 * 1024

# Characters legal in a git ref or revspec we want to support
# (branch/tag names, ``HEAD~N``, ``main^``, ``origin/foo``, reflog
# ``@{2.weeks.ago}`` with interior hyphens, ``@{-1}`` for previous branch).
# Anything outside this class is rejected at the public boundary so a value
# like ``main --output=/tmp/x`` or ``HEAD..main`` cannot smuggle option-like
# tokens into git's parser via concatenation in ``files_branch``.
_REF_ALLOWED = re.compile(r"^[A-Za-z0-9_./~^@{}\-]+$")


def _read_tempfile_capped(
    fh: BinaryIO, *, max_bytes: int, label: str, args: tuple[str, ...]
) -> str:
    """Read a subprocess temp file after enforcing a byte ceiling."""
    fh.flush()
    fh.seek(0, os.SEEK_END)
    size = fh.tell()
    if size > max_bytes:
        raise GitScopeError(
            f"git {' '.join(args)} produced more than {max_bytes // (1024 * 1024)} "
            f"MiB of {label} — refusing to load. Narrow the scope (e.g. a more "
            "recent ``--since`` ref) or run advisor against a smaller subtree."
        )
    fh.seek(0)
    return fh.read().decode("utf-8", errors="replace")


def _run_git(cwd: Path, *args: str) -> list[str]:
    """Run ``git *args`` in ``cwd`` and return stdout lines (empty on empty output).

    Raises :class:`GitScopeError` on non-zero exit, missing binary, or
    timeout.
    """
    _require_git()
    # ``start_new_session=True`` puts git (and any grandchildren it forks,
    # e.g. ``ssh`` or ``git-credential-manager``) into a fresh process
    # group. On ``TimeoutExpired`` we ``killpg`` the whole group so a
    # wedged credential prompt cannot outlive the CLI — closing the gap
    # between this module's "cannot wedge indefinitely" promise and what
    # ``subprocess.run`` alone delivers (it only signals the direct child).
    # POSIX-only; on Windows ``start_new_session`` is silently ignored and
    # ``os.killpg`` doesn't exist, so we skip the group kill there.
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            proc = subprocess.Popen(
                ["git", *args],
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                start_new_session=True,
            )
        except OSError as exc:
            raise GitScopeError(f"failed to invoke git: {exc}") from exc
        try:
            proc.communicate(timeout=_GIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            # Kill the whole process group so grandchildren (credential
            # managers, ssh, askpass helpers) die with git. ``killpg`` is
            # POSIX-only; on Windows fall back to plain ``proc.kill()``.
            if hasattr(os, "killpg"):
                # start_new_session=True makes the child its own session leader,
                # so PGID == proc.pid on POSIX. Use os.getpgid() explicitly so
                # the invariant is visible in the source rather than implicit.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            else:
                proc.kill()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            finally:
                # Ensure the process is reaped even if communicate() timed out
                # a second time. The process is already SIGKILL'd so wait()
                # returns immediately unless it is in uninterruptible D-state.
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            raise GitScopeError(
                f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SECONDS}s"
            ) from exc

        stderr = _read_tempfile_capped(
            stderr_file,
            max_bytes=_GIT_MAX_STDERR_BYTES,
            label="stderr",
            args=args,
        )
        if proc.returncode != 0:
            stderr_text = stderr.strip() or "(no stderr)"
            raise GitScopeError(f"git {' '.join(args)} failed: {stderr_text}")
        # Keep stdout out of Python heap until after the cap check. A
        # pathological diff can spill to the OS temp file, but it cannot
        # force advisor to allocate the whole payload before refusing it.
        stdout = _read_tempfile_capped(
            stdout_file,
            max_bytes=_GIT_MAX_STDOUT_BYTES,
            label="stdout",
            args=args,
        )
    return [line for line in stdout.splitlines() if line.strip()]


def _repo_root(cwd: Path) -> Path:
    """Return the top-level directory of the git repo containing ``cwd``."""
    lines = _run_git(cwd, "rev-parse", "--show-toplevel")
    if not lines:
        raise GitScopeError(f"{cwd} is not inside a git repository")
    return Path(lines[0])


def _resolve_files(repo_root: Path, rel_paths: list[str]) -> list[str]:
    """Convert repo-relative paths to absolute paths, keeping only existing files.

    ``git diff --name-only`` emits deleted files too (``git log`` semantics);
    we drop them because advisor cannot rank what is not on disk.
    """
    out: list[str] = []
    # Mirrors safe_rglob_paths' containment guard (_fs.py:121): git's
    # --name-only output is contractually repo-relative, but a defense-
    # in-depth check guards against malformed output (submodule edges,
    # hooks, hostile worktree configs) leaking paths outside repo_root.
    resolved_root = repo_root.resolve()
    for rel in rel_paths:
        p = repo_root / rel
        if not p.is_file():
            continue
        try:
            if p.resolve().is_relative_to(resolved_root):
                out.append(str(p))
        except OSError:
            continue
    return out


def files_since(target: Path, ref: str) -> list[str]:
    """Files changed between ``ref`` and the working tree.

    Covers committed changes after ``ref`` **and** unstaged/staged changes in
    the working copy — the full diff a reviewer would see.
    """
    repo = _repo_root(target)
    # ``--`` terminates the option list so any future value-from-input that
    # looked option-like (``--name-only``, ``-p``, etc.) would be rejected
    # by git as a path rather than parsed as a flag. The boundary check in
    # ``resolve_git_scope`` already filters leading-dash refs; this is
    # defense-in-depth for callers that bypass that boundary.
    lines = _run_git(repo, "diff", "--name-only", ref, "--")
    return _resolve_files(repo, lines)


def files_staged(target: Path) -> list[str]:
    """Files currently staged for commit (``git diff --cached``)."""
    repo = _repo_root(target)
    lines = _run_git(repo, "diff", "--name-only", "--cached", "--")
    return _resolve_files(repo, lines)


def files_branch(target: Path, base_ref: str) -> list[str]:
    """Files changed in the current branch relative to ``base_ref``.

    Uses ``git diff --name-only base...HEAD`` — the triple-dot form finds
    the merge base, so the diff reflects only changes introduced on the
    current branch (ignoring work done on ``base_ref`` since they diverged).
    This is what a GitHub PR UI shows.
    """
    repo = _repo_root(target)
    lines = _run_git(repo, "diff", "--name-only", f"{base_ref}...HEAD", "--")
    return _resolve_files(repo, lines)


def resolve_git_scope(
    target: Path,
    *,
    since: str | None = None,
    staged: bool = False,
    branch: str | None = None,
) -> list[str] | None:
    """Resolve the active git-scope selector to a list of file paths.

    Exactly one of ``since``/``staged``/``branch`` may be truthy. When all
    three are falsy, returns ``None`` — the caller should fall back to the
    normal full-tree scan.

    Raises :class:`GitScopeError` if git is unavailable, the directory is
    not a git repo, or the supplied ref cannot be resolved.
    """
    selectors = [bool(since), bool(staged), bool(branch)]
    if sum(selectors) > 1:
        raise GitScopeError("--since, --staged and --branch are mutually exclusive; pick one")
    # Reject refs that begin with ``-`` so they cannot be interpreted as
    # git options by ``git diff`` (e.g. ``-p`` would silently enable patch
    # mode; ``-h`` would hang on help output). Validation happens here at
    # the public boundary rather than inside ``_run_git`` so the error
    # message can name the specific selector the user passed.
    # Additionally enforce a character-class allowlist on the rest of the
    # ref: refs/revspecs in practice only need ``[A-Za-z0-9_./~^@{}-]``.
    # Anything else (whitespace, ``=``, ``--``, control chars, quotes,
    # shell-meta) is rejected so a value like ``main --output=/tmp/x`` or
    # ``HEAD..main`` cannot smuggle option-like tokens into git's parser
    # via concatenation in ``files_branch``. We do NOT shell out to
    # ``git check-ref-format`` because it rejects valid revspecs like
    # ``HEAD~1`` and ``main^`` that this module legitimately accepts.
    for label, value in (("--since", since), ("--branch", branch)):
        if not value:
            continue
        if value.startswith("-"):
            raise GitScopeError(
                f"{label} ref {value!r} cannot begin with '-'; "
                f"git would parse it as an option, not a ref"
            )
        if not _REF_ALLOWED.fullmatch(value):
            raise GitScopeError(
                f"{label} ref {value!r} contains characters outside "
                f"[A-Za-z0-9_./~^@{{}}-]; reject to prevent option-injection"
            )
        # Reject literal ``..`` / ``...`` in the user-supplied value:
        # ``files_branch`` concatenates ``f\"{base_ref}...HEAD\"``, so a
        # value containing ``..`` would produce a malformed multi-dot
        # revrange (``HEAD..main...HEAD``) that git parses unpredictably.
        # Legitimate refspecs do not contain ``..`` — only revranges do,
        # which the caller constructs, never the user.
        if ".." in value:
            raise GitScopeError(
                f"{label} ref {value!r} contains '..'; pass a single ref, not a revrange"
            )
    if since:
        return files_since(target, since)
    if staged:
        return files_staged(target)
    if branch:
        return files_branch(target, branch)
    return None
