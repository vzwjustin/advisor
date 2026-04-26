"""Format findings as a GitHub-flavored markdown PR comment.

Emits a collapsible ``<details>`` block per finding plus a summary table
keyed by severity. Safe to paste into a PR body — every user-controlled
finding field is HTML-escaped before landing inside the intentional HTML
markup (``<details>``, ``<summary>``, ``<code>``, ``<strong>``), pipe
characters are escaped so they don't break the summary table, and the
evidence block uses the existing fenced-code-block fence-collision
neutralizer (replace ``` with ''' so the fence stays balanced).

Pure string-in, string-out. No I/O.
"""

from __future__ import annotations

import html
import re

from .verify import Finding

# Defense-in-depth for evidence content: a stray ``</details>`` inside the
# fenced evidence block could close our outer ``<details>`` early on a
# Markdown renderer that mishandles the fence. Replace the leading ``<``
# with ``&lt;`` so the literal text survives the fence rendering and
# never reaches the HTML parser as a real tag.
_DETAILS_TAG_RE = re.compile(r"<(/?)details\b", re.IGNORECASE)

_SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW")

# GitHub rejects PR/issue bodies and comments above 65,536 characters with a
# 422 error. Leave headroom for the trailing truncation notice.
_GITHUB_BODY_LIMIT = 60_000


def _escape_html(text: str) -> str:
    """HTML-escape a user-controlled field that lands inside HTML markup.

    Wraps :func:`html.escape` with ``quote=True`` so attribute-context
    payloads (``onerror="…"``) cannot break out either, even though all
    current call sites land inside element bodies, not attributes —
    defense in depth for any future template tweak.
    """
    return html.escape(text, quote=True)


def _escape_summary(text: str) -> str:
    """Escape a field for placement inside a ``<summary>`` element.

    The summary line also serves as the `<details>` title in the rendered
    page; collapse newlines (so a multi-line description doesn't break the
    summary onto two visual rows) and escape pipes (so a stray ``|`` in
    a finding's description can't accidentally extend the preceding
    summary table on some Markdown renderers).
    """
    return _escape_html(text).replace("|", "\\|").replace("\n", " ")


def _escape_inline_code(text: str) -> str:
    """Escape a field for placement inside an HTML ``<code>`` element.

    HTML-escape covers ``<``/``>``/``&``/quotes; the literal-backtick swap
    keeps the value visually distinct when GitHub re-renders the inner
    text as Markdown after stripping the outer ``<code>`` (a quirk of GFM).
    """
    return _escape_html(text).replace("`", "‘")


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
        # Truncate before HTML-escape so the slice can't land in the middle of
        # a multi-character entity like ``&lt;``.
        title_raw = f.description[:100] or "(no description)"
        title = _escape_summary(title_raw)
        block: list[str] = [
            (
                f"<details><summary><strong>[{_escape_summary(f.severity)}]</strong> "
                f"<code>{_escape_inline_code(f.file_path)}</code> — {title}</summary>"
            ),
            "",
            f"**Description:** {_escape_html(f.description)}",
            "",
            "**Evidence:**",
            "",
            "```",
            # Inside a fenced code block GitHub renders content as literal
            # text — HTML-escaping would surface ``&lt;`` to the reader.
            # We only neutralize (a) the closing fence itself so the wrapper
            # block stays balanced and (b) ``<details>`` tag-shaped strings
            # so a renderer that mishandles the fence can't accidentally
            # close our outer ``<details>`` early.
            _DETAILS_TAG_RE.sub(
                lambda m: f"&lt;{m.group(1)}details", f.evidence.replace("```", "'''")
            ),
            "```",
            "",
            f"**Fix:** {_escape_html(f.fix)}",
        ]
        if f.rule_id:
            block.append("")
            block.append(f"**Rule:** `{_escape_inline_code(f.rule_id)}`")
        block.append("")
        block.append("</details>")
        block.append("")
        # Stop appending once the running body would exceed the GitHub cap.
        # Posting a truncated comment with a "run locally" pointer is more
        # useful than a 422 from the API.
        block_chars = sum(len(line) + 1 for line in block)
        if projected_chars + block_chars > _GITHUB_BODY_LIMIT:
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
