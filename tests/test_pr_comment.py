"""Tests for PR-comment markdown format (Phase 4d)."""

from __future__ import annotations

from advisor.pr_comment import _GITHUB_BODY_LIMIT, format_pr_comment
from advisor.verify import Finding


def _f(severity: str = "HIGH", description: str = "issue") -> Finding:
    return Finding(
        file_path="src/x.py:1",
        severity=severity,
        description=description,
        evidence="line 1",
        fix="use env",
    )


class TestFormatPrComment:
    def test_empty_findings(self) -> None:
        out = format_pr_comment([])
        assert "Advisor review" in out
        assert "No findings" in out

    def test_summary_table_counts(self) -> None:
        findings = [_f("CRITICAL"), _f("HIGH"), _f("HIGH"), _f("LOW")]
        out = format_pr_comment(findings)
        assert "4 findings" in out
        assert "| CRITICAL | 1 |" in out
        assert "| HIGH | 2 |" in out
        assert "| LOW | 1 |" in out
        assert "| MEDIUM | 0 |" in out

    def test_details_block_per_finding(self) -> None:
        findings = [_f(description="one"), _f(description="two")]
        out = format_pr_comment(findings)
        assert out.count("<details>") == 2

    def test_escapes_pipes_and_backticks(self) -> None:
        findings = [
            Finding(
                file_path="a`b.py",
                severity="HIGH",
                description="has | pipe and `backticks`",
                evidence="```code```",
                fix="",
            )
        ]
        out = format_pr_comment(findings)
        # No unescaped pipe in the details summary line should break the
        # preceding table.
        summary_line = next(line for line in out.splitlines() if "<details>" in line)
        # Pipes are escaped in table cells (for the title excerpt).
        assert "\\|" in summary_line or "|" not in summary_line.split("—", 1)[1]
        # Triple-backticks inside evidence should be neutralized.
        assert "'''code'''" in out

    def test_neutralizes_details_tags_case_insensitively(self) -> None:
        finding = Finding(
            file_path="src/x.py:1",
            severity="HIGH",
            description="</DETAILS><Details open>",
            evidence="</DeTaIlS>",
            fix="<DETAILS>",
        )
        out = format_pr_comment([finding])
        assert out.lower().count("<details>") == 1
        assert out.lower().count("</details>") == 1
        assert "<details open>" not in out.lower()

    def test_single_oversized_finding_is_truncated_under_body_limit(self) -> None:
        finding = Finding(
            file_path="src/x.py:1",
            severity="HIGH",
            description="large finding",
            evidence="x" * (_GITHUB_BODY_LIMIT * 2),
            fix="use env",
        )
        out = format_pr_comment([finding])
        assert len(out) < _GITHUB_BODY_LIMIT
        assert "Output truncated" in out
