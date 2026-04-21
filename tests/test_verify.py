"""Tests for advisor.verify module."""

import pytest

from advisor.verify import (
    Finding,
    build_verify_prompt,
    format_findings_block,
    parse_findings_from_text,
    parse_findings_with_drift,
)


class TestFormatFindingsBlock:
    def test_formats_findings(self):
        findings = [
            Finding(
                file_path="src/auth.py:42",
                severity="CRITICAL",
                description="Hardcoded secret",
                evidence="API_KEY = 'abc123'",
                fix="Use environment variable",
            )
        ]
        block = format_findings_block(findings)

        assert "Finding 1" in block
        assert "src/auth.py:42" in block
        assert "CRITICAL" in block

    def test_empty_findings(self):
        block = format_findings_block([])
        assert "No findings" in block


class TestBuildVerifyPrompt:
    def test_contains_instructions(self):
        findings = [Finding("src/a.py:1", "HIGH", "desc", "ev", "fix")]
        prompt = build_verify_prompt(findings)

        assert "CONFIRMED" in prompt
        assert "REJECTED" in prompt
        assert "strict" in prompt.lower()


class TestParseFindingsFromText:
    def test_parses_structured_output(self):
        text = """
- **File**: src/auth.py:42
- **Severity**: CRITICAL
- **Description**: Hardcoded API key
- **Evidence**: Line 42: API_KEY = 'secret'
- **Fix**: Use os.environ

- **File**: src/db.py:10
- **Severity**: HIGH
- **Description**: SQL injection
- **Evidence**: f-string in query
- **Fix**: Use parameterized queries
"""
        findings = parse_findings_from_text(text)

        assert len(findings) == 2
        assert findings[0].file_path == "src/auth.py:42"
        assert findings[0].severity == "CRITICAL"
        assert findings[1].file_path == "src/db.py:10"

    def test_handles_empty_text(self):
        assert parse_findings_from_text("") == []

    def test_handles_no_issues_text(self):
        text = "No issues found in this file."
        assert parse_findings_from_text(text) == []

    def test_drops_blocks_missing_required_fields(self):
        # Incomplete blocks are dropped entirely rather than silently
        # promoted with default values — we do not fabricate Findings.
        text = """
- **File**: src/app.py:5
- **Severity**: MEDIUM
"""
        findings = parse_findings_from_text(text)
        assert findings == []

    def test_keeps_only_complete_blocks(self):
        text = """
- **File**: src/good.py:1
- **Severity**: HIGH
- **Description**: real issue
- **Evidence**: line 1
- **Fix**: patch it

- **File**: src/bad.py:2
- **Severity**: LOW
"""
        findings = parse_findings_from_text(text)
        assert len(findings) == 1
        assert findings[0].file_path == "src/good.py:1"

    def test_immutability(self):
        f = Finding("a.py", "HIGH", "d", "e", "f")
        with pytest.raises(AttributeError):
            f.severity = "LOW"  # type: ignore

    def test_parse_handles_field_label_in_body(self):
        """A description containing '**Fix**:' must not corrupt the parse."""
        text = """### Finding 1
- **File**: src/db.py:10
- **Severity**: HIGH
- **Description**: Use Fix: parameterized queries instead of string concat
- **Evidence**: f-string in query at line 10
- **Fix**: Use cursor.execute with placeholders
"""
        findings = parse_findings_from_text(text)
        assert len(findings) == 1
        f = findings[0]
        assert f.file_path == "src/db.py:10"
        assert "Fix: parameterized queries" in f.description
        assert f.fix == "Use cursor.execute with placeholders"

    def test_bold_fix_label_in_evidence_body_not_captured(self):
        """A bold **Fix**: label inside an Evidence body (without a list marker)
        must not steal the fix slot. Only a proper '- **Fix**:' list item opens
        the real fix field."""
        text = """### Finding 1
- **File**: foo.py:42
- **Severity**: HIGH
- **Description**: SQL injection
- **Evidence**: The bad pattern is
  **Fix**: something inside evidence narrative
  continues here
- **Fix**: Use parameterized queries.
"""
        findings = parse_findings_from_text(text)
        assert len(findings) == 1
        f = findings[0]
        assert f.file_path == "foo.py:42"
        assert "The bad pattern is" in f.evidence
        assert "something inside evidence narrative" in f.evidence
        assert f.fix == "Use parameterized queries."

    def test_parse_flushes_on_finding_header(self):
        """Two findings with out-of-order fields parse as separate blocks."""
        text = """### Finding 1
- **File**: src/auth.py:5
- **Severity**: CRITICAL
- **Description**: Hardcoded secret
- **Evidence**: line 5
- **Fix**: use env var

### Finding 2
- **Severity**: LOW
- **File**: src/util.py:3
- **Description**: Unused import
- **Evidence**: line 3
- **Fix**: remove import
"""
        findings = parse_findings_from_text(text)
        assert len(findings) == 2
        assert findings[0].file_path == "src/auth.py:5"
        assert findings[0].severity == "CRITICAL"
        assert findings[1].file_path == "src/util.py:3"
        assert findings[1].severity == "LOW"

    def test_parses_asterisk_bullets(self):
        """Agents that emit ``* **File**:`` bullets must parse identically
        to the ``-`` bullet form. Regression: previously these were
        silently dropped, yielding zero findings.
        """
        text = """### Finding 1
* **File**: src/auth.py:42
* **Severity**: CRITICAL
* **Description**: Hardcoded API key
* **Evidence**: API_KEY = 'secret'
* **Fix**: Use os.environ
"""
        findings = parse_findings_from_text(text)
        assert len(findings) == 1
        assert findings[0].file_path == "src/auth.py:42"
        assert findings[0].severity == "CRITICAL"

    def test_parses_mixed_bullet_styles(self):
        """A block mixing ``-`` and ``*`` bullets (rare but possible)
        still parses all fields.
        """
        text = """### Finding 1
- **File**: src/db.py:10
* **Severity**: HIGH
- **Description**: SQL injection
* **Evidence**: f-string in query
- **Fix**: Use parameterized queries
"""
        findings = parse_findings_from_text(text)
        assert len(findings) == 1
        assert findings[0].fix == "Use parameterized queries"


class TestRoundTrip:
    """``format_findings_block`` and ``parse_findings_from_text`` must be
    inverse operations for well-formed Finding lists.
    """

    def test_roundtrip_preserves_findings(self):
        originals = [
            Finding("src/a.py:10", "HIGH", "desc a", "evidence a", "fix a"),
            Finding("src/b.py:20", "MEDIUM", "desc b", "evidence b", "fix b"),
            Finding("src/c.py:30", "LOW", "desc c", "evidence c", "fix c"),
        ]
        parsed = parse_findings_from_text(format_findings_block(originals))
        assert len(parsed) == len(originals)
        for orig, p in zip(originals, parsed, strict=True):
            assert p.file_path == orig.file_path
            assert p.severity == orig.severity
            assert p.description == orig.description
            assert p.evidence == orig.evidence
            assert p.fix == orig.fix


class TestParserRobustness:
    """Parser must never crash on adversarial / malformed input; always a list."""

    def test_empty_input(self):
        assert parse_findings_from_text("") == []

    def test_garbage_input(self):
        assert parse_findings_from_text("random stuff that isn't a finding\n" * 20) == []

    def test_partial_block_is_dropped(self):
        text = "### Finding 1\n- **File**: only.py\n"
        assert parse_findings_from_text(text) == []

    def test_markdown_heading_in_body_survives(self):
        # Regression: a plain `# heading` line inside an Evidence body used
        # to be silently stripped, dropping the finding. It must now round-trip.
        text = (
            "### Finding 1\n"
            "- **File**: src/a.py\n"
            "- **Severity**: HIGH\n"
            "- **Description**: handles a # heading line\n"
            "- **Evidence**: code path\n"
            "- **Fix**: none\n"
        )
        findings = parse_findings_from_text(text)
        assert len(findings) == 1
        assert findings[0].file_path == "src/a.py"


# Hypothesis is an optional test dep; skip if unavailable.
hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st


# ``deadline=None`` disables per-example wall-clock timing (slow Windows and
# low-tier CI runners routinely miss the default 200 ms target, producing
# flakes that have nothing to do with the code). The ``too_slow`` health
# check is suppressed for the same reason; we still rely on ``max_examples``
# to bound total run time. ``derandomize=True`` makes reproductions stable
# across machines when a regression IS found.
@given(st.text(min_size=0, max_size=2000))
@settings(
    max_examples=200,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
def test_parse_findings_never_crashes(text):
    """Fuzz: arbitrary text input must never raise and must return a list."""
    result = parse_findings_from_text(text)
    assert isinstance(result, list)
    assert all(isinstance(f, Finding) for f in result)


class TestScopeDriftFilter:
    """`parse_findings_with_drift` — structural out-of-batch filter.

    A runner assigned to ``{auth.py, session.py}`` must not be able to
    land findings against ``crypto.py`` in the final report. The filter
    is applied at parse time so drift is dropped before any downstream
    consumer sees it.
    """

    def _text(self, *paths):
        blocks = []
        for i, p in enumerate(paths, 1):
            blocks.append(
                f"### Finding {i}\n"
                f"- **File**: {p}\n"
                f"- **Severity**: HIGH\n"
                f"- **Description**: d{i}\n"
                f"- **Evidence**: e{i}\n"
                f"- **Fix**: f{i}\n"
            )
        return "\n".join(blocks)

    def test_none_batch_is_identity(self):
        """batch_files=None (default) preserves every well-formed finding."""
        text = self._text("auth.py", "crypto.py", "session.py")
        kept, dropped = parse_findings_with_drift(text, None)
        assert len(kept) == 3
        assert dropped == []

    def test_in_batch_paths_kept(self):
        text = self._text("auth.py", "session.py")
        kept, dropped = parse_findings_with_drift(text, {"auth.py", "session.py"})
        assert [f.file_path for f in kept] == ["auth.py", "session.py"]
        assert dropped == []

    def test_out_of_batch_paths_dropped_and_surfaced(self):
        text = self._text("auth.py", "crypto.py", "session.py")
        kept, dropped = parse_findings_with_drift(text, {"auth.py", "session.py"})
        assert sorted(f.file_path for f in kept) == ["auth.py", "session.py"]
        assert [f.file_path for f in dropped] == ["crypto.py"]

    def test_empty_batch_drops_everything(self):
        """An empty set is distinct from None — drops every finding."""
        text = self._text("auth.py")
        kept, dropped = parse_findings_with_drift(text, set())
        assert kept == []
        assert len(dropped) == 1

    def test_path_normalization_strips_leading_dot_slash(self):
        """Findings with ``./auth.py`` match batch entry ``auth.py``."""
        text = self._text("./auth.py")
        kept, dropped = parse_findings_with_drift(text, {"auth.py"})
        assert len(kept) == 1
        assert dropped == []

    def test_path_normalization_handles_backslashes(self):
        """Windows-style paths normalize to POSIX for comparison."""
        text = self._text("dir\\file.py")
        kept, dropped = parse_findings_with_drift(text, {"dir/file.py"})
        assert len(kept) == 1
        assert dropped == []

    def test_parse_findings_from_text_accepts_batch_arg(self):
        """The public `parse_findings_from_text` entry point gains the parameter too."""
        text = self._text("auth.py", "crypto.py")
        kept = parse_findings_from_text(text, {"auth.py"})
        assert [f.file_path for f in kept] == ["auth.py"]

    def test_drop_warning_emitted_to_logger(self, caplog):
        import logging

        text = self._text("crypto.py")
        with caplog.at_level(logging.WARNING, logger="advisor.verify"):
            kept, dropped = parse_findings_with_drift(text, {"auth.py"})
        assert kept == []
        assert len(dropped) == 1
        assert any("scope-drift" in r.message for r in caplog.records)
