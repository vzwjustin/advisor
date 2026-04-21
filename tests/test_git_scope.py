"""Tests for ``advisor.git_scope`` — git-incremental file scoping."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from advisor.git_scope import GitScopeError, resolve_git_scope


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A minimal git repo with two commits and one staged change."""
    _run(["git", "init", "-q", "-b", "main"], tmp_path)
    _run(["git", "config", "user.email", "t@t"], tmp_path)
    _run(["git", "config", "user.name", "t"], tmp_path)
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
