"""File-level focus dispatcher — one agent per file.

Glasswing technique #1: instead of asking one agent to review everything,
split work so each agent focuses on a single file. This increases diversity
of findings and enables parallel execution.
"""

from dataclasses import dataclass

from .rank import RankedFile


@dataclass(frozen=True)
class FocusTask:
    """A single-file task ready for agent dispatch."""
    file_path: str
    priority: int
    prompt: str


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
    max_tasks: int = 10,
    min_priority: int = 2,
    prompt_template: str = DEFAULT_TASK_PROMPT,
) -> list[FocusTask]:
    """Generate one FocusTask per file from a ranked list.

    Args:
        ranked_files: Output of rank.rank_files(), sorted by priority.
        max_tasks: Maximum number of tasks to generate.
        min_priority: Skip files below this priority.
        prompt_template: Template with {file_path}, {priority}, {reasons}.

    Returns:
        List of FocusTask objects ready for agent dispatch.
    """
    tasks: list[FocusTask] = []

    for rf in ranked_files:
        if len(tasks) >= max_tasks:
            break
        if rf.priority < min_priority:
            continue

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


def format_dispatch_plan(tasks: list[FocusTask]) -> str:
    """Format tasks into a readable dispatch plan.

    Args:
        tasks: Output of create_focus_tasks().

    Returns:
        Markdown summary of the dispatch plan.
    """
    lines = [
        "## Dispatch Plan",
        f"Dispatching {len(tasks)} focused agents in parallel:",
        "",
    ]
    for i, t in enumerate(tasks, 1):
        lines.append(f"{i}. **P{t.priority}** `{t.file_path}`")

    return "\n".join(lines)
