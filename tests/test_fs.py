"""Tests for shared filesystem helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from advisor._fs import normalize_path, read_text_capped, safe_rglob_paths, validate_file_types


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


class TestReadTextCapped:
    """``read_text_capped`` eliminates the stat-then-read TOCTOU window
    and caps memory before any parsing runs. The cap is measured in
    bytes (not decoded characters) so multi-byte content can't sneak
    past as a smaller character count."""

    def test_under_cap_returns_full_content(self, tmp_path: Path) -> None:
        target = tmp_path / "ok.txt"
        target.write_text("hello world", encoding="utf-8")
        assert read_text_capped(target, max_bytes=100) == "hello world"

    def test_strips_utf8_sig_bom(self, tmp_path: Path) -> None:
        """Default encoding is utf-8-sig — must strip a leading BOM so
        callers comparing the first line against a literal header don't
        get tripped up by the Windows-editor-emitted BOM byte."""
        target = tmp_path / "bom.txt"
        target.write_bytes(b'\xef\xbb\xbf{"schema": 1}')
        assert read_text_capped(target, max_bytes=100) == '{"schema": 1}'

    def test_exactly_at_cap_is_accepted(self, tmp_path: Path) -> None:
        """A file whose byte length equals the cap must still be
        returned — the boundary is inclusive on the safe side. Catches
        an off-by-one where ``>=`` swaps in for ``>``."""
        target = tmp_path / "edge.txt"
        target.write_bytes(b"a" * 100)
        assert read_text_capped(target, max_bytes=100) == "a" * 100

    def test_one_byte_over_cap_raises(self, tmp_path: Path) -> None:
        target = tmp_path / "big.txt"
        target.write_bytes(b"a" * 101)
        with pytest.raises(ValueError, match="exceeds 100 bytes"):
            read_text_capped(target, max_bytes=100)

    def test_cap_is_bytes_not_characters(self, tmp_path: Path) -> None:
        """100 characters of euro (``€``, 3 bytes in utf-8) = 300 bytes.
        A character-based cap would erroneously accept this; the byte
        cap correctly rejects it."""
        target = tmp_path / "multibyte.txt"
        target.write_text("€" * 100, encoding="utf-8")
        with pytest.raises(ValueError, match="exceeds 100 bytes"):
            read_text_capped(target, max_bytes=100)

    def test_missing_file_raises_file_not_found(self, tmp_path: Path) -> None:
        """Callers distinguish "no file" from "bad file" — re-raise the
        underlying FileNotFoundError so they keep that signal."""
        with pytest.raises(FileNotFoundError):
            read_text_capped(tmp_path / "absent.txt", max_bytes=100)


class TestValidateFileTypes:
    """``validate_file_types`` must distinguish path-traversal ``..`` segments
    from legitimate filenames that contain ``..`` as a substring."""

    def test_allows_double_dot_in_filename(self) -> None:
        """``foo..bar.py`` is a legal filename — ``..`` is a substring, not a
        path segment, so it must not be rejected."""
        validate_file_types("foo..bar.py")  # must not raise

    def test_rejects_parent_segment(self) -> None:
        """``../etc/passwd`` and ``a/../b`` contain ``..`` as a standalone
        path segment and must be rejected."""
        with pytest.raises(ValueError, match="unsafe"):
            validate_file_types("../etc/passwd")
        with pytest.raises(ValueError, match="unsafe"):
            validate_file_types("a/../b")
