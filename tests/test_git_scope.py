"""Tests for ``advisor.git_scope`` — git-incremental file scoping."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from advisor import git_scope
from advisor.__main__ import _resolve_plan_files
from advisor.git_scope import GitScopeError, resolve_git_scope


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A minimal git repo with two commits and one staged change."""
    _run(["git", "init", "-q", "-b", "main"], tmp_path)
    _run(["git", "config", "user.email", "t@t"], tmp_path)
    _run(["git", "config", "user.name", "t"], tmp_path)
    # Disable GPG/SSH commit signing locally — some CI environments have
    # ``commit.gpgsign = true`` set globally, which would make our test
    # commits fail with "signing failed". Tests must be self-contained
    # and never depend on the host's signing keys.
    _run(["git", "config", "commit.gpgsign", "false"], tmp_path)
    _run(["git", "config", "tag.gpgsign", "false"], tmp_path)
    (tmp_path / "a.py").write_text("a\n")
    (tmp_path / "b.py").write_text("b\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-q", "-m", "init"], tmp_path)
    (tmp_path / "a.py").write_text("a2\n")
    _run(["git", "add", "a.py"], tmp_path)
    _run(["git", "commit", "-q", "-m", "a2"], tmp_path)
    # stage an edit to b.py
    (tmp_path / "b.py").write_text("b2\n")
    _run(["git", "add", "b.py"], tmp_path)
    return tmp_path


class TestResolveGitScope:
    def test_since_returns_changed_files(self, git_repo: Path) -> None:
        paths = resolve_git_scope(git_repo, since="HEAD~1")
        assert paths is not None
        names = {Path(p).name for p in paths}
        assert "a.py" in names

    def test_staged_returns_staged_files(self, git_repo: Path) -> None:
        paths = resolve_git_scope(git_repo, staged=True)
        assert paths is not None
        names = {Path(p).name for p in paths}
        assert "b.py" in names

    def test_no_scope_returns_none(self, git_repo: Path) -> None:
        paths = resolve_git_scope(git_repo)
        assert paths is None

    def test_non_git_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(GitScopeError):
            resolve_git_scope(tmp_path, since="HEAD~1")

    def test_bad_ref_raises(self, git_repo: Path) -> None:
        with pytest.raises(GitScopeError):
            resolve_git_scope(git_repo, since="no-such-ref")

    def test_ref_with_injection_chars_raises(self, git_repo: Path) -> None:
        # Refs containing characters outside the allowlist (``=``, space,
        # ``--``) are rejected before git is invoked — protects against an
        # ``--output=/tmp/x`` smuggled via string concatenation in
        # ``files_branch``. Leading ``-`` is rejected by the older
        # dash-check branch; the rest are caught by the allowlist.
        with pytest.raises(GitScopeError):
            resolve_git_scope(git_repo, since="--config=user.name=hacked")
        with pytest.raises(GitScopeError, match="characters outside"):
            resolve_git_scope(git_repo, branch="main --output=/tmp/x")
        with pytest.raises(GitScopeError, match=r"contains '\.\.'"):
            resolve_git_scope(git_repo, since="HEAD..main")

    def test_valid_revspecs_pass_allowlist(self, git_repo: Path) -> None:
        # ``HEAD~1`` and ``main^`` contain ``~`` / ``^`` which must remain
        # legal — they're standard revspec syntax we need to support.
        # Just checking these don't raise on the allowlist; semantic
        # resolution is exercised by ``test_since_returns_changed_files``.
        resolve_git_scope(git_repo, since="HEAD~1")

    def test_stdout_cap_rejects_runaway_git_output(self, git_repo: Path, monkeypatch) -> None:
        monkeypatch.setattr(git_scope, "_GIT_MAX_STDOUT_BYTES", 1)
        with pytest.raises(GitScopeError, match="stdout"):
            git_scope._run_git(git_repo, "rev-parse", "--show-toplevel")


@pytest.fixture
def multi_subdir_repo(tmp_path: Path) -> Path:
    """Repo with changes in src/ and tools/ — exercises target intersection."""
    _run(["git", "init", "-q", "-b", "main"], tmp_path)
    _run(["git", "config", "user.email", "t@t"], tmp_path)
    _run(["git", "config", "user.name", "t"], tmp_path)
    _run(["git", "config", "commit.gpgsign", "false"], tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "tools").mkdir()
    (tmp_path / "src" / "a.py").write_text("a\n")
    (tmp_path / "tools" / "b.py").write_text("b\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-q", "-m", "init"], tmp_path)
    # Modify one file in each subdir so ``--since HEAD~1`` returns both.
    (tmp_path / "src" / "a.py").write_text("a2\n")
    (tmp_path / "tools" / "b.py").write_text("b2\n")
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-q", "-m", "edit"], tmp_path)
    return tmp_path


class TestRefAllowedRejectsTrailingNewline:
    def test_ref_allowed_rejects_trailing_newline(self, git_repo: Path) -> None:
        """B5: a ref value with a trailing newline must be rejected."""
        with pytest.raises(GitScopeError):
            resolve_git_scope(git_repo, since="main\n")


class TestResolvePlanFilesTargetIntersection:
    def test_git_scope_intersects_with_target_subdir(self, multi_subdir_repo: Path) -> None:
        # ``advisor plan src --since HEAD~1`` must return ONLY paths under
        # src/, not the tools/ change. Without intersection, git-scope
        # silently widens to the whole repo.
        target = multi_subdir_repo / "src"
        args = argparse.Namespace(
            since="HEAD~1",
            staged=False,
            branch=None,
            file_types="*.py",
        )
        files, err = _resolve_plan_files(target, args)
        assert err is None
        assert files is not None
        names = {Path(p).name for p in files}
        assert "a.py" in names
        assert "b.py" not in names
