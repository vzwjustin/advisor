"""Tests for shared filesystem helpers."""

from __future__ import annotations

from pathlib import Path

from advisor._fs import safe_rglob_paths


def test_safe_rglob_paths_returns_deterministic_order(tmp_path: Path) -> None:
    (tmp_path / "b.py").write_text("")
    (tmp_path / "a.py").write_text("")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "c.py").write_text("")

    paths = safe_rglob_paths(tmp_path, "*.py")

    assert paths == sorted(paths)
