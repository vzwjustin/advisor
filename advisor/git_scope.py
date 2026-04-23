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

import shutil
import subprocess
from pathlib import Path


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


def _run_git(cwd: Path, *args: str) -> list[str]:
    """Run ``git *args`` in ``cwd`` and return stdout lines (empty on empty output).

    Raises :class:`GitScopeError` on non-zero exit, missing binary, or
    timeout.
    """
    _require_git()
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            # Pin the decoder — ``text=True`` alone relies on
            # ``locale.getpreferredencoding()``, which can be ASCII/CP1252
            # on containers or Windows. A non-ASCII filename from git
            # would then raise ``UnicodeDecodeError`` outside the caught
            # exception set and crash the CLI. ``errors="replace"`` keeps
            # the command scoped-review-usable even on partial encoding
            # issues; display strings only need to be readable, not
            # round-trippable.
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitScopeError(
            f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SECONDS}s"
        ) from exc
    except OSError as exc:
        raise GitScopeError(f"failed to invoke git: {exc}") from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "(no stderr)"
        raise GitScopeError(f"git {' '.join(args)} failed: {stderr}")
    return [line for line in completed.stdout.splitlines() if line.strip()]


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
    for rel in rel_paths:
        p = repo_root / rel
        if p.is_file():
            out.append(str(p))
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
    for label, value in (("--since", since), ("--branch", branch)):
        if value and value.startswith("-"):
            raise GitScopeError(
                f"{label} ref {value!r} cannot begin with '-'; "
                f"git would parse it as an option, not a ref"
            )
    if since:
        return files_since(target, since)
    if staged:
        return files_staged(target)
    if branch:
        return files_branch(target, branch)
    return None
