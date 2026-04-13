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

    def test_tolerates_missing_fields(self):
        text = """
- **File**: src/app.py:5
- **Severity**: MEDIUM
"""
        findings = parse_findings_from_text(text)

        assert len(findings) == 1
        assert findings[0].description == ""
        assert findings[0].fix == ""

    def test_immutability(self):
        f = Finding("a.py", "HIGH", "d", "e", "f")
        try:
            f.severity = "LOW"  # type: ignore
            assert False, "Should have raised"
        except AttributeError:
            pass
