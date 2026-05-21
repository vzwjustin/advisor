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

# Characters legal in a git ref or revspec we want to support
# (branch/tag names, ``HEAD~N``, ``main^``, ``origin/foo``, reflog
# ``@{2.weeks.ago}``). Anything outside this class is rejected at the
# public boundary so a value like ``main --output=/tmp/x`` or ``HEAD..main``
# cannot smuggle option-like tokens into git's parser via concatenation
# in ``files_branch``.
_REF_ALLOWED = re.compile(r"^[A-Za-z0-9_./~^@{}\-]+$")


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
    try:
        proc = subprocess.Popen(
            ["git", *args],
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # See encoding/errors rationale below.
            encoding="utf-8",
            errors="replace",
            start_new_session=True,
        )
    except OSError as exc:
        raise GitScopeError(f"failed to invoke git: {exc}") from exc
    try:
        # ``text=True`` alone relies on ``locale.getpreferredencoding()``,
        # which can be ASCII/CP1252 on containers or Windows. A non-ASCII
        # filename from git would then raise ``UnicodeDecodeError`` outside
        # the caught exception set and crash the CLI. ``errors="replace"``
        # keeps the command scoped-review-usable even on partial encoding
        # issues; display strings only need to be readable, not
        # round-trippable.
        stdout, stderr = proc.communicate(timeout=_GIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        # Kill the whole process group so grandchildren (credential
        # managers, ssh, askpass helpers) die with git. ``killpg`` is
        # POSIX-only; on Windows fall back to plain ``proc.kill()``.
        if hasattr(os, "killpg"):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            proc.kill()
        # Drain pipes so the Popen object releases its file descriptors;
        # ignore output — we're erroring out anyway.
        try:
            proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        raise GitScopeError(
            f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SECONDS}s"
        ) from exc
    if proc.returncode != 0:
        stderr_text = stderr.strip() or "(no stderr)"
        raise GitScopeError(f"git {' '.join(args)} failed: {stderr_text}")
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
        if not _REF_ALLOWED.match(value):
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
