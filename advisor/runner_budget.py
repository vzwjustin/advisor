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
   message, so it can track cumulative output characters cheaply. Soft
   nudge at :data:`SOFT_WARN_FRACTION` (default 60% of the ceiling),
   hard rotate at :data:`ROTATE_FRACTION` (default 80%). Objective —
   no runner introspection needed. The ceiling is measured in
   *characters* (``len(str)``), which is the closest cheap proxy for
   tokens; despite what Python's name might suggest, it is not
   byte-accurate for non-ASCII text.

3. **Hard ceiling safety net** — whichever of ``char_ceiling``,
   ``file_read_ceiling``, or ``fix_ceiling`` trips first, the advisor
   rotates without waiting for a ``CONTEXT_PRESSURE`` ping. Current
   ``max_fixes_per_runner`` was the fix-only ceiling; this module
   generalizes to all three axes.

Everything here is pure functions and frozen dataclasses — no I/O, no
mutation. Callers use the returned new budget object.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from typing import Final

from ._fs import normalize_path as _normalize

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
DEFAULT_CHAR_CEILING: Final[int] = 80_000
DEFAULT_FILE_READ_CEILING: Final[int] = 20

# ``SCOPE: <file> · <stage>`` anchor. Separator must be surrounded by
# whitespace so hyphens inside filenames (``src/my-file.py``) don't get
# mistaken for the delimiter. Accepts middle-dot ``·``, pipe ``|``, or
# hyphen ``-`` so runners that autocorrect punctuation still parse.
#
# The trailing ``\s*$`` (in multi-line mode) anchors ``stage`` to the
# actual line end. This forces the regex engine to consume up to the
# LAST separator on the line — a path that legitimately contains the
# separator pattern (e.g. ``src/foo · bar.py``) lands in ``<file>``
# rather than getting split at the first ``·``. Without the anchor,
# the non-greedy ``[^\n]*?`` would pick the first match and silently
# parse the stage as ``bar`` instead of ``reading``.
_SCOPE_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*SCOPE\s*:\s*(?P<file>\S[^\n]*?)\s+[·|\-]\s+(?P<stage>\w+)\s*$",
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
        output_chars: Cumulative character count of runner-produced
            replies (``sum(len(msg) for msg)``). Character count is the
            cheap proxy for token spend — it is not a byte count.
        files_read: Distinct files the runner has reported reading.
            Populated from the ``SCOPE:`` anchor's file field.
        fixes_done: Completed fix assignments.
        char_ceiling: Hard rotate threshold on output characters.
        file_read_ceiling: Hard rotate threshold on distinct file reads.
        fix_ceiling: Hard rotate threshold on fix assignments
            (wired through from :class:`TeamConfig.max_fixes_per_runner`).
        soft_nudge_sent: Flipped to True by
            :func:`format_budget_nudge` the first time it emits a
            BUDGET SOFT message. Prevents repeated soft nudges while
            the budget stays in the SOFT_WARN region.
        rotate_nudge_sent: Same, for the ROTATE threshold.
        last_stage: The most recent parsed scope stage, if any. Used to
            detect regressions (``done`` → ``reading`` of a new file).
        last_file: The most recent parsed scope file. Used for drift
            detection against the runner's assigned batch.
    """

    runner_id: str
    char_ceiling: int = DEFAULT_CHAR_CEILING
    file_read_ceiling: int = DEFAULT_FILE_READ_CEILING
    fix_ceiling: int = 5
    output_chars: int = 0
    files_read: tuple[str, ...] = ()
    fixes_done: int = 0
    soft_nudge_sent: bool = False
    rotate_nudge_sent: bool = False
    last_stage: str | None = None
    last_file: str | None = None


def new_budget(
    runner_id: str,
    *,
    char_ceiling: int = DEFAULT_CHAR_CEILING,
    file_read_ceiling: int = DEFAULT_FILE_READ_CEILING,
    fix_ceiling: int = 5,
) -> RunnerBudget:
    """Construct a fresh zeroed :class:`RunnerBudget`."""
    if char_ceiling <= 0:
        raise ValueError("char_ceiling must be > 0")
    if file_read_ceiling <= 0:
        raise ValueError("file_read_ceiling must be > 0")
    # ``fix_ceiling = 0`` is the explore-only configuration — runners read
    # files but never apply fixes. Mirrors ``cost.estimate_cost`` which
    # explicitly accepts ``max_fixes_per_runner = 0``. Reject only true
    # negatives.
    if fix_ceiling < 0:
        raise ValueError("fix_ceiling must be >= 0")
    return RunnerBudget(
        runner_id=runner_id,
        char_ceiling=char_ceiling,
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
    file_read: str | None = None,
) -> RunnerBudget:
    """Produce a new :class:`RunnerBudget` reflecting ``message_text``.

    ``fix_completed`` should be set by the caller when the runner's
    message confirms a completed fix — the fix counter is not parsed
    from the message body because the signal already flows through
    :func:`build_fix_assignment_message` on the dispatch side.

    ``file_read`` lets the caller register a file-read event even when
    the runner's reply omits the ``SCOPE:`` anchor. Without this, the
    file-axis ROTATE guard silently under-counts: a runner that reads
    a file but forgets to emit the anchor contributes nothing to
    ``files_read`` and can exceed ``file_read_ceiling`` without
    triggering rotation. Callers that observe a file-read out-of-band
    (e.g. via Read tool call inspection) should pass it explicitly.

    This only accumulates state — it does NOT decide whether to emit a
    nudge. :func:`format_budget_nudge` inspects the resulting budget
    and gates on the ``_nudge_sent`` flags to prevent duplicate nudges.
    """
    anchor = parse_scope_anchor(message_text)
    new_files = budget.files_read
    anchor_path = _normalize(anchor.file_path) if anchor and anchor.file_path else None
    if anchor_path and anchor_path not in new_files:
        new_files = (*new_files, anchor_path)
    file_read_path = _normalize(file_read) if file_read else None
    if file_read_path and file_read_path not in new_files:
        new_files = (*new_files, file_read_path)

    return replace(
        budget,
        output_chars=budget.output_chars + len(message_text),
        files_read=new_files,
        fixes_done=budget.fixes_done + (1 if fix_completed else 0),
        last_stage=anchor.stage if anchor else budget.last_stage,
        last_file=anchor_path if anchor_path else budget.last_file,
    )


def budget_status(budget: RunnerBudget) -> str:
    """Return ``'OK'`` / ``'SOFT_WARN'`` / ``'ROTATE'`` for ``budget``.

    Precedence: any hard-ceiling trip → ``ROTATE``. Otherwise, chars
    above :data:`SOFT_WARN_FRACTION` → ``SOFT_WARN``. Otherwise ``OK``.
    """
    rotate_chars = int(budget.char_ceiling * ROTATE_FRACTION)
    if (
        budget.output_chars >= rotate_chars
        or len(budget.files_read) >= budget.file_read_ceiling
        or budget.fixes_done >= budget.fix_ceiling
    ):
        return "ROTATE"
    soft_chars = int(budget.char_ceiling * SOFT_WARN_FRACTION)
    if budget.output_chars >= soft_chars:
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


def normalize_batch_files(paths: Iterable[str]) -> frozenset[str]:
    """Return a pre-normalized, hashable batch set for :func:`out_of_batch`.

    Pre-normalize once outside the per-turn loop to avoid
    re-normalizing on every call. :func:`out_of_batch` fast-paths
    :class:`frozenset` inputs — pass the result of this function to
    keep the hot path O(1).
    """
    return frozenset(_normalize(p) for p in paths)


def out_of_batch(anchor: ScopeAnchor | None, batch_files: Iterable[str]) -> bool:
    """True when ``anchor.file_path`` is non-empty and not in ``batch_files``.

    ``batch_files`` may be a plain set/list/tuple (normalized on the
    fly, O(N) per call) or a :class:`frozenset` from
    :func:`normalize_batch_files` (assumed pre-normalized, O(1)
    lookup). The ``frozenset`` fast-path keeps per-turn cost flat for
    large batches.

    An empty / missing anchor returns ``False`` — no file claim is
    neither drifting nor confirming.
    """
    if anchor is None or not anchor.file_path:
        return False
    key = _normalize(anchor.file_path)
    if isinstance(batch_files, frozenset):
        return key not in batch_files
    return key not in {_normalize(f) for f in batch_files}


def format_budget_nudge(budget: RunnerBudget) -> tuple[str | None, RunnerBudget]:
    """Return ``(nudge_to_send, new_budget)``.

    A nudge fires **exactly once per threshold crossing**. Callers must
    adopt the returned budget — that's the signal that the nudge has
    been emitted, so subsequent calls while the budget stays in the
    same SOFT_WARN / ROTATE region return ``(None, budget)``. If the
    caller ignores the returned budget, the gate will re-fire. That's
    intentional: adoption is the commit; not adopting is the rollback.

    ``budget_status`` takes precedence — if a ROTATE trip also has
    ``soft_nudge_sent=False``, the ROTATE nudge fires first and the
    soft flag is *not* set, so the caller can still see a SOFT_WARN
    later if the budget state somehow regresses (which shouldn't happen
    with monotonic state, but keeps the flags orthogonal).
    """
    status = budget_status(budget)
    if status == "OK":
        return None, budget
    if status == "SOFT_WARN":
        if budget.soft_nudge_sent:
            return None, budget
        msg = (
            f"BUDGET SOFT — {budget.output_chars}/{budget.char_ceiling} chars used. "
            "Compact your next reply: one primary finding, skip recaps, "
            "then confirm you are still under budget."
        )
        return msg, replace(budget, soft_nudge_sent=True)
    # ROTATE
    if budget.rotate_nudge_sent:
        return None, budget
    msg = (
        f"BUDGET ROTATE — {budget.runner_id} has crossed the hard ceiling "
        f"(chars {budget.output_chars}/{budget.char_ceiling}, "
        f"files {len(budget.files_read)}/{budget.file_read_ceiling}, "
        f"fixes {budget.fixes_done}/{budget.fix_ceiling}). "
        "Finish the current tool call, emit a one-paragraph handoff "
        "brief, then wait for shutdown_request."
    )
    return msg, replace(budget, rotate_nudge_sent=True)
