"""Tests for shared filesystem helpers."""

from __future__ import annotations

from pathlib import Path

from advisor._fs import normalize_path, safe_rglob_paths


def test_safe_rglob_paths_returns_deterministic_order(tmp_path: Path) -> None:
    (tmp_path / "b.py").write_text("")
    (tmp_path / "a.py").write_text("")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "c.py").write_text("")

    paths = safe_rglob_paths(tmp_path, "*.py")

    assert paths == sorted(paths)


class TestAtomicWriteLineEndings:
    """``_atomic_write`` must produce LF-only files on every platform.

    Without ``newline=""`` on the underlying file handle, Python's
    universal-newlines write translation turns each ``\\n`` into
    ``\\r\\n`` on Windows. JSONL parsers tolerate that, but
    ``msvcrt.locking`` on Windows uses byte offsets and CRLF
    expansion silently misaligns the lock region — concurrent
    appenders then collide.
    """

    def test_payload_with_lf_stays_lf(self, tmp_path: Path) -> None:
        from advisor._fs import atomic_write_text

        p = tmp_path / "out.jsonl"
        atomic_write_text(p, "line1\nline2\nline3\n")
        # Read raw bytes — text-mode read with universal newlines
        # would re-translate and hide the bug.
        raw = p.read_bytes()
        assert b"\r\n" not in raw, "atomic_write must not introduce CRLF"
        assert raw.count(b"\n") == 3


class TestNormalizePathLexicalCollapse:
    """``..`` and redundant ``.`` collapse — runners that anchor on
    cosmetically-different paths (e.g. ``src/../src/auth.py``) used to
    trip false-positive scope drift. Lexical normalization makes
    equivalent paths compare equal.
    """

    def test_dotdot_collapses(self) -> None:
        assert normalize_path("src/../src/auth.py") == "src/auth.py"

    def test_redundant_dot_collapses(self) -> None:
        assert normalize_path("src/./auth.py") == "src/auth.py"

    def test_double_slash_collapses(self) -> None:
        # posixpath.normpath squashes ``//`` to ``/``.
        assert normalize_path("src//auth.py") == "src/auth.py"

    def test_dotdot_and_line_suffix_combined(self) -> None:
        # Line-suffix strip happens before lexical collapse, so
        # ``src/../src/auth.py:42`` first drops the ``:42`` then
        # collapses ``..``.
        assert normalize_path("src/../src/auth.py:42") == "src/auth.py"

    def test_already_normalized_unchanged(self) -> None:
        assert normalize_path("src/auth.py") == "src/auth.py"

    def test_leading_dot_slash_preserves_normalization(self) -> None:
        # Leading ``./`` is stripped before normpath; the result is the
        # bare relative path with any internal ``..`` collapsed too.
        assert normalize_path("./src/../src/auth.py") == "src/auth.py"

    def test_empty_input_stays_empty(self) -> None:
        # An empty path must not become ``"."`` after normalization —
        # downstream uses string equality with other empty paths.
        assert normalize_path("") == ""
        assert normalize_path("   ") == ""
