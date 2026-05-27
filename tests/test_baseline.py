"""Tests for baseline snapshot + diff (Phase 4b)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from advisor.baseline import (
    SCHEMA_VERSION,
    diff_against_baseline,
    filter_against_baseline,
    findings_to_entries,
    read_baseline,
    write_baseline,
)
from advisor.verify import Finding


def _f(path: str, description: str, severity: str = "HIGH") -> Finding:
    return Finding(
        file_path=path,
        severity=severity,
        description=description,
        evidence="",
        fix="",
    )


class TestRoundTrip:
    def test_write_read_preserves_entries(self, tmp_path: Path) -> None:
        findings = [_f("a.py:1", "issue a"), _f("b.py:2", "issue b")]
        entries = findings_to_entries(findings)
        p = tmp_path / "baseline.jsonl"
        write_baseline(p, entries)

        loaded = read_baseline(p)
        assert len(loaded) == len(entries)
        assert {e.file_path for e in loaded} == {e.file_path for e in entries}

    def test_header_has_schema_version(self, tmp_path: Path) -> None:
        p = tmp_path / "b.jsonl"
        write_baseline(p, findings_to_entries([_f("x.py", "y")]))
        text = p.read_text()
        first = json.loads(text.splitlines()[0])
        assert first["schema_version"] == SCHEMA_VERSION

    def test_non_utf8_file_warns_and_returns_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "bad-encoding.jsonl"
        p.write_bytes(b"\x80\x81\x82")
        with pytest.warns(UserWarning, match="could not read baseline"):
            loaded = read_baseline(p)
        assert loaded == []

    def test_oversized_baseline_warns_and_returns_empty(self, tmp_path: Path) -> None:
        """A baseline file larger than ``_MAX_BASELINE_BYTES`` must not be
        loaded — defends against unbounded memory growth if a hostile
        PR injects a giant ``.advisor/baseline.jsonl``. Behavior on
        oversize matches the pre-existing "unreadable → warn + empty"
        contract so a corrupt baseline doesn't break the run."""
        from advisor.baseline import _MAX_BASELINE_BYTES

        p = tmp_path / "huge.jsonl"
        p.write_bytes(b"a" * (_MAX_BASELINE_BYTES + 1))
        with pytest.warns(UserWarning, match="could not read baseline"):
            loaded = read_baseline(p)
        assert loaded == []

    def test_identity_path_collapses_dotdot(self) -> None:
        """A finding's identity path must collapse ``..`` so the
        baseline matcher and the suppressions matcher agree on what
        counts as "the same file". Pre-fix, baseline used a stripped
        spelling but did not collapse ``..``; suppressions (via
        ``_fs.normalize_path``) did. A finding written as
        ``src/../src/auth.py`` would baseline as that literal string
        and miss a suppression rule for ``src/auth.py``.
        """
        from advisor.baseline import _normalize_identity_path

        assert _normalize_identity_path("src/../src/auth.py") == "src/auth.py"
        assert _normalize_identity_path("./src/./auth.py") == "src/auth.py"
        # Line-suffix preservation — baseline keeps line identity even
        # though ``_fs.normalize_path`` strips it.
        assert _normalize_identity_path("src/../src/auth.py:42") == "src/auth.py:42"


class TestFilterAgainstBaseline:
    def test_matched_finding_is_suppressed(self, tmp_path: Path) -> None:
        findings = [_f("a.py", "issue a")]
        entries = findings_to_entries(findings)
        # A second call with the same findings should all be suppressed.
        new, dropped = filter_against_baseline(findings, entries)
        assert new == []
        assert len(dropped) == 1

    def test_new_finding_is_kept(self) -> None:
        baseline = findings_to_entries([_f("a.py", "old")])
        new_findings = [_f("a.py", "brand new issue")]
        new, dropped = filter_against_baseline(new_findings, baseline)
        assert len(new) == 1
        assert dropped == []

    def test_path_aliases_match_without_changing_line_identity(self) -> None:
        baseline = findings_to_entries([_f("src/auth.py:10", "issue")])
        current = [_f("./src\\auth.py:10", "issue")]
        new, dropped = filter_against_baseline(current, baseline)
        assert new == []
        assert dropped == current

        moved = [_f("src/auth.py:11", "issue")]
        new, dropped = filter_against_baseline(moved, baseline)
        assert new == moved
        assert dropped == []


class TestDiff:
    def test_partitions_are_disjoint_and_complete(self) -> None:
        baseline = findings_to_entries([_f("a.py", "old1"), _f("b.py", "old2")])
        current = [_f("a.py", "old1"), _f("c.py", "fresh")]
        diff = diff_against_baseline(current, baseline)
        # persisting (a.py), new (c.py), fixed (b.py)
        assert len(diff.persisting) == 1
        assert len(diff.new) == 1
        assert len(diff.fixed) == 1
        assert diff.new[0].file_path == "c.py"
        assert diff.fixed[0].file_path == "b.py"

    def test_empty_baseline_everything_is_new(self) -> None:
        findings = [_f("a.py", "x"), _f("b.py", "y")]
        diff = diff_against_baseline(findings, [])
        assert diff.new == findings
        assert diff.persisting == []
        assert diff.fixed == []

    def test_path_aliases_are_persisting(self) -> None:
        baseline = findings_to_entries([_f("src/auth.py:10", "old1")])
        current = [_f("./src\\auth.py:10", "old1")]
        diff = diff_against_baseline(current, baseline)
        assert diff.new == []
        assert diff.persisting == current
        assert diff.fixed == []


# ---------------------------------------------------------------------------
# Regression tests for Wave 3 — H2, H4, H5
# ---------------------------------------------------------------------------


def test_baseline_rule_id_handles_lone_surrogates() -> None:
    """H2 (baseline site): findings_to_entries / filter_against_baseline must
    not raise UnicodeEncodeError on descriptions containing lone surrogates."""
    finding = _f("src/auth.py", "issue with surrogate \ud800")
    entries = findings_to_entries([finding])
    assert len(entries) == 1
    # Round-trip: filter against the just-written baseline must not raise.
    new, suppressed = filter_against_baseline([finding], entries)
    assert new == []
    assert len(suppressed) == 1


def test_baseline_identity_aliases_absolute_and_relative() -> None:
    """H4: baseline written with relative paths must match findings with absolute
    paths (and vice-versa), so CI runs that emit absolute paths don't resurface
    every suppressed finding as 'new' on a local run using relative paths."""
    # Baseline written with relative path.
    baseline = findings_to_entries([_f("src/auth.py:10", "issue")])

    # Current finding uses an absolute path that ends with the relative path.
    abs_finding = _f("/home/runner/work/repo/src/auth.py:10", "issue")
    new, suppressed = filter_against_baseline([abs_finding], baseline)
    assert new == [], "absolute-path variant should be suppressed by relative-path baseline"
    assert len(suppressed) == 1

    # Reverse: baseline written with absolute path, current uses relative.
    baseline_abs = findings_to_entries([_f("/home/runner/work/repo/src/auth.py:10", "issue")])
    rel_finding = _f("src/auth.py:10", "issue")
    new2, suppressed2 = filter_against_baseline([rel_finding], baseline_abs)
    assert new2 == [], "relative-path variant should be suppressed by absolute-path baseline"
    assert len(suppressed2) == 1


def test_baseline_normalize_collapses_leading_double_slash() -> None:
    """H5: _normalize_identity_path must collapse leading '//' to '/' so
    network-mount or typo paths like '//foo.py' do not produce a distinct
    identity key from '/foo.py'.  POSIX normpath preserves '//' by design;
    the fix applies re.sub before normpath."""
    from advisor.baseline import _normalize_identity_path

    assert _normalize_identity_path("//src/auth.py") == "/src/auth.py"
    assert _normalize_identity_path("///src/auth.py") == "/src/auth.py"
    # Single slash is unaffected.
    assert _normalize_identity_path("/src/auth.py") == "/src/auth.py"
    # Findings with '//foo.py' and '/foo.py' now produce the same key.
    assert _normalize_identity_path("//foo.py") == _normalize_identity_path("/foo.py")

