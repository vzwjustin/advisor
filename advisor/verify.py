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
    expected_vs_actual: str = ""


# Required for a well-formed block; ``rule_id`` is optional and never
# gates parsing — blocks without it still produce a Finding.
_REQUIRED_FIELDS = ("file_path", "severity", "description", "evidence", "fix")

# Severity allowlist — kept symmetric with ``history._ALLOWED_SEVERITIES``
# so that the parse-time gate matches what the history loader and SARIF
# emitter expect. A runner emitting a mixed-case (``"High"``) or invented
# (``"INVENTED"``) severity flows through ``_canonical_severity``, which
# upper-cases and validates against this set before constructing the
# ``Finding``. Unknown values are coerced to ``"UNKNOWN"`` with a logged
# warning so downstream consumers (baseline rule_id keying, history
# ingestion, SARIF level mapping) see a single canonical form.
_ALLOWED_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW"})


def _canonical_severity(raw: str, *, context: str = "") -> str:
    """Canonicalize a runner-supplied severity string against the allowlist.

    Returns one of ``CRITICAL`` / ``HIGH`` / ``MEDIUM`` / ``LOW``, or
    ``"UNKNOWN"`` for any input that does not match after strip + upper.
    Logs a warning on coercion so operators can see drift in runner output.

    Centralized here so all three ``Finding`` construction sites (the parser
    at ``_dict_to_finding``, the JSON-import path at
    ``__main__._load_findings_from_input``, and ``pr_comment.sanitize_finding``)
    see the same canonical form. Without this, two of the three paths
    previously emitted un-canonicalized severities like ``"Critical"`` or
    ``"INVENTED"`` straight through to SARIF / baseline / PR rendering — the
    docstring guarantee on ``_ALLOWED_SEVERITIES`` was materially false.

    ``context`` is a free-form identifier (file path, ``"<json-import>"``,
    etc.) included in the coercion warning so a noisy run can be traced to
    its source. Empty by default — callers that have no useful context can
    omit.
    """
    sev_raw = raw.strip().upper()
    if sev_raw in _ALLOWED_SEVERITIES:
        return sev_raw
    _log.warning(
        "verify: coercing unknown severity %r to 'UNKNOWN'%s",
        raw,
        f" (context: {context})" if context else "",
    )
    return "UNKNOWN"


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
    "expected_vs_actual": (
        # Unicode arrow (U+2192) — what FINDING_SCHEMA tells runners to
        # emit. Most runners follow the schema verbatim.
        "- **Expected → Actual**:",
        "* **Expected → Actual**:",
        "**Expected → Actual**:",
        # ASCII arrow fallback — LLM runners (and humans hand-editing
        # findings) routinely autocorrect ``→`` to ``->``. Without these
        # variants the line bleeds into the prior field as continuation
        # text and the divergence signal is lost. Mirrors the same
        # tolerance the SCOPE-anchor parser at runner_budget.py extends
        # to ``·``/``|``/``-`` separators.
        "- **Expected -> Actual**:",
        "* **Expected -> Actual**:",
        "**Expected -> Actual**:",
    ),
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


def _safe_inline(s: str) -> str:
    """Sanitize a runner-authored field for inline embedding in the findings block.

    The findings block is rendered as Markdown bullets that the verification
    LLM parses as structured fields. The outer :func:`_fence` wrapper prevents
    fence-escape attacks but does NOT prevent a runner from forging additional
    finding fields *inside* the fenced block by embedding ``\\n- **Severity**:``
    or a stray backtick that closes an inline-code span. Strip backticks
    (replace with ``'``) and collapse newlines/CRs to spaces so the rendered
    text stays on one line and can never inject another bullet.

    Also collapse Unicode line separators U+2028 / U+2029 — advisor's own
    parser splits on ``\\n`` only and is safe, but the downstream verifier
    LLM consuming this block may render those code points as visual
    newlines and be confused about which severity to confirm. Defense in
    depth against verifier-LLM injection rather than against advisor's parser.
    NUL (U+0000) and zero-width code points (U+200B / U+200C / U+200D /
    U+FEFF / U+00AD) are dropped entirely so a runner cannot smuggle
    invisible bytes into a finding field.
    """
    return (
        s.replace("`", "'")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace(" ", " ")
        .replace(" ", " ")
        .replace("", " ")
        .replace("\x00", "")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
        .replace("\u00ad", "")
    )


def format_findings_block(findings: list[Finding]) -> str:
    """Format findings into a markdown block for the verification prompt."""
    if not findings:
        return "_No findings to verify._"

    lines: list[str] = []
    for i, f in enumerate(findings, 1):
        lines.append(f"### Finding {i}")
        lines.append(f"- **File**: `{_safe_inline(f.file_path)}`")
        lines.append(f"- **Severity**: {_safe_inline(f.severity)}")
        lines.append(f"- **Description**: {_safe_inline(f.description)}")
        lines.append(f"- **Evidence**: {_safe_inline(f.evidence)}")
        if f.expected_vs_actual:
            lines.append(f"- **Expected → Actual**: {_safe_inline(f.expected_vs_actual)}")
        lines.append(f"- **Fix**: {_safe_inline(f.fix)}")
        if f.rule_id:
            lines.append(f"- **Rule**: {_safe_inline(f.rule_id)}")
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
        # Unscoped path: callers expect well-formed Findings only.
        # ``<incomplete>`` sentinels emitted by ``_dict_to_finding`` for
        # partial drops are filtered here so the public API stays the
        # same as before the drift-tally fix.
        return [f for f in raw if f.file_path != INCOMPLETE_FILE_PATH], []

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

    Note: does NOT case-fold — runners must echo paths with the exact
    casing they received in the explore phase. See
    :func:`advisor._fs.normalize_path` for the full contract.
    """
    return _normalize_path_impl(path)


def _parse_blocks(text: str) -> list[Finding]:
    """Inner parser shared by the scoped and unscoped public entry points."""
    findings: list[Finding] = []
    current: dict[str, str] = {}
    # Continuation pieces for each active field, accumulated as a list and
    # joined at flush time. Replaces the prior ``current[key] = current[key]
    # + " " + stripped`` pattern, which was O(n²) when an LLM emitted
    # multi-megabyte output with no field delimiters: every continuation
    # line copied the growing string. List-append is O(1); the join at
    # flush is O(n) total.
    parts: dict[str, list[str]] = {}
    active_key: str | None = None
    # "list" or "plain"; keeps body labels from stealing slots.
    field_style: str | None = None
    in_header_block: bool = False  # True once we see any ### Finding header
    in_fence: bool = False  # True while inside a fenced code block (``` or ~~~)
    fence_marker: str | None = None  # opening marker that started the current fence

    def _merge_parts() -> None:
        """Fold accumulated continuation pieces back into ``current``.

        Idempotent: clears ``parts`` after merging so subsequent calls are
        no-ops until new content accumulates.
        """
        for key, pieces in parts.items():
            if not pieces:
                continue
            joined = " ".join(pieces)
            existing = current.get(key, "")
            current[key] = (existing + " " + joined).strip() if existing else joined.strip()
        parts.clear()

    def _append(key: str, value: str) -> None:
        """Append a continuation piece to ``parts[key]`` — O(1) per line."""
        parts.setdefault(key, []).append(value)

    def _flush() -> None:
        _merge_parts()
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
            else:
                # Close on any triple-marker line while in a fence — runners
                # routinely emit mismatched pairs (``~~~`` open, ``\`\`\``
                # close). The strict same-marker policy left ``in_fence``
                # latched True past the intended close, swallowing the
                # subsequent ``Fix:`` line and dropping the finding via the
                # ``missing fix`` partial-drop. Close-on-any matches the H2
                # auto-recovery's close-then-recover preference.
                fence_marker = None
                in_fence = False
            # Whether or not a field is currently active, a fence-marker line
            # is itself not field content — skip it.
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
                _append(active_key, stripped)
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
                _append(active_key, stripped)
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
                    _append(active_key, stripped)
        elif active_key and stripped and not stripped.startswith("### Finding"):
            # Fence markers are already handled at the top of the loop and
            # never reach here — accumulate everything else into the active field.
            _append(active_key, stripped)

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


# Sentinel ``file_path`` value used when a block had at least one required
# field but was missing others (a "partial drop"). Synthesizing a Finding
# with this path lets the drift tally in ``parse_findings_with_drift``
# include partial drops in its dropped count — otherwise an incomplete
# block at EOF disappears from both the kept and dropped lists and the
# audit undercounts parse misses.
INCOMPLETE_FILE_PATH = "<incomplete>"


def _dict_to_finding(d: dict[str, str]) -> Finding | None:
    """Convert a parsed block to a Finding, or None if the block was empty.

    Returns ``None`` only when the block was empty (no required field
    populated or present-but-empty). Otherwise returns either a
    well-formed Finding or a synthesized partial-drop Finding with
    ``file_path == INCOMPLETE_FILE_PATH`` so the caller can surface it
    in the drift tally. ``rule_id`` is optional — absent from the dict
    yields ``rule_id=None``.
    """
    # Distinguish "field absent from the parsed dict" from "field present
    # but empty string" — both are equally invalid as a Finding, but they
    # come from different upstream emit bugs and the log should show
    # which one happened.
    absent = [k for k in _REQUIRED_FIELDS if k not in d]
    empty = [k for k in _REQUIRED_FIELDS if k in d and not d[k]]
    if absent or empty:
        # Surface partial drops: if the block has at least one populated
        # required field, the body almost certainly intended to be a
        # finding (e.g. a Fix line consumed inside an unclosed fence
        # before auto-recovery). Silent loss in that path masked real
        # findings during pass M-N audits.
        populated = [k for k in _REQUIRED_FIELDS if d.get(k)]
        if not populated:
            return None
        _log.warning(
            "verify: dropping partial finding (have %s, empty %s, absent %s)",
            populated,
            empty,
            absent,
        )
        # Synthesize an incomplete Finding so the drift tally in
        # parse_findings_with_drift can account for parse misses. The
        # caller filters this sentinel out of the kept list when batch
        # filtering is disabled (preserves the original unscoped API).
        return Finding(
            file_path=INCOMPLETE_FILE_PATH,
            severity=_canonical_severity(d.get("severity", ""), context=INCOMPLETE_FILE_PATH),
            description=d.get("description") or f"<partial: empty={empty} absent={absent}>",
            evidence=d.get("evidence") or "",
            fix=d.get("fix") or "",
            rule_id=d.get("rule_id") or None,
            expected_vs_actual=d.get("expected_vs_actual", ""),
        )
    return Finding(
        file_path=d["file_path"],
        severity=_canonical_severity(d["severity"], context=d["file_path"]),
        description=d["description"],
        evidence=d["evidence"],
        fix=d["fix"],
        rule_id=d.get("rule_id") or None,
        expected_vs_actual=d.get("expected_vs_actual", ""),
    )
