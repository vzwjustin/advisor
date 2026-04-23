"""File-level focus dispatcher — one batch of files per runner.

Split review work across parallel agents. The advisor
(Opus) decides batch sizes dynamically — a single runner can handle one hot
file or dozens of low-risk utilities, depending on complexity. Batching is
driven by the advisor, not hard-coded caps.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .rank import RankedFile


@dataclass(frozen=True, slots=True)
class FocusTask:
    """A single-file task with advisor guidance attached."""

    file_path: str
    priority: int
    prompt: str


@dataclass(frozen=True, slots=True)
class FocusBatch:
    """A bundle of files for a single runner.

    The advisor sizes each batch based on file complexity. A hot, dense file
    may be alone in its batch; dozens of small utilities may share one.
    """

    batch_id: int
    tasks: tuple[FocusTask, ...]
    complexity: str  # "low" | "medium" | "high" — advisor's assessment

    @property
    def file_paths(self) -> tuple[str, ...]:
        return tuple(t.file_path for t in self.tasks)

    @property
    def top_priority(self) -> int:
        return max((t.priority for t in self.tasks), default=0)


DEFAULT_TASK_PROMPT = (
    "You are reviewing a single file for issues. Focus exclusively on:\n"
    "  `{file_path}` (priority {priority})\n\n"
    "Relevant signals: {reasons}\n\n"
    "Instructions:\n"
    "1. Read the file thoroughly.\n"
    "2. Hypothesize potential issues (bugs, security flaws, logic errors).\n"
    "3. Trace call paths to confirm or reject each hypothesis.\n"
    "4. For each confirmed issue, output:\n"
    "   - **File**: path and line number\n"
    "   - **Severity**: CRITICAL / HIGH / MEDIUM / LOW\n"
    "   - **Description**: what the issue is\n"
    "   - **Evidence**: the code path or proof\n"
    "   - **Fix**: suggested remediation\n"
    "5. If no issues found, state that explicitly.\n\n"
    "Do NOT review other files. Stay focused on this one."
)

# Single-pass placeholder substitution. Using a chain of ``str.replace`` on
# each field would be order-dependent: if a substituted value (e.g. a
# malicious ``file_path``) contained a literal ``{reasons}`` token, a later
# pass would then rewrite it. ``re.sub`` with a single combined pattern
# replaces each placeholder exactly once from its own slot.
_PLACEHOLDER_RE = re.compile(r"\{(file_path|priority|reasons)\}")


def _render_task_prompt(template: str, mapping: dict[str, str]) -> str:
    """Fill ``{file_path}``/``{priority}``/``{reasons}`` in one pass.

    Unknown placeholders are left intact so custom templates can use
    literal braces (e.g. JSON examples) without escaping.
    """
    return _PLACEHOLDER_RE.sub(lambda m: mapping.get(m.group(1), m.group(0)), template)


def create_focus_tasks(
    ranked_files: list[RankedFile],
    max_tasks: int | None = None,
    min_priority: int = 2,
    prompt_template: str = DEFAULT_TASK_PROMPT,
) -> list[FocusTask]:
    """Generate one FocusTask per ranked file at or above `min_priority`.

    Args:
        ranked_files: Output of rank.rank_files(), sorted by priority
                      descending. Once priority drops below ``min_priority``
                      all remaining files are also below — the loop exits
                      early.
        max_tasks: Optional soft cap on task count. `None` means no cap —
                   let the advisor decide how many files to review.
        min_priority: Skip files below this priority.
        prompt_template: Template with {file_path}, {priority}, {reasons}.

    Returns:
        A new list of FocusTask objects ready for batching.
    """
    tasks: list[FocusTask] = []

    for rf in ranked_files:
        if max_tasks is not None and len(tasks) >= max_tasks:
            break
        if rf.priority < min_priority:
            break

        reasons_str = ", ".join(rf.reasons) if rf.reasons else "general review"
        prompt = _render_task_prompt(
            prompt_template,
            {
                "file_path": rf.path,
                "priority": str(rf.priority),
                "reasons": reasons_str,
            },
        )
        tasks.append(
            FocusTask(
                file_path=rf.path,
                priority=rf.priority,
                prompt=prompt,
            )
        )

    return tasks


# Marker for ``create_focus_batches(complexity=...)`` that tells the
# builder to auto-derive per-batch complexity from the top priority of
# the tasks it contains. Exposed as a module constant rather than a
# sentinel string so callers can reference it unambiguously.
AUTO_COMPLEXITY = "auto"


def _complexity_for_priority(top_priority: int) -> str:
    """Map a top-priority tier to a default complexity label.

    Covers the offline ``advisor plan --batch-size N`` path — in the
    live pipeline the advisor overrides this based on reading the
    files. Simple heuristic: P1–P2 is ``low``, P3 is ``medium``,
    P4–P5 is ``high``. Keeps the plan's batch-header informative
    instead of the previous flat ``medium`` for everything.
    """
    if top_priority >= 4:
        return "high"
    if top_priority <= 2:
        return "low"
    return "medium"


def create_focus_batches(
    tasks: list[FocusTask],
    files_per_batch: int = 5,
    complexity: str = AUTO_COMPLEXITY,
) -> list[FocusBatch]:
    """Group FocusTasks into batches for parallel runner dispatch.

    This is a mechanical fallback used by the CLI `plan` command. In the live
    pipeline the Opus advisor decides batch composition and complexity per
    batch based on its own reading of each file. No hard upper cap — `files_per_batch`
    is a simple grouping knob, not a ceiling on what a runner can handle.

    When ``complexity == "auto"`` (the default), each batch's complexity
    is derived from the top priority of its tasks via
    :func:`_complexity_for_priority`. Passing any other string forces all
    batches to that exact value — preserves the previous fixed-label
    behavior for callers that want it.
    """
    if files_per_batch < 1:
        raise ValueError("files_per_batch must be >= 1")

    batches: list[FocusBatch] = []
    for i in range(0, len(tasks), files_per_batch):
        chunk = tuple(tasks[i : i + files_per_batch])
        if complexity == AUTO_COMPLEXITY:
            top = max((t.priority for t in chunk), default=1)
            batch_complexity = _complexity_for_priority(top)
        else:
            batch_complexity = complexity
        batches.append(
            FocusBatch(
                batch_id=len(batches) + 1,
                tasks=chunk,
                complexity=batch_complexity,
            )
        )
    return batches


def _priority_mix(priorities: list[int]) -> str:
    """Compact priority histogram — e.g. ``P5 ×11  P4 ×5  P3 ×5``.

    Rendered inline on the dispatch-plan header so a reader in
    Claude Code gets an at-a-glance sense of the work shape before
    scrolling the full list. The bare ``P<n>`` tokens are picked up
    by :func:`advisor._style.colorize_markdown` and rendered with the
    same priority palette as the numbered list below.
    """
    if not priorities:
        return ""
    counts = Counter(priorities)
    return "  ".join(f"P{p} ×{counts[p]}" for p in sorted(counts, reverse=True))


def format_dispatch_plan(tasks: list[FocusTask]) -> str:
    """Format tasks into a readable dispatch plan."""
    if not tasks:
        return "## Dispatch Plan\nNo files matched — nothing to dispatch.\n"

    # Singular/plural grammar for a single-file plan — "1 focused agent"
    # reads cleaner than "1 focused agents" in the CLI and in README
    # screenshots. The full-word substring "focused agent" remains a
    # subset of "focused agents", so assertion-based tests stay green.
    agent_word = "agent" if len(tasks) == 1 else "agents"
    mix = _priority_mix([t.priority for t in tasks])
    header = f"Dispatching {len(tasks)} focused {agent_word} in parallel"
    if mix:
        header = f"{header} ({mix}):"
    else:
        header = f"{header}:"
    lines = [
        "## Dispatch Plan",
        header,
        "",
    ]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. **P{t.priority}** `{t.file_path}`")

    return "\n".join(lines).rstrip() + "\n"


def format_batch_plan(batches: list[FocusBatch]) -> str:
    """Format batches into a readable dispatch plan."""
    if not batches:
        return "## Batch Dispatch Plan\nNo files matched — nothing to dispatch.\n"

    total_files = sum(len(b.tasks) for b in batches)
    runner_word = "runner" if len(batches) == 1 else "runners"
    file_word = "file" if total_files == 1 else "files"
    mix = _priority_mix([t.priority for b in batches for t in b.tasks])
    header = f"Dispatching {len(batches)} {runner_word} across {total_files} {file_word}"
    if mix:
        header = f"{header} ({mix}):"
    else:
        header = f"{header}:"
    lines = [
        "## Batch Dispatch Plan",
        header,
        "",
    ]
    for b in batches:
        batch_file_word = "file" if len(b.tasks) == 1 else "files"
        lines.append(
            f"**Batch {b.batch_id}** (complexity: {b.complexity}, "
            f"top P{b.top_priority}) — {len(b.tasks)} {batch_file_word}:"
        )
        for t in b.tasks:
            lines.append(f"  - **P{t.priority}** `{t.file_path}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
