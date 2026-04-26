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
        # The wrapper emits exactly one real <details>...</details> pair.
        # Any user-supplied <details>-shaped text is HTML-escaped (so it
        # renders as literal text, never as a tag) and must not contribute
        # additional opening/closing tags that GitHub would parse.
        assert out.lower().count("<details>") == 1
        assert out.lower().count("</details>") == 1
        assert "<details open>" not in out.lower()
        # The user-supplied tag-shaped text appears as escaped entities.
        assert "&lt;/DETAILS&gt;" in out
        assert "&lt;Details open&gt;" in out
        assert "&lt;DETAILS&gt;" in out

    def test_html_payloads_in_user_fields_are_escaped(self) -> None:
        """User-controlled finding fields land inside intentional HTML
        markup (``<details>``/``<summary>``/``<code>``/``<strong>``).
        Every one of those fields must be HTML-escaped so an attacker
        who controls runner output (or planted history JSONL) cannot
        inject script/img/iframe payloads into a posted PR comment.
        """
        finding = Finding(
            file_path="src/<x>.py:1",
            severity='<img src=x onerror="alert(1)">',
            description='<script>alert("desc")</script> & more',
            evidence="<plain evidence>",
            fix='<iframe src="evil"></iframe>',
            rule_id="advisor/<bad>/abc",
        )
        out = format_pr_comment([finding])
        # No raw injected tag from a user field should appear in the
        # rendered body (the wrapper's own <details>/<summary>/<code>
        # tags are fine — those aren't user-controlled).
        assert "<script>" not in out
        assert "<img src=x onerror" not in out
        assert "<iframe" not in out
        # Each payload appears as escaped entities instead.
        assert "&lt;script&gt;" in out
        assert "&lt;img src=x onerror=" in out
        assert "&lt;iframe" in out
        assert "&lt;x&gt;" in out  # from file_path
        assert "&lt;bad&gt;" in out  # from rule_id
        # Ampersand in description is escaped (not double-escaped).
        assert "&amp; more" in out

    def test_html_payloads_in_evidence_only_neutralize_fence_and_details(self) -> None:
        """Evidence lives inside a fenced code block; GitHub renders the
        content as literal text, so HTML-escape there would surface
        ``&lt;`` noise to readers. We neutralize two things: the closing
        fence (so the wrapping ``` block stays balanced) and any
        ``<details>``/``</details>`` tag-shaped string (defense in depth
        against a renderer that mishandles the fence and walks the inner
        text as HTML — a stray ``</details>`` would otherwise close the
        wrapping block early).
        """
        finding = Finding(
            file_path="src/x.py:1",
            severity="HIGH",
            description="d",
            evidence="<script>x</script>\n```end-of-fence\n</details>",
            fix="f",
        )
        out = format_pr_comment([finding])
        # General HTML inside the fence is preserved (not entity-escaped)
        # because Markdown renders it as text.
        assert "<script>x</script>" in out
        # Triple-backtick run is swapped to '''.
        assert "'''end-of-fence" in out
        # The </details> in the evidence is neutralized so it can't close
        # the outer <details> block.
        assert "</details>\n```" not in out
        assert "&lt;/details" in out

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
