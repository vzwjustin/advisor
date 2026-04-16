"""Tests for advisor.verify module."""

from advisor.verify import (
    Finding,
    format_findings_block,
    build_verify_prompt,
    parse_findings_from_text,
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
        findings = [
            Finding("src/a.py:1", "HIGH", "desc", "ev", "fix")
        ]
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
        try:
            f.severity = "LOW"  # type: ignore
            assert False, "Should have raised"
        except AttributeError:
            pass

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
