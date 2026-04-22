"""Format findings as a GitHub-flavored markdown PR comment.

Emits a collapsible ``<details>`` block per finding plus a summary table
keyed by severity. Safe to paste into a PR body — backticks in
descriptions are escaped so the markdown renderer doesn't choke, and
pipe characters are HTML-escaped so they don't break inline tables.

Pure string-in, string-out. No I/O.
"""

from __future__ import annotations

from .verify import Finding

_SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW")


def _escape_table_cell(text: str) -> str:
    """Escape characters that break GFM table rendering."""
    # Pipes split columns; backticks inside backticks need careful handling.
    return text.replace("|", "\\|").replace("\n", " ")


def _escape_inline(text: str) -> str:
    """Escape backticks when embedded in an inline ``code`` span."""
    # A value containing `` needs the whole span to use more backticks —
    # simpler to swap the runs to tilde-visible so output stays safe.
    return text.replace("`", "‘")


def _neutralize_details(text: str) -> str:
    """Neutralize stray ``</details>`` / ``<details>`` tags in prose.

    When finding descriptions or fixes are interpolated into a
    ``<details>``-wrapped body, a ``</details>`` substring inside them
    closes our outer block early, cascading into subsequent findings and
    breaking the GitHub PR render. We replace the angle brackets with
    zero-width-escaped equivalents so the text still reads as intended
    but the tag no longer parses.
    """
    return text.replace("</details>", "<​/details>").replace("<details>", "<​details>")


def format_pr_comment(findings: list[Finding]) -> str:
    """Render findings as a Markdown block suitable for a PR body.

    Empty ``findings`` produces a short "no findings" acknowledgement —
    posting *something* is still useful signal for the reviewer
    ("advisor ran, nothing landed") compared to silence.
    """
    if not findings:
        return "## Advisor review\n\n_No findings at the current threshold._\n"

    counts: dict[str, int] = {s: 0 for s in _SEVERITY_ORDER}
    for f in findings:
        sev = f.severity.upper()
        counts[sev] = counts.get(sev, 0) + 1

    lines: list[str] = [
        "## Advisor review",
        "",
        f"**{len(findings)} finding(s)**",
        "",
        "| Severity | Count |",
        "| --- | ---: |",
    ]
    for sev in _SEVERITY_ORDER:
        lines.append(f"| {sev} | {counts.get(sev, 0)} |")
    lines.append("")
    lines.append("### Details")
    lines.append("")
    for f in findings:
        title = _escape_table_cell(f.description)[:100] or "(no description)"
        lines.append(
            f"<details><summary><strong>[{f.severity}]</strong> "
            f"<code>{_escape_inline(f.file_path)}</code> — {title}</summary>"
        )
        lines.append("")
        lines.append(f"**Description:** {_neutralize_details(f.description)}")
        lines.append("")
        lines.append("**Evidence:**")
        lines.append("")
        lines.append("```")
        lines.append(_neutralize_details(f.evidence).replace("```", "'''"))
        lines.append("```")
        lines.append("")
        lines.append(f"**Fix:** {_neutralize_details(f.fix)}")
        if f.rule_id:
            lines.append("")
            lines.append(f"**Rule:** `{_escape_inline(f.rule_id)}`")
        lines.append("")
        lines.append("</details>")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
