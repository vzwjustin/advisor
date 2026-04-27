"""Verification pass — filters agent findings for real, significant issues.

A final agent reviews all findings from focused agents,
confirms whether each is real and interesting, and filters out noise. This
prevents false positives from wasting human attention.

Relationship with `orchestrate.build_verify_message`:
    This module (``build_verify_prompt`` + ``Finding`` + ``parse_findings_from_text``)
    is the **structured-findings path**: callers collect ``Finding`` objects,
    format them into a fenced block, and ask an agent to CONFIRM/REJECT each.
    ``orchestrate.build_verify_message`` is a **different, complementary**
    helper — a short SendMessage used to resume the live advisor mid-pipeline
    with an already-assembled findings string. Both are public API and
    intentionally separate: one is for offline/batch verification, the other
    for the in-team advisor loop. Don't wire them together blindly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ._fs import normalize_path as _normalize_path_impl
from .orchestrate._fence import fence as _fence

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Finding:
    """A single finding from a focused agent.

    ``rule_id`` is an optional stable identifier for grouping related
    findings (e.g. on GitHub Code Scanning). When absent, the SARIF
    emitter synthesizes one from severity + description hash.
    """

    file_path: str
    severity: str
    description: str
    evidence: str
    fix: str
    rule_id: str | None = None


# Required for a well-formed block; ``rule_id`` is optional and never
# gates parsing — blocks without it still produce a Finding.
_REQUIRED_FIELDS = ("file_path", "severity", "description", "evidence", "fix")

# Accept both ``-`` and ``*`` list markers; some agents (especially those
# that also emit Markdown prose) prefer ``*`` bullets. Without this, a
# runner returning ``* **File**: ...`` blocks would parse as zero findings.
# The unadorned ``**Key**:`` form remains supported as a safety-net for
# agents that drop the list marker entirely.
_KEY_PREFIXES: dict[str, tuple[str, ...]] = {
    "file_path": ("- **File**:", "* **File**:", "**File**:"),
    "severity": ("- **Severity**:", "* **Severity**:", "**Severity**:"),
    "description": ("- **Description**:", "* **Description**:", "**Description**:"),
    "evidence": ("- **Evidence**:", "* **Evidence**:", "**Evidence**:"),
    "fix": ("- **Fix**:", "* **Fix**:", "**Fix**:"),
    "rule_id": ("- **Rule**:", "* **Rule**:", "**Rule**:"),
}

# List-item markers recognized by the block-opening check. Must stay in
# sync with the prefixes above — a bold label only opens a new field slot
# when it appears as a real bullet, not when it's narrative text inside
# another field's body.
_LIST_MARKERS = ("- ", "* ")


_VERIFY_PROMPT_HEAD = (
    "You are the verification agent. Your job is to review findings from "
    "other agents and determine which are real, significant issues versus "
    "false positives or low-value noise.\n\n"
    "## Findings to Verify\n\n"
)
_VERIFY_PROMPT_TAIL = (
    "\n\n## Instructions\n\n"
    "For each finding:\n"
    "1. Read the cited file and line to confirm the issue exists.\n"
    "2. Check whether the issue is exploitable or impactful in practice.\n"
    "3. Reject findings that are:\n"
    "   - False positives (the code is actually safe)\n"
    "   - Theoretical only (requires unrealistic conditions)\n"
    "   - Duplicates of another finding\n"
    "   - Too minor to act on (style nits, unlikely edge cases)\n\n"
    "4. For each finding, output:\n"
    "   - **CONFIRMED** or **REJECTED**\n"
    "   - **Reason**: why you confirmed or rejected it\n\n"
    "5. End with a summary: how many confirmed, how many rejected, "
    "and the top 3 most critical issues to fix first.\n\n"
    "Be strict. Only confirm issues that are real and worth fixing."
)


def format_findings_block(findings: list[Finding]) -> str:
    """Format findings into a markdown block for the verification prompt."""
    if not findings:
        return "_No findings to verify._"

    lines: list[str] = []
    for i, f in enumerate(findings, 1):
        lines.append(f"### Finding {i}")
        lines.append(f"- **File**: `{f.file_path}`")
        lines.append(f"- **Severity**: {f.severity}")
        lines.append(f"- **Description**: {f.description}")
        lines.append(f"- **Evidence**: {f.evidence}")
        lines.append(f"- **Fix**: {f.fix}")
        if f.rule_id:
            lines.append(f"- **Rule**: {f.rule_id}")
        lines.append("")

    return "\n".join(lines)


def build_verify_prompt(findings: list[Finding]) -> str:
    """Build the complete verification agent prompt.

    The findings block is wrapped via :func:`orchestrate._fence.fence`, which
    picks a fence longer than any backtick run inside the payload. This
    prevents a finding whose evidence/fix field contains ``` from breaking
    out of the fenced data block and being interpreted as prompt text.
    """
    block = format_findings_block(findings)
    return _VERIFY_PROMPT_HEAD + _fence(block) + _VERIFY_PROMPT_TAIL


def parse_findings_from_text(
    text: str,
    batch_files: set[str] | None = None,
) -> list[Finding]:
    """Best-effort parse of agent output into Finding objects.

    Blocks missing any of the five required fields are dropped rather than
    silently promoted with default values. This prevents the verification
    pipeline from acting on fabricated data when agent output is malformed.

    Block boundaries are detected in two ways (primary first):
    1. ``### Finding`` header lines — the primary delimiter, matches the
       format emitted by ``format_findings_block``.
    2. A second ``file_path`` key — safety-net for output that omits headers.

    Within a block, a key match is only treated as a new field if the key
    has not already been set in the current block. Otherwise the line is
    treated as continuation text for the active field. This prevents field
    labels that appear inside description/evidence/fix bodies (e.g. a
    description containing "Fix: use parameterized queries") from corrupting
    the block.

    If ``batch_files`` is provided, findings whose ``file_path`` is not in
    the batch are **dropped** with a warning emitted via the module logger.
    This is the structural scope-drift filter: a runner assigned to
    ``{auth.py, session.py}`` that wanders into ``crypto.py`` cannot land
    findings against ``crypto.py`` in the final report. Path matching
    normalizes leading ``./`` and backslashes; callers should pass
    repo-relative POSIX paths.

    Use :func:`parse_findings_with_drift` if you need the list of dropped
    findings as well (e.g. for the ``advisor audit`` subcommand).
    """
    kept, _dropped = parse_findings_with_drift(text, batch_files)
    return kept


def parse_findings_with_drift(
    text: str,
    batch_files: set[str] | None = None,
) -> tuple[list[Finding], list[Finding]]:
    """Parse findings, returning ``(kept, dropped_out_of_batch)``.

    When ``batch_files`` is ``None``, the second list is always empty and
    every well-formed finding is kept — identical behavior to the original
    parser before the scope filter was introduced.

    When ``batch_files`` is a (possibly empty) set, findings whose
    normalized ``file_path`` is absent from the set are moved to the
    ``dropped`` list and a warning is logged. An empty set drops every
    finding — callers should decide whether to pass ``None`` or ``set()``.
    """
    normalized_batch: set[str] | None
    if batch_files is None:
        normalized_batch = None
    else:
        normalized_batch = {_normalize_path(p) for p in batch_files}
        if not normalized_batch:
            _log.warning(
                "parse_findings_with_drift: batch_files is an empty set; "
                "every finding will be dropped. Pass None to disable scope "
                "filtering instead."
            )

    raw = _parse_blocks(text)
    if normalized_batch is None:
        return raw, []

    kept: list[Finding] = []
    dropped: list[Finding] = []
    for f in raw:
        if _normalize_path(f.file_path) in normalized_batch:
            kept.append(f)
        else:
            dropped.append(f)
            _log.warning(
                "scope-drift: dropped finding on %r (not in batch of %d files)",
                f.file_path,
                len(normalized_batch),
            )
    return kept, dropped


def _normalize_path(path: str) -> str:
    """Normalize a file path for batch-membership comparison.

    Thin alias over :func:`advisor._fs.normalize_path`. Kept as a
    module-level name so callers that import ``verify._normalize_path``
    continue to work; the real implementation lives in ``_fs`` so
    :mod:`advisor.runner_budget` shares the identical definition.
    """
    return _normalize_path_impl(path)


def _parse_blocks(text: str) -> list[Finding]:
    """Inner parser shared by the scoped and unscoped public entry points."""
    findings: list[Finding] = []
    current: dict[str, str] = {}
    active_key: str | None = None
    # "list" or "plain"; keeps body labels from stealing slots.
    field_style: str | None = None
    in_header_block: bool = False  # True once we see any ### Finding header
    in_fence: bool = False  # True while inside a fenced code block (``` or ~~~)
    fence_marker: str | None = None  # opening marker that started the current fence

    def _flush() -> None:
        finding = _dict_to_finding(current)
        if finding is not None:
            findings.append(finding)

    def _match_key(stripped: str) -> tuple[str, str] | None:
        for key, prefixes in _KEY_PREFIXES.items():
            for prefix in prefixes:
                if stripped.startswith(prefix):
                    return key, _extract_value(stripped, prefix)
        return None

    for line in text.split("\n"):
        stripped = line.strip()

        # Track fence state for the entire file, not just inside an active
        # field. A runner emitting fenced prose before any field would
        # otherwise leave `in_fence=False` and the H2/`### Finding` guards
        # below would treat fenced headings as real boundaries.
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = "```" if stripped.startswith("```") else "~~~"
            if fence_marker is None:
                fence_marker = marker
                in_fence = True
            elif fence_marker == marker:
                fence_marker = None
                in_fence = False
            # mismatched marker inside an open fence: ignore, stay in fence
            if active_key:
                continue
            continue

        # Report boundary: an H2 heading (e.g. "## Summary") closes the
        # current findings region. Reset the latch so any later headerless
        # findings region in the same text re-arms the second-File safety
        # net correctly instead of staying permanently disabled.
        # Mirror the ### Finding guard below: only flush a block that is
        # already complete. An incomplete in-progress block followed by an
        # H2 heading was previously _flush()ed unconditionally and lost via
        # _dict_to_finding's missing-fields drop — the H2 line itself was
        # almost certainly continuation of the active body (e.g. a runner
        # pasted "## relevant section" inside an Evidence block).
        # When inside a fenced code block, never treat H2 as a boundary —
        # it's source code inside an Evidence or Fix value, not a report section.
        # Fence auto-recovery (mirror of the ### Finding rule): a column-0 H2
        # heading while we still think we're in a fence is almost certainly an
        # unclosed code block from the prior finding's body. Auto-close so the
        # H2 is honored as a region terminator.
        if line.startswith("## ") and not line.startswith("### ") and in_fence:
            in_fence = False
            fence_marker = None
        if not in_fence and stripped.startswith("## ") and not stripped.startswith("### "):
            block_is_complete = all(k in current for k in _REQUIRED_FIELDS)
            if not active_key or block_is_complete:
                if current:
                    _flush()
                current = {}
                active_key = None
                field_style = None
                in_header_block = False
                continue
            # Mid-body for a still-incomplete block — treat the H2 line as
            # continuation of the active field so it doesn't vanish.
            if active_key:
                current[active_key] = (current[active_key] + " " + stripped).strip()
            continue

        # Primary block delimiter: ### Finding headers from format_findings_block.
        # Guard against a runner embedding "### Finding 3 (see above)" inside
        # an Evidence or Fix body — that would otherwise flush a partial
        # in-progress block and silently drop it via _dict_to_finding. Only
        # honor the header as a boundary when either no block is in progress
        # (active_key is None) OR the current block already has every required
        # field (so flushing is safe).
        # Fence auto-recovery: real ``### Finding`` headers from
        # ``format_findings_block`` are always emitted at column 0. A column-0
        # header that appears while we still think we're inside a fence is
        # almost certainly evidence that the previous finding's body had an
        # unclosed code block — auto-close the fence so the new header is
        # honored as a boundary instead of silently swallowing the rest of
        # the input. Indented ``### Finding`` lines (real code examples) are
        # still suppressed by the fence latch.
        if line.startswith("### Finding") and in_fence:
            in_fence = False
            fence_marker = None
        if not in_fence and stripped.startswith("### Finding"):
            block_is_complete = all(k in current for k in _REQUIRED_FIELDS)
            if not active_key or block_is_complete:
                if current:
                    _flush()
                current = {}
                active_key = None
                field_style = None
                in_header_block = True
                continue
            # Otherwise we're mid-body for a still-incomplete block — treat
            # the line as continuation of the active field so it doesn't
            # vanish into a dropped partial.
            if active_key:
                current[active_key] = (current[active_key] + " " + stripped).strip()
            continue

        matched = _match_key(stripped)
        # A key-label opens a new field when it is a proper list item, or when
        # the block started in the plain ``**Key**:`` form. Plain labels inside
        # a list-style block remain narrative text, so an Evidence value that
        # includes ``**Fix**: ...`` does not steal the real fix slot.
        is_list_item = stripped.startswith(_LIST_MARKERS)
        is_plain_item = stripped.startswith("**")
        opens_plain_block = (
            matched is not None
            and is_plain_item
            and (field_style == "plain" or (field_style is None and not current))
        )

        if matched is not None and (is_list_item or opens_plain_block) and not in_fence:
            key, value = matched
            if key not in current:
                # New field for this block — record it.
                current[key] = value
                active_key = key
                if field_style is None:
                    field_style = "list" if is_list_item else "plain"
            elif key == "file_path" and not in_header_block:
                # Safety-net: second file_path signals a new block in
                # header-less output. Only active when no ### Finding headers
                # have been seen — inside a header-delimited block, a second
                # - **File**: line is continuation prose, not a block boundary.
                _flush()
                current = {key: value}
                active_key = key
                field_style = "list" if is_list_item else "plain"
            else:
                # Key already set — this label text appeared inside a body
                # value. Treat as continuation of the active field.
                if active_key:
                    current[active_key] = (current[active_key] + " " + stripped).strip()
        elif active_key and stripped and not stripped.startswith("### Finding"):
            # Fence markers are already handled at the top of the loop and
            # never reach here — accumulate everything else into the active field.
            current[active_key] = (current.get(active_key, "") + " " + stripped).strip()

    if current:
        _flush()

    return findings


def _extract_value(line: str, prefix: str) -> str:
    """Extract the value after the known prefix in a '**Key**: value' line.

    Uses the matched prefix length to avoid splitting on colons inside the
    value (e.g. Windows paths like ``C:\\Users\\...``). The single caller in
    :func:`_match_key` always supplies a non-empty prefix from
    ``_KEY_PREFIXES``, so no colon-split fallback is needed.

    The triple ``strip().strip("`").strip()`` is deliberate: the first
    strip removes whitespace around the backticked span, the second
    strips the surrounding backticks, and the third re-strips any
    whitespace that was *inside* the backticks. Without the final
    strip, an emitted ``- **File**: ` foo ``` would round-trip as the
    literal ``" foo "`` (with the inner whitespace surviving), which
    breaks downstream allowlist and path-matching consumers that key
    on the trimmed value.
    """
    return line[len(prefix) :].strip().strip("`").strip()


def _dict_to_finding(d: dict[str, str]) -> Finding | None:
    """Convert a parsed block to a Finding, or None if any required field is missing.

    ``rule_id`` is optional — absent from the dict is fine and yields
    ``rule_id=None`` on the Finding.
    """
    missing = [k for k in _REQUIRED_FIELDS if not d.get(k)]
    if missing:
        return None
    return Finding(
        file_path=d["file_path"],
        severity=d["severity"],
        description=d["description"],
        evidence=d["evidence"],
        fix=d["fix"],
        rule_id=d.get("rule_id") or None,
    )
