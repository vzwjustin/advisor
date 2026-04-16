"""File-level focus dispatcher — one batch of files per runner.

Glasswing technique #1: split review work across parallel agents. The advisor
(Opus) decides batch sizes dynamically — a single runner can handle one hot
file or dozens of low-risk utilities, depending on complexity. Batching is
driven by the advisor, not hard-coded caps.
"""

from __future__ import annotations

from dataclasses import dataclass

from .rank import RankedFile


@dataclass(frozen=True)
class FocusTask:
    """A single-file task with advisor guidance attached."""
    file_path: str
    priority: int
    prompt: str


@dataclass(frozen=True)
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


def create_focus_tasks(
    ranked_files: list[RankedFile],
    max_tasks: int | None = None,
    min_priority: int = 2,
    prompt_template: str = DEFAULT_TASK_PROMPT,
) -> list[FocusTask]:
    """Generate one FocusTask per ranked file above `min_priority`.

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

        prompt = prompt_template.format(
            file_path=rf.path,
            priority=rf.priority,
            reasons=", ".join(rf.reasons) if rf.reasons else "general review",
        )
        tasks.append(FocusTask(
            file_path=rf.path,
            priority=rf.priority,
            prompt=prompt,
        ))

    return tasks


def create_focus_batches(
    tasks: list[FocusTask],
    files_per_batch: int = 5,
    complexity: str = "medium",
) -> list[FocusBatch]:
    """Group FocusTasks into batches for parallel runner dispatch.

    This is a mechanical fallback used by the CLI `plan` command. In the live
    pipeline the Opus advisor decides batch composition and complexity per
    batch based on its own reading of each file. No hard upper cap — `files_per_batch`
    is a simple grouping knob, not a ceiling on what a runner can handle.
    """
    if files_per_batch < 1:
        raise ValueError("files_per_batch must be >= 1")

    batches: list[FocusBatch] = []
    for i in range(0, len(tasks), files_per_batch):
        chunk = tuple(tasks[i : i + files_per_batch])
        batches.append(FocusBatch(
            batch_id=len(batches) + 1,
            tasks=chunk,
            complexity=complexity,
        ))
    return batches


def format_dispatch_plan(tasks: list[FocusTask]) -> str:
    """Format tasks into a readable dispatch plan."""
    lines = [
        "## Dispatch Plan",
        f"Dispatching {len(tasks)} focused agents in parallel:",
        "",
    ]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. **P{t.priority}** `{t.file_path}`")

    return "\n".join(lines).rstrip() + "\n"


def format_batch_plan(batches: list[FocusBatch]) -> str:
    """Format batches into a readable dispatch plan."""
    total_files = sum(len(b.tasks) for b in batches)
    lines = [
        "## Batch Dispatch Plan",
        f"Dispatching {len(batches)} runners across {total_files} files:",
        "",
    ]
    for b in batches:
        lines.append(
            f"**Batch {b.batch_id}** (complexity: {b.complexity}, "
            f"top P{b.top_priority}) — {len(b.tasks)} file(s):"
        )
        for t in b.tasks:
            lines.append(f"  - P{t.priority} `{t.file_path}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
