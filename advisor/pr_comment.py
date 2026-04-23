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

# GitHub rejects PR/issue bodies and comments above 65,536 characters with a
# 422 error. Leave headroom for the trailing truncation notice.
_GITHUB_BODY_LIMIT = 60_000


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

    # Unknown severities are clamped to LOW so the per-severity table rows
    # sum to ``len(findings)``. Without this, a finding tagged ``"INFO"``
    # (or any non-canonical severity) would increment the total headline
    # but never appear in any row — the table would silently under-count.
    counts: dict[str, int] = {s: 0 for s in _SEVERITY_ORDER}
    for f in findings:
        sev = f.severity.upper()
        if sev not in counts:
            sev = "LOW"
        counts[sev] += 1

    lines: list[str] = [
        "## Advisor review",
        "",
        f"**{len(findings)} {'finding' if len(findings) == 1 else 'findings'}**",
        "",
        "| Severity | Count |",
        "| --- | ---: |",
    ]
    for sev in _SEVERITY_ORDER:
        lines.append(f"| {sev} | {counts[sev]} |")
    lines.append("")
    lines.append("### Details")
    lines.append("")
    # Track the running character count incrementally so the truncation
    # check stays O(n) overall. Summing ``lines`` every iteration was
    # O(n²) — painful for 500+ findings approaching the body cap.
    projected_chars = sum(len(line) + 1 for line in lines)
    rendered_count = 0
    truncated = False
    for f in findings:
        title = _escape_table_cell(f.description)[:100] or "(no description)"
        block: list[str] = [
            (
                f"<details><summary><strong>[{f.severity}]</strong> "
                f"<code>{_escape_inline(f.file_path)}</code> — {title}</summary>"
            ),
            "",
            f"**Description:** {_neutralize_details(f.description)}",
            "",
            "**Evidence:**",
            "",
            "```",
            _neutralize_details(f.evidence).replace("```", "'''"),
            "```",
            "",
            f"**Fix:** {_neutralize_details(f.fix)}",
        ]
        if f.rule_id:
            block.append("")
            block.append(f"**Rule:** `{_escape_inline(f.rule_id)}`")
        block.append("")
        block.append("</details>")
        block.append("")
        # Stop appending once the running body would exceed the GitHub cap.
        # Posting a truncated comment with a "run locally" pointer is more
        # useful than a 422 from the API.
        block_chars = sum(len(line) + 1 for line in block)
        if projected_chars + block_chars > _GITHUB_BODY_LIMIT and rendered_count > 0:
            truncated = True
            break
        lines.extend(block)
        projected_chars += block_chars
        rendered_count += 1
    if truncated:
        omitted = len(findings) - rendered_count
        lines.append(
            f"_Output truncated to fit GitHub's body length cap — "
            f"{omitted} {'finding' if omitted == 1 else 'findings'} omitted. "
            f"Run `advisor` locally for the full report._"
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
