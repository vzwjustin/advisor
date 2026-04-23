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
# plus a little slack, without reaching across unrelated blocks.
_RUNNER_ATTRIBUTION_WINDOW = 500

_RUNNER_MENTION_RE = re.compile(r"runner-(\d+)")

_CONTEXT_PRESSURE_RE = re.compile(r"CONTEXT_PRESSURE", re.IGNORECASE)

_PROTOCOL_VIOLATION_RE = re.compile(
    r"PROTOCOL_VIOLATION\s*:\s*[^\n]*",
)

_HANDOFF_RE = re.compile(r"##\s+Handoff\s+from\s+runner-\d+")


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
            strings found, in order.
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
    """Return the ``runner-N`` id nearest (before) the fix assignment.

    Scans backwards within ``_RUNNER_ATTRIBUTION_WINDOW`` characters from
    ``match_start`` for the last ``runner-N`` mention. Returns
    ``"runner-?"`` when no mention is found — surfacing the ambiguity
    rather than silently dropping the fix.
    """
    window_start = max(0, match_start - _RUNNER_ATTRIBUTION_WINDOW)
    window = transcript[window_start:match_start]
    mentions = list(_RUNNER_MENTION_RE.finditer(window))
    if not mentions:
        return "runner-?"
    return f"runner-{mentions[-1].group(1)}"


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
    for runner, count in sorted(fix_counts.items()):
        if count > cap:
            cap_overruns.append(
                f"{runner}: observed {count} fix assignments (cap={cap}) — rotation was late or missed"
            )

    # Context-pressure: per-runner first-mention order + raw total count.
    cp_matches = list(_CONTEXT_PRESSURE_RE.finditer(transcript))
    cp_runners_ordered: list[str] = []
    seen_cp: set[str] = set()
    for m in cp_matches:
        runner = _attribute_fix_to_runner(transcript, m.start())
        if runner not in seen_cp:
            seen_cp.add(runner)
            cp_runners_ordered.append(runner)

    rotations = sum(1 for _ in _HANDOFF_RE.finditer(transcript))

    # Cap protocol_violations at 1000 entries — a pathological transcript
    # with thousands of matches would otherwise inflate AuditReport memory
    # without adding signal. Mirrors the cap on _history_payload.
    protocol_violations: list[str] = []
    for m in _PROTOCOL_VIOLATION_RE.finditer(transcript):
        if len(protocol_violations) >= 1000:
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
    )


def audit_to_dict(report: AuditReport) -> dict[str, object]:
    """Serialize an :class:`AuditReport` to a JSON-friendly dict.

    Findings are flattened to the same shape used by the rest of the CLI
    (``file_path``/``severity``/``description``/``evidence``/``fix``) so
    scripted consumers don't need to handle a second schema.
    """

    def _f(f: Finding) -> dict[str, str]:
        return {
            "file_path": f.file_path,
            "severity": f.severity,
            "description": f.description,
            "evidence": f.evidence,
            "fix": f.fix,
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
        for runner in sorted(report.fix_counts):
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
