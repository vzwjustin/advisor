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

from .sarif import _strip_controls
from .verify import Finding


def _sanitize(f: Finding) -> Finding:
    """Strip C0 control bytes from each user-controlled field on a Finding.

    Mirrors the SARIF emitter's invariant: NUL, BEL, BACKSPACE, and other
    C0 controls do not survive into the rendered output. ``\\t``, ``\\n``,
    ``\\r`` are preserved (``keep_block_whitespace=True``) because they're
    meaningful inside the fenced evidence block and the inline helpers
    already collapse them where needed for single-line contexts. Returns a
    new frozen :class:`Finding` so callers see the sanitized values
    without mutating their inputs.
    """
    return Finding(
        file_path=_strip_controls(f.file_path, keep_block_whitespace=True),
        severity=_strip_controls(f.severity, keep_block_whitespace=True),
        description=_strip_controls(f.description, keep_block_whitespace=True),
        evidence=_strip_controls(f.evidence, keep_block_whitespace=True),
        fix=_strip_controls(f.fix, keep_block_whitespace=True),
        rule_id=(
            _strip_controls(f.rule_id, keep_block_whitespace=True) if f.rule_id else f.rule_id
        ),
        expected_vs_actual=_strip_controls(f.expected_vs_actual, keep_block_whitespace=True),
    )


# Defense-in-depth for evidence content: a stray ``</details>`` inside the
# fenced evidence block could close our outer ``<details>`` early on a
# Markdown renderer that mishandles the fence. Replace the leading ``<``
# with ``&lt;`` so the literal text survives the fence rendering and
# never reaches the HTML parser as a real tag.
_DETAILS_TAG_RE = re.compile(r"<(/?)details\b", re.IGNORECASE)

_SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM", "LOW")

# GitHub rejects PR/issue bodies and comments above 65,536 UTF-8 bytes
# with a 422 error. The cap is byte-measured, not char-measured — a
# CJK-heavy comment (3 bytes/char) was previously able to slip past a
# 60_000-char guard and still exceed 65_536 bytes. Leave headroom for
# the trailing truncation notice.
_GITHUB_BODY_LIMIT = 60_000

# Per-finding evidence cap. A single verbose evidence block (e.g. a
# pasted 10KB stack trace) used to be able to truncate the whole
# comment after only 2-3 findings; that mis-attributed truncation to
# finding count when it was really one outlier. Capping evidence first
# means truncation is now driven by total finding count.
#
# Measured in **bytes**, not characters — the downstream body budget
# ``_GITHUB_BODY_LIMIT`` is byte-measured, so a char-only cap let a
# 500-char CJK evidence block consume up to 1,500 bytes of the body and
# crowd other findings out of the comment.
_EVIDENCE_BYTE_CAP = 500


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


def _cap_evidence(evidence: str) -> str:
    """Cap a single evidence block at ``_EVIDENCE_BYTE_CAP`` UTF-8 bytes."""
    encoded = evidence.encode("utf-8")
    if len(encoded) <= _EVIDENCE_BYTE_CAP:
        return evidence
    # ``errors="ignore"`` drops any trailing partial code point left by
    # slicing mid-character so the result is always valid UTF-8.
    # Reserve the full UTF-8 byte width of the ``…`` ellipsis (U+2026 is
    # 3 bytes: 0xE2 0x80 0xA6). The prior slice ``CAP - 1`` left only 1
    # byte for the ellipsis and overshot the cap by up to 2 bytes — a
    # contract violation against the docstring promise even if upstream
    # body-limit accounting absorbs it.
    ellipsis_bytes = len("…".encode())
    return (
        encoded[: _EVIDENCE_BYTE_CAP - ellipsis_bytes].decode("utf-8", errors="ignore").rstrip()
        + "…"
    )


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

    # Strip C0 controls from every user field before any rendering. Matches
    # the SARIF emitter's invariant — NUL/BEL/etc. cannot survive into the
    # output, where they'd render as replacement glyphs and could trip the
    # GitHub API's body validator.
    findings = [_sanitize(f) for f in findings]

    # Sort by severity so HIGH/CRITICAL findings always appear first in
    # the rendered detail list. The body is truncated at
    # ``_GITHUB_BODY_LIMIT`` UTF-8 bytes when it would exceed GitHub's
    # cap — without this sort, a long-evidence LOW finding could push
    # CRITICAL findings off the bottom of the comment. Reviewers reading
    # the PR body see the most-actionable items first; the truncation
    # message at the bottom names how many got cut. Unknown severities
    # sort last (they're clamped to LOW for table counts but render
    # with their original string). Stable sort preserves the caller's
    # original ordering within each severity bucket.
    _sev_rank = {sev: i for i, sev in enumerate(_SEVERITY_ORDER)}
    findings = sorted(
        findings,
        key=lambda f: _sev_rank.get(f.severity.upper(), len(_SEVERITY_ORDER)),
    )

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
    # Track the running byte count incrementally so the truncation check
    # stays O(n) overall. GitHub's body cap is measured in UTF-8 bytes,
    # not characters; a char-only budget under-counted multibyte text and
    # let CJK-heavy descriptions slip past 60_000 chars while still
    # tripping the 65_536-byte ceiling. Summing ``lines`` every iteration
    # would also be O(n²) — painful for 500+ findings approaching the
    # body cap.
    projected_chars = sum(len(line.encode("utf-8")) + 1 for line in lines)
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
        ]
        if f.expected_vs_actual:
            block.append(f"**Expected → Actual:** {_escape_html(f.expected_vs_actual)}")
            block.append("")
        block += [
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
                lambda m: f"&lt;{m.group(1)}details",
                _cap_evidence(f.evidence).replace("```", "'''"),
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
        block_chars = sum(len(line.encode("utf-8")) + 1 for line in block)
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
