"""Runner budget + scope-anchor tracking — live drift + exhaustion signals.

Three cheap signals layered over the existing heartbeat / CONTEXT_PRESSURE
protocol:

1. **Scope anchor** — every runner reply opens with one line
   ``SCOPE: <file_path> · <stage>``. The stage is one of
   :data:`SCOPE_STAGES`. Drift is deterministic: the advisor compares
   the anchored file against the runner's batch; a file that isn't in
   the batch means scope drift, a stage regression (``done`` → anything
   else on a new file) also rings the bell.

2. **Output-size budget** — the advisor already sees every runner
   message, so it can track cumulative output bytes cheaply. Soft nudge
   at :data:`SOFT_WARN_FRACTION` (default 60% of the ceiling), hard
   rotate at :data:`ROTATE_FRACTION` (default 80%). Objective — no
   runner introspection needed.

3. **Hard ceiling safety net** — whichever of ``byte_ceiling``,
   ``file_read_ceiling``, or ``fix_ceiling`` trips first, the advisor
   rotates without waiting for a ``CONTEXT_PRESSURE`` ping. Current
   ``max_fixes_per_runner`` was the fix-only ceiling; this module
   generalizes to all three axes.

Everything here is pure functions and frozen dataclasses — no I/O, no
mutation. Callers use the returned new budget object.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Final

SCOPE_STAGES: Final[tuple[str, ...]] = (
    "reading",
    "hypothesizing",
    "confirming",
    "fixing",
    "done",
)

# Ordered so a regression is a strict ``<`` comparison on the index.
# ``fixing`` sits after ``confirming`` because fix assignments follow
# confirmed findings — dropping back from ``fixing`` to ``reading`` a
# different file signals drift (explore creeping into a fix assignment).
_STAGE_INDEX: Final[dict[str, int]] = {s: i for i, s in enumerate(SCOPE_STAGES)}

SOFT_WARN_FRACTION: Final[float] = 0.60
ROTATE_FRACTION: Final[float] = 0.80

# Defaults are the middle of the observed healthy range for Sonnet
# subagents in this repo. Configurable via
# :class:`~advisor.orchestrate.TeamConfig` fields.
DEFAULT_BYTE_CEILING: Final[int] = 80_000
DEFAULT_FILE_READ_CEILING: Final[int] = 20

# ``SCOPE: <file> · <stage>`` anchor. Bullet separator tolerates the
# Unicode middle-dot ``·`` and plain ``|`` / ``-`` so runners that
# autocorrect the punctuation still parse.
_SCOPE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*SCOPE\s*:\s*(?P<file>[^\n·|\-]+?)\s*(?:·|\||-)\s*(?P<stage>\w+)",
    re.IGNORECASE | re.MULTILINE,
)


@dataclass(frozen=True, slots=True)
class ScopeAnchor:
    """A parsed ``SCOPE:`` header from a runner reply."""

    file_path: str
    stage: str  # one of SCOPE_STAGES — unknown stages are kept as-is so callers can detect drift


@dataclass(frozen=True, slots=True)
class RunnerBudget:
    """Advisor-side bookkeeping for a single runner's context spend.

    All fields are monotonic (they only grow during a session). Use
    :func:`update_budget` to produce a new budget after each runner
    turn — never mutate in place.

    Attributes:
        runner_id: Pool identifier, e.g. ``"runner-2"``.
        output_bytes: Cumulative char count of runner-produced replies.
        files_read: Distinct files the runner has reported reading.
            Populated from the ``SCOPE:`` anchor's file field.
        fixes_done: Completed fix assignments.
        byte_ceiling: Hard rotate threshold on output bytes.
        file_read_ceiling: Hard rotate threshold on distinct file reads.
        fix_ceiling: Hard rotate threshold on fix assignments
            (wired through from :class:`TeamConfig.max_fixes_per_runner`).
        soft_warned: Set by :func:`update_budget` the first time bytes
            cross :data:`SOFT_WARN_FRACTION`. Prevents the advisor from
            re-nudging every turn after the threshold.
        last_stage: The most recent parsed scope stage, if any. Used to
            detect regressions (``done`` → ``reading`` of a new file).
        last_file: The most recent parsed scope file. Used for drift
            detection against the runner's assigned batch.
    """

    runner_id: str
    byte_ceiling: int = DEFAULT_BYTE_CEILING
    file_read_ceiling: int = DEFAULT_FILE_READ_CEILING
    fix_ceiling: int = 5
    output_bytes: int = 0
    files_read: tuple[str, ...] = ()
    fixes_done: int = 0
    soft_warned: bool = False
    last_stage: str | None = None
    last_file: str | None = None


def new_budget(
    runner_id: str,
    *,
    byte_ceiling: int = DEFAULT_BYTE_CEILING,
    file_read_ceiling: int = DEFAULT_FILE_READ_CEILING,
    fix_ceiling: int = 5,
) -> RunnerBudget:
    """Construct a fresh zeroed :class:`RunnerBudget`."""
    if byte_ceiling <= 0:
        raise ValueError("byte_ceiling must be > 0")
    if file_read_ceiling <= 0:
        raise ValueError("file_read_ceiling must be > 0")
    if fix_ceiling <= 0:
        raise ValueError("fix_ceiling must be > 0")
    return RunnerBudget(
        runner_id=runner_id,
        byte_ceiling=byte_ceiling,
        file_read_ceiling=file_read_ceiling,
        fix_ceiling=fix_ceiling,
    )


def parse_scope_anchor(text: str) -> ScopeAnchor | None:
    """Return the first ``SCOPE: <file> · <stage>`` anchor in ``text``.

    Returns ``None`` when the header is missing — absence itself is a
    signal the caller can act on (soft-remind the runner). Only the
    *first* anchor on a reply is considered authoritative; callers that
    care about every file the runner touched should consume the
    per-message file list another way.
    """
    m = _SCOPE_RE.search(text)
    if m is None:
        return None
    return ScopeAnchor(
        file_path=m.group("file").strip().strip("`"),
        stage=m.group("stage").strip().lower(),
    )


def update_budget(
    budget: RunnerBudget,
    *,
    message_text: str,
    fix_completed: bool = False,
) -> RunnerBudget:
    """Produce a new :class:`RunnerBudget` reflecting ``message_text``.

    ``fix_completed`` should be set by the caller when the runner's
    message confirms a completed fix — the fix counter is not parsed
    from the message body because the signal already flows through
    :func:`build_fix_assignment_message` on the dispatch side.
    """
    anchor = parse_scope_anchor(message_text)
    new_files = budget.files_read
    if anchor and anchor.file_path and anchor.file_path not in budget.files_read:
        new_files = (*budget.files_read, anchor.file_path)

    new_bytes = budget.output_bytes + len(message_text)
    soft_threshold = int(budget.byte_ceiling * SOFT_WARN_FRACTION)
    soft = budget.soft_warned or new_bytes >= soft_threshold

    return replace(
        budget,
        output_bytes=new_bytes,
        files_read=new_files,
        fixes_done=budget.fixes_done + (1 if fix_completed else 0),
        soft_warned=soft,
        last_stage=anchor.stage if anchor else budget.last_stage,
        last_file=anchor.file_path if anchor else budget.last_file,
    )


def budget_status(budget: RunnerBudget) -> str:
    """Return ``'OK'`` / ``'SOFT_WARN'`` / ``'ROTATE'`` for ``budget``.

    Precedence: any hard-ceiling trip → ``ROTATE``. Otherwise, bytes
    above :data:`SOFT_WARN_FRACTION` → ``SOFT_WARN``. Otherwise ``OK``.
    """
    rotate_bytes = int(budget.byte_ceiling * ROTATE_FRACTION)
    if (
        budget.output_bytes >= rotate_bytes
        or len(budget.files_read) >= budget.file_read_ceiling
        or budget.fixes_done >= budget.fix_ceiling
    ):
        return "ROTATE"
    soft_bytes = int(budget.byte_ceiling * SOFT_WARN_FRACTION)
    if budget.output_bytes >= soft_bytes:
        return "SOFT_WARN"
    return "OK"


def stage_regressed(prev: str | None, current: str | None) -> bool:
    """True when ``current`` is an earlier stage than ``prev``.

    Unknown stages do not trigger a regression — the advisor should
    take unknown tokens as a lint warning (runner might have invented a
    label) rather than a drift claim.
    """
    if prev is None or current is None:
        return False
    if prev not in _STAGE_INDEX or current not in _STAGE_INDEX:
        return False
    return _STAGE_INDEX[current] < _STAGE_INDEX[prev]


def out_of_batch(anchor: ScopeAnchor | None, batch_files: set[str]) -> bool:
    """True when ``anchor.file_path`` is non-empty and not in ``batch_files``.

    ``batch_files`` is expected to be the set of repo-relative POSIX
    paths assigned to the runner's batch (mirroring the scope-drift
    filter in :func:`advisor.verify.parse_findings_with_drift`). An
    empty / missing anchor returns ``False`` — no file claim is neither
    drifting nor confirming.
    """
    if anchor is None or not anchor.file_path:
        return False
    return _normalize(anchor.file_path) not in {_normalize(f) for f in batch_files}


def _normalize(path: str) -> str:
    """Shared normalizer matching ``verify._normalize_path``."""
    p = path.strip().strip("`").replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


def format_budget_nudge(budget: RunnerBudget) -> str | None:
    """Short one-line nudge for ``SendMessage(to=runner)``, or ``None``.

    ``None`` when the budget is healthy. ``SOFT_WARN`` returns a
    compaction hint; ``ROTATE`` returns a hard stop directive.
    """
    status = budget_status(budget)
    if status == "OK":
        return None
    if status == "SOFT_WARN":
        return (
            f"BUDGET SOFT — {budget.output_bytes}/{budget.byte_ceiling} bytes used. "
            "Compact your next reply: one primary finding, skip recaps, "
            "then confirm you are still under budget."
        )
    return (
        f"BUDGET ROTATE — {budget.runner_id} has crossed the hard ceiling "
        f"(bytes {budget.output_bytes}/{budget.byte_ceiling}, "
        f"files {len(budget.files_read)}/{budget.file_read_ceiling}, "
        f"fixes {budget.fixes_done}/{budget.fix_ceiling}). "
        "Finish the current tool call, emit a one-paragraph handoff "
        "brief, then wait for shutdown_request."
    )
