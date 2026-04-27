"""Post-hoc audit of an advisor run transcript.

Given a saved :class:`~advisor.checkpoint.Checkpoint` and the textual
transcript of a Claude Code session that executed it, :func:`audit_transcript`
answers the questions that the runtime has no way to answer for itself:

* How many fixes did each runner actually accept?
* Which runners pinged ``CONTEXT_PRESSURE``? Which didn't but should have?
* Were there any rotations? Did they happen before or after the cap was
  exceeded?
* Did the advisor ever emit the ``PROTOCOL_VIOLATION`` named-stop string?
* Did any runner report findings on files outside its assigned batch?

The analysis is pure-text: we don't parse or re-execute anything. The
transcript is whatever a user pipes in — raw Claude Code output, a
copy-pasted conversation, a log file. Detection is based on the stable
markers emitted by :func:`advisor.orchestrate.build_fix_assignment_message`,
:func:`advisor.orchestrate.build_runner_handoff_message`, and the
runner/advisor prompt templates.

This is intentionally best-effort. A transcript that has been edited,
truncated, or produced by a fork of the prompts may under-count or
mis-attribute events. The audit is a diagnostic aid for post-mortem
analysis of a bad run — not an authoritative trace. Use it the way you'd
use ``git blame``: a starting point, not a verdict.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .checkpoint import Checkpoint
from .verify import Finding, parse_findings_with_drift

# ── Detection patterns ───────────────────────────────────────────
#
# Each pattern targets a stable string emitted by the production prompts
# / message builders. Keep them in sync with:
#   - build_fix_assignment_message   → _FIX_ASSIGNMENT_RE
#   - build_runner_handoff_message   → _HANDOFF_RE
#   - runner_prompts: CONTEXT_PRESSURE ping instruction → _CONTEXT_PRESSURE_RE
#   - advisor.txt: PROTOCOL_VIOLATION named-stop clause → _PROTOCOL_VIOLATION_RE

# "## Fix assignment (fix 3 of 5)" / "## Fix assignment (LAST FIX (5 of 5))"
# The ``fix N of M`` group is non-optional because a fix message without
# a budget stamp predates the enforcement and should show up separately.
_FIX_ASSIGNMENT_RE = re.compile(
    r"##\s+Fix\s+assignment\s*\(\s*"
    r"(?:\*\*LAST\s+FIX\*\*\s*\()?"  # optional "**LAST FIX** (" wrapper
    r"(?:fix\s+)?"
    r"(\d+)\s+of\s+(\d+)",
    re.IGNORECASE,
)

# Heuristic attribution: a ``runner-N`` mention within this many characters
# immediately before a fix-assignment header is treated as the recipient.
# Chosen to span a typical SendMessage(to='runner-N', message='...') call
# plus a little slack, without reaching across unrelated blocks. The
# 500-char window is also wide enough that a fenced JSON-like dispatch
# blob with embedded prose still attributes correctly; tighter windows
# (≤200) under-attribute on real transcripts.
_RUNNER_ATTRIBUTION_WINDOW = 500

_RUNNER_MENTION_RE = re.compile(r"runner-(\d+)")

# Matches the ``CONTEXT_PRESSURE`` self-report token wherever it appears in
# the transcript. Unlike fix-assignment attribution, the count here is the
# raw mention count across the whole transcript — attribution to a specific
# runner is handled by ``context_pressure_runners`` (first-mention order),
# not by proximity windowing, so this regex stays unanchored on purpose.
_CONTEXT_PRESSURE_RE = re.compile(r"CONTEXT_PRESSURE", re.IGNORECASE)

_PROTOCOL_VIOLATION_RE = re.compile(
    # Anchor on line-start so a runner quoting the sentinel inside prose
    # or a fenced evidence block (e.g. ``**Evidence**: we match on
    # PROTOCOL_VIOLATION: ...``) doesn't inflate the violation count.
    # The sentinel is emitted as a standalone top-of-line marker per the
    # advisor prompt; anchoring tightens the match to that structural form.
    r"^PROTOCOL_VIOLATION\s*:\s*[^\n]*",
    re.MULTILINE,
)

_HANDOFF_RE = re.compile(r"##\s+Handoff\s+from\s+runner-\d+")


def _strip_fenced_blocks(text: str) -> str:
    """Return ``text`` with triple-backtick / ``~~~`` fenced regions removed.

    Used to suppress PROTOCOL_VIOLATION false-positives when a runner quotes
    the sentinel inside a code-fenced example. Fence markers may carry
    leading whitespace — match ``verify.py``'s indent-tolerant parser so a
    nested-list fence (four-space indent before the triple-backtick) is
    recognized as a real fence.
    """
    lines = text.split("\n")
    out: list[str] = []
    in_fence = False
    marker: str | None = None
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            this_marker = "```" if stripped.startswith("```") else "~~~"
            if marker is None:
                marker = this_marker
                in_fence = True
                out.append("")  # preserve line numbering for any later use
                continue
            if marker == this_marker:
                marker = None
                in_fence = False
                out.append("")
                continue
        if in_fence:
            out.append("")
        else:
            out.append(ln)
    return "\n".join(out)


#: Maximum ``PROTOCOL_VIOLATION`` lines surfaced in an :class:`AuditReport`.
#: A pathological transcript with thousands of matches would otherwise
#: inflate report memory without adding signal — the first thousand are
#: enough to triage. The truncation is reported via
#: :attr:`AuditReport.protocol_violations_truncated`.
PROTOCOL_VIOLATION_CAP = 1000


def _runner_sort_key(runner_id: str) -> tuple[int, int, str]:
    """Natural-sort key for ``runner-N`` ids (and the ``runner-?`` sentinel).

    Plain ``sorted(...)`` puts ``runner-10`` before ``runner-2`` because
    it compares strings lexically. Pool sizes are clamped to 20 in
    :class:`TeamConfig` / :func:`_resolve_max_runners`, which is enough
    for the lexical bug to surface in real audit output. Use the parsed
    integer as the primary sort key, with the unattributed ``runner-?``
    sentinel sorted last so it doesn't fall between numeric ids on the
    page. Fallback string compare keeps non-numeric ids stable across
    runs.
    """
    suffix = runner_id[len("runner-") :] if runner_id.startswith("runner-") else runner_id
    if suffix.isdigit():
        return (0, int(suffix), runner_id)
    # ``runner-?`` and any future non-numeric id sort after the numbered ones.
    return (1, 0, runner_id)


@dataclass(frozen=True, slots=True)
class AuditReport:
    """Structured output of :func:`audit_transcript`.

    Every collection is ordered by first appearance in the transcript
    (except ``fix_counts``, which is a final tally keyed by runner_id).

    Attributes:
        run_id: The checkpoint's run_id for traceability.
        max_fixes_per_runner: The cap the run was configured with.
        large_file_line_threshold: The large-file trigger line count.
        large_file_max_fixes: The cap that applies when any batch file
            crosses ``large_file_line_threshold``.
        fix_counts: Runner-id → observed fix-assignment count. Keys are
            strings of the form ``"runner-2"``. Unattributed fix
            assignments (no nearby runner mention) are tallied under
            ``"runner-?"`` so they are still visible.
        cap_overruns: Human-readable lines describing runners whose
            observed fix count exceeded the configured cap. Empty when
            the run stayed within budget.
        context_pressure_runners: Runner ids that emitted
            ``CONTEXT_PRESSURE`` at least once, in first-mention order.
            Duplicates are removed — repeated pings from the same runner
            count once here (the raw count is reported in
            ``context_pressure_count``).
        context_pressure_count: Total ``CONTEXT_PRESSURE`` occurrences,
            including repeats. A runner that pings twice without being
            rotated shows up once in ``context_pressure_runners`` and
            twice here.
        rotations: Number of handoff messages (``## Handoff from
            runner-N``) detected. Each handoff is a rotation.
        protocol_violations: The exact one-line ``PROTOCOL_VIOLATION``
            strings found, in order. Capped at
            :data:`PROTOCOL_VIOLATION_CAP` entries to bound memory on
            pathological transcripts; the ``protocol_violations_truncated``
            flag indicates whether the cap was hit.
        protocol_violations_truncated: ``True`` when the protocol-violation
            scan stopped at the cap before exhausting matches. ``False``
            when every PROTOCOL_VIOLATION line in the transcript is in
            ``protocol_violations``. Surfaced in the human-readable
            report so a reader can tell "0 violations" from "1000+
            violations and we stopped counting".
        findings_in_batch: :class:`Finding` objects whose ``file_path``
            is in the union of all batched files.
        findings_out_of_batch: :class:`Finding` objects whose
            ``file_path`` is **not** in any batch — the scope-drift
            evidence the audit surfaces.
        batch_file_count: Size of the union used for in/out-of-batch
            classification. Useful context in reports.
    """

    run_id: str
    max_fixes_per_runner: int
    large_file_line_threshold: int
    large_file_max_fixes: int
    fix_counts: dict[str, int]
    cap_overruns: list[str]
    context_pressure_runners: list[str]
    context_pressure_count: int
    rotations: int
    protocol_violations: list[str]
    findings_in_batch: list[Finding]
    findings_out_of_batch: list[Finding]
    batch_file_count: int = 0
    # Raw per-runner fix numbers observed (e.g. {"runner-2": [1, 2, 3]}).
    # Kept separately from ``fix_counts`` so downstream consumers can tell
    # "3 fixes observed" from "fix numbers 1, 2, 5" (a gap can indicate
    # a transcript was truncated or a fix was re-dispatched).
    fix_numbers: dict[str, list[int]] = field(default_factory=dict)
    protocol_violations_truncated: bool = False


def _collect_batch_files(cp: Checkpoint) -> set[str]:
    """Return the union of every file assigned to any batch in the checkpoint.

    Used for the scope-drift classification. A plan with no batches (flat
    dispatch) falls back to the full task list so the audit still does
    something useful — the alternative would be 'all findings are
    out-of-batch' which is unhelpful noise.
    """
    files: set[str] = set()
    if cp.batches:
        for batch in cp.batches:
            raw_tasks = batch.get("tasks", [])
            if not isinstance(raw_tasks, list):
                continue
            for t in raw_tasks:
                if isinstance(t, dict):
                    fp = t.get("file_path")
                    if isinstance(fp, str) and fp:
                        files.add(fp)
    if not files:
        for t in cp.tasks:
            if isinstance(t, dict):
                fp = t.get("file_path")
                if isinstance(fp, str) and fp:
                    files.add(fp)
    return files


def _attribute_fix_to_runner(transcript: str, match_start: int) -> str:
    """Return the ``runner-N`` id of a fix assignment's recipient.

    Strategy, in priority order:

    1. Most recent ``to='runner-N'`` / ``to="runner-N"`` envelope before
       the marker — that's the literal SendMessage recipient and the
       authoritative answer.
    2. Fallback to the most recent bare ``runner-N`` mention in the
       window (legacy heuristic, kept for transcripts that pre-date the
       structured envelope or use an unparseable variant).
    3. ``"runner-?"`` when no mention is found at all — surfaces the
       ambiguity rather than silently dropping the fix.

    The strategy mirrors :func:`_attribute_context_pressure_to_runner`
    so both attributions agree on what counts as authoritative. Earlier
    versions of this function used only step 2, which misattributed
    fixes when prose in the window mentioned a different runner
    (``"runner-5 found this"`` then ``## Fix assignment …`` for
    runner-2 attributed to runner-5).
    """
    window_start = max(0, match_start - _RUNNER_ATTRIBUTION_WINDOW)
    window = transcript[window_start:match_start]
    # Step 1 — authoritative envelope.
    to_mentions = list(_SENDMESSAGE_TO_RUNNER_RE.finditer(window))
    if to_mentions:
        return f"runner-{to_mentions[-1].group(1)}"
    # Step 2 — bare-mention fallback for transcripts without structured
    # envelopes within the window.
    mentions = list(_RUNNER_MENTION_RE.finditer(window))
    if not mentions:
        return "runner-?"
    return f"runner-{mentions[-1].group(1)}"


# CONTEXT_PRESSURE attribution differs from fix-assignment attribution.
# Fix assignments are dispatches *to* a runner — the most recent
# ``runner-N`` mention before the marker is the recipient (and that's the
# correct actor). CONTEXT_PRESSURE is a self-report *from* a runner, sent
# via ``SendMessage(to='team-lead', message='...')`` (team-lead relays to
# the advisor), so the envelope's ``to=`` field points at team-lead, not
# the runner. Reusing the fix-attribution helper here misattributes the
# ping to whichever runner was last addressed by the advisor.
#
# Strategy, in order:
#   1. Parse the enclosing ``SendMessage(...)`` call. If the message body
#      contains a ``runner-N`` mention (the runner self-identifying inline,
#      e.g. ``'runner-1 CONTEXT_PRESSURE — ...'``), that is the actor.
#   2. Otherwise scan backwards for the most recent ``to='runner-N'`` —
#      the runner currently being addressed by the advisor is the one
#      doing the work, and therefore the one pinging.
#   3. Fall back to ``runner-?``.

# NOTE: matches only literal quoted ``to='runner-N'`` / ``to="runner-N"``.
# F-string or dynamic ids (``to=f'runner-{n}'``) silently fall through and
# the audit attributes the fix to ``runner-?`` instead. Keep call sites
# literal so the audit can attribute correctly.
_SENDMESSAGE_TO_RUNNER_RE = re.compile(r"to\s*=\s*['\"]runner-(\d+)['\"]")
# Capture the message body of a SendMessage(...) call when the marker is
# inside it. Bounded scan — we look in a fixed window before/after the
# marker, not the whole transcript, to avoid pulling identifiers from
# unrelated calls.
_SENDMESSAGE_OPEN_RE = re.compile(r"SendMessage\s*\(")


def _attribute_context_pressure_to_runner(transcript: str, match_start: int) -> str:
    """Return the ``runner-N`` id that emitted a CONTEXT_PRESSURE ping.

    See module-level discussion above. Tries (in order) to read the
    enclosing ``SendMessage(...)`` call's message body for an inline
    ``runner-N`` self-identification, then falls back to the most recent
    ``to='runner-N'`` dispatch envelope as a proxy for the active runner,
    and finally to ``runner-?``.
    """
    window_start = max(0, match_start - _RUNNER_ATTRIBUTION_WINDOW)
    before = transcript[window_start:match_start]

    # Step 1 — find the most recent ``SendMessage(`` opener in the window.
    # If found, search the slice from that opener through ``match_start``
    # for an inline ``runner-N`` mention. That slice is the message body
    # the runner is emitting; an inline ``runner-N`` token there is the
    # runner self-identifying, which is the most reliable signal.
    openers = list(_SENDMESSAGE_OPEN_RE.finditer(before))
    if openers:
        body_start = window_start + openers[-1].end()
        body_slice = transcript[body_start:match_start]
        # Skip the ``to='runner-X'`` recipient field — that's the addressee
        # of the SendMessage, not the sender. For a self-report the
        # addressee is the advisor; if the body still names a runner, it's
        # the sender identifying itself in the prose.
        body_without_to = _SENDMESSAGE_TO_RUNNER_RE.sub("", body_slice)
        body_mentions = list(_RUNNER_MENTION_RE.finditer(body_without_to))
        if body_mentions:
            return f"runner-{body_mentions[-1].group(1)}"

    # Step 2 — fall back to the most recent ``to='runner-N'`` dispatch
    # before the marker. The active runner (the one the advisor most
    # recently addressed) is the one doing the work and therefore the
    # one pinging. Less reliable than inline self-id but better than
    # the proximity heuristic that conflates with prose mentions.
    to_mentions = list(_SENDMESSAGE_TO_RUNNER_RE.finditer(before))
    if to_mentions:
        return f"runner-{to_mentions[-1].group(1)}"

    return "runner-?"


def audit_transcript(transcript: str, cp: Checkpoint) -> AuditReport:
    """Produce an :class:`AuditReport` for a transcript / checkpoint pair.

    ``transcript`` is treated as opaque text. ``cp`` supplies the caps and
    the batch layout against which the transcript is judged. Unknown fields
    (anything the transcript doesn't mention) show up as zeros / empty lists
    — the absence of a signal is itself informative and is preserved.
    """
    fix_counts: dict[str, int] = {}
    fix_numbers: dict[str, list[int]] = {}
    for m in _FIX_ASSIGNMENT_RE.finditer(transcript):
        fix_num = int(m.group(1))
        runner = _attribute_fix_to_runner(transcript, m.start())
        fix_counts[runner] = fix_counts.get(runner, 0) + 1
        fix_numbers.setdefault(runner, []).append(fix_num)

    cap = cp.max_fixes_per_runner
    cap_overruns: list[str] = []
    for runner, count in sorted(fix_counts.items(), key=lambda kv: _runner_sort_key(kv[0])):
        if count > cap:
            if runner == "runner-?":
                cap_overruns.append(
                    f"runner-? (unattributed): {count} fix assignments detected (cap={cap}) "
                    "— attribution failed; check transcript for dense dispatch blocks"
                )
            else:
                cap_overruns.append(
                    f"{runner}: observed {count} fix assignments (cap={cap}) "
                    "— rotation was late or missed"
                )

    # Context-pressure: per-runner first-mention order + raw total count.
    # Uses a separate attribution path (SendMessage envelope-aware) because
    # CONTEXT_PRESSURE pings are self-reports *from* a runner — the message
    # envelope's ``to=`` field points at the advisor, not the runner —
    # whereas fix assignments are dispatches *to* a runner. Reusing
    # ``_attribute_fix_to_runner`` here would attribute the ping to
    # whichever runner the advisor most recently addressed, not the one
    # actually pinging.
    cp_matches = list(_CONTEXT_PRESSURE_RE.finditer(transcript))
    cp_runners_ordered: list[str] = []
    seen_cp: set[str] = set()
    for m in cp_matches:
        runner = _attribute_context_pressure_to_runner(transcript, m.start())
        if runner not in seen_cp:
            seen_cp.add(runner)
            cp_runners_ordered.append(runner)

    rotations = sum(1 for _ in _HANDOFF_RE.finditer(transcript))

    # Cap protocol_violations at PROTOCOL_VIOLATION_CAP entries — a
    # pathological transcript with thousands of matches would otherwise
    # inflate AuditReport memory without adding signal. Mirrors the cap
    # on _history_payload. The ``_truncated`` flag surfaces in the
    # final report so a reader can tell "0 violations" from "we stopped
    # at the cap" — silent truncation hides the very signal the audit
    # exists to highlight.
    protocol_violations: list[str] = []
    protocol_violations_truncated = False
    # Strip fenced code blocks first — a runner quoting the sentinel inside
    # an Evidence ``` block is documenting, not violating. The ^ anchor
    # alone catches column-0 prose mentions but not column-0 lines that sit
    # *inside* a fenced region.
    transcript_unfenced = _strip_fenced_blocks(transcript)
    for m in _PROTOCOL_VIOLATION_RE.finditer(transcript_unfenced):
        if len(protocol_violations) >= PROTOCOL_VIOLATION_CAP:
            protocol_violations_truncated = True
            break
        protocol_violations.append(m.group(0))

    batch_files = _collect_batch_files(cp)
    in_batch: list[Finding]
    out_batch: list[Finding]
    if batch_files:
        in_batch, out_batch = parse_findings_with_drift(transcript, batch_files)
    else:
        # Nothing to compare against — treat all parsed findings as in-batch
        # so the audit doesn't falsely flag drift on a legacy/empty plan.
        in_batch = parse_findings_with_drift(transcript, None)[0]
        out_batch = []

    return AuditReport(
        run_id=cp.run_id,
        max_fixes_per_runner=cp.max_fixes_per_runner,
        large_file_line_threshold=cp.large_file_line_threshold,
        large_file_max_fixes=cp.large_file_max_fixes,
        fix_counts=fix_counts,
        cap_overruns=cap_overruns,
        context_pressure_runners=cp_runners_ordered,
        context_pressure_count=len(cp_matches),
        rotations=rotations,
        protocol_violations=protocol_violations,
        findings_in_batch=in_batch,
        findings_out_of_batch=out_batch,
        batch_file_count=len(batch_files),
        # Defensive copy — caller could otherwise mutate the local
        # ``fix_numbers`` dict after construction and silently change the
        # report's view. AuditReport is intended to be a snapshot.
        fix_numbers=dict(fix_numbers),
        protocol_violations_truncated=protocol_violations_truncated,
    )


def audit_to_dict(report: AuditReport) -> dict[str, object]:
    """Serialize an :class:`AuditReport` to a JSON-friendly dict.

    Findings are flattened to the same shape used by the rest of the CLI
    (``file_path``/``severity``/``description``/``evidence``/``fix``) so
    scripted consumers don't need to handle a second schema.
    """

    def _f(f: Finding) -> dict[str, object]:
        # Include ``rule_id`` so the JSON round-trip via
        # ``_load_findings_from_input`` preserves it. Without this,
        # ``advisor audit --json | advisor ...`` silently drops the
        # field, breaking suppression matching downstream.
        return {
            "file_path": f.file_path,
            "severity": f.severity,
            "description": f.description,
            "evidence": f.evidence,
            "fix": f.fix,
            "rule_id": f.rule_id,
        }

    return {
        "run_id": report.run_id,
        "caps": {
            "max_fixes_per_runner": report.max_fixes_per_runner,
            "large_file_line_threshold": report.large_file_line_threshold,
            "large_file_max_fixes": report.large_file_max_fixes,
        },
        "fix_counts": report.fix_counts,
        "fix_numbers": report.fix_numbers,
        "cap_overruns": report.cap_overruns,
        "context_pressure": {
            "runners": report.context_pressure_runners,
            "total_count": report.context_pressure_count,
        },
        "rotations": report.rotations,
        "protocol_violations": report.protocol_violations,
        "protocol_violations_truncated": report.protocol_violations_truncated,
        "batch_file_count": report.batch_file_count,
        "findings_in_batch": [_f(f) for f in report.findings_in_batch],
        "findings_out_of_batch": [_f(f) for f in report.findings_out_of_batch],
    }


def format_audit_report(report: AuditReport) -> str:
    """Render an :class:`AuditReport` as a human-readable markdown block.

    Empty sections are collapsed to a single ``(none)`` line so the report
    remains scannable even when a run had no violations or drift — the
    absence of red flags is itself a useful signal and should be visible.
    """
    lines: list[str] = []
    lines.append(f"# Audit — run {report.run_id}")
    lines.append("")
    lines.append(
        f"Caps: max_fixes_per_runner={report.max_fixes_per_runner}, "
        f"large_file_line_threshold={report.large_file_line_threshold}, "
        f"large_file_max_fixes={report.large_file_max_fixes}"
    )
    lines.append(f"Batch file universe: {report.batch_file_count} files")
    lines.append("")

    lines.append("## Fix counts per runner")
    if report.fix_counts:
        for runner in sorted(report.fix_counts, key=_runner_sort_key):
            nums = report.fix_numbers.get(runner, [])
            nums_str = f" (fix numbers seen: {', '.join(str(n) for n in nums)})" if nums else ""
            lines.append(f"- {runner}: {report.fix_counts[runner]}{nums_str}")
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Cap overruns")
    if report.cap_overruns:
        for o in report.cap_overruns:
            lines.append(f"- {o}")
    else:
        lines.append("- (none — every runner stayed within cap)")
    lines.append("")

    lines.append("## CONTEXT_PRESSURE pings")
    if report.context_pressure_runners:
        runner_word = "runner" if len(report.context_pressure_runners) == 1 else "runners"
        lines.append(
            f"- total occurrences: {report.context_pressure_count} "
            f"(from {len(report.context_pressure_runners)} {runner_word})"
        )
        for r in report.context_pressure_runners:
            lines.append(f"  - {r}")
    else:
        lines.append("- (none — no runner self-reported saturation)")
    lines.append("")

    lines.append("## Rotations (handoffs)")
    lines.append(f"- count: {report.rotations}")
    lines.append("")

    lines.append("## PROTOCOL_VIOLATION strings")
    if report.protocol_violations:
        for v in report.protocol_violations:
            lines.append(f"- {v}")
        if report.protocol_violations_truncated:
            # Make the silent cap explicit so a reader can tell
            # "1000 violations" from "1000+ violations and we stopped
            # counting" — the latter is itself a finding worth
            # surfacing rather than hiding inside an undocumented limit.
            lines.append(
                f"- … (truncated at {PROTOCOL_VIOLATION_CAP}; "
                f"transcript contains additional matches)"
            )
    else:
        lines.append("- (none)")
    lines.append("")

    lines.append("## Out-of-batch findings (scope drift)")
    if report.findings_out_of_batch:
        for f in report.findings_out_of_batch:
            lines.append(f"- `{f.file_path}` [{f.severity}] — {f.description}")
    else:
        lines.append("- (none — no runner reported on a file outside its batch)")
    lines.append("")

    lines.append(f"## In-batch findings: {len(report.findings_in_batch)}")

    return "\n".join(lines)
