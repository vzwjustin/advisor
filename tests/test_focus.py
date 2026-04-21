"""Tests for advisor.focus module."""

import pytest

from advisor.focus import (
    FocusTask,
    create_focus_batches,
    create_focus_tasks,
    format_batch_plan,
    format_dispatch_plan,
)
from advisor.rank import RankedFile


class TestCreateFocusTasks:
    def test_creates_one_task_per_file(self):
        ranked = [
            RankedFile(path="src/auth.py", priority=5, reasons=("auth",)),
            RankedFile(path="src/api.py", priority=3, reasons=("api",)),
        ]
        tasks = create_focus_tasks(ranked)

        assert len(tasks) == 2
        assert tasks[0].file_path == "src/auth.py"
        assert tasks[1].file_path == "src/api.py"

    def test_respects_max_tasks(self):
        ranked = [RankedFile(path=f"src/f{i}.py", priority=5, reasons=("auth",)) for i in range(20)]
        tasks = create_focus_tasks(ranked, max_tasks=3)

        assert len(tasks) == 3

    def test_no_max_tasks_default(self):
        ranked = [RankedFile(path=f"src/f{i}.py", priority=5, reasons=("auth",)) for i in range(50)]
        tasks = create_focus_tasks(ranked)
        assert len(tasks) == 50  # no hard cap by default

    def test_respects_min_priority(self):
        ranked = [
            RankedFile(path="src/auth.py", priority=5, reasons=("auth",)),
            RankedFile(path="src/util.py", priority=1, reasons=("util",)),
        ]
        tasks = create_focus_tasks(ranked, min_priority=3)

        assert len(tasks) == 1
        assert tasks[0].file_path == "src/auth.py"

    def test_prompt_contains_file_path(self):
        ranked = [RankedFile(path="src/auth.py", priority=5, reasons=("auth",))]
        tasks = create_focus_tasks(ranked)

        assert "src/auth.py" in tasks[0].prompt
        assert "Do NOT review other files" in tasks[0].prompt

    def test_empty_input(self):
        assert create_focus_tasks([]) == []

    def test_handles_braces_in_path(self):
        """Paths containing literal { or } appear verbatim in the prompt."""
        ranked = [RankedFile(path="weird{name}.py", priority=3, reasons=("util",))]
        tasks = create_focus_tasks(ranked)
        assert len(tasks) == 1
        assert tasks[0].file_path == "weird{name}.py"
        assert "weird{name}.py" in tasks[0].prompt

    def test_single_pass_substitution_is_not_reentrant(self):
        """Regression for the `.replace()` chain bug: a path containing
        a literal ``{reasons}`` token must NOT have reasons re-substituted
        into it. The substituted ``file_path`` value is final — later
        placeholder passes cannot touch it."""
        # Adversarial path — would have been rewritten by the old replace()
        # chain because replace("{file_path}", ...) runs before
        # replace("{reasons}", ...).
        ranked = [RankedFile(path="weird{reasons}.py", priority=3, reasons=("alpha",))]
        tasks = create_focus_tasks(ranked)
        prompt = tasks[0].prompt
        # The literal ``{reasons}`` from the path must survive intact:
        assert "weird{reasons}.py" in prompt
        # And the dedicated {reasons} slot must still render "alpha":
        assert "Relevant signals: alpha" in prompt
        # Guard against the ordering bug: "weirdalpha.py" must never appear.
        assert "weirdalpha.py" not in prompt

    def test_unknown_placeholders_passthrough(self):
        """Custom templates may use literal braces (e.g. JSON examples);
        unknown placeholders are left intact so the template author
        doesn't need to escape them."""
        from advisor.focus import create_focus_tasks

        ranked = [RankedFile(path="a.py", priority=3, reasons=("x",))]
        template = "for {file_path} priority={priority} {unknown} reasons={reasons}"
        tasks = create_focus_tasks(ranked, prompt_template=template)
        assert tasks[0].prompt == "for a.py priority=3 {unknown} reasons=x"

    def test_immutability(self):
        ranked = [RankedFile(path="src/a.py", priority=5, reasons=("auth",))]
        task = create_focus_tasks(ranked)[0]
        with pytest.raises(AttributeError):
            task.file_path = "other.py"  # type: ignore


class TestFormatDispatchPlan:
    def test_formats_markdown(self):
        tasks = [
            FocusTask(file_path="src/auth.py", priority=5, prompt="..."),
            FocusTask(file_path="src/api.py", priority=3, prompt="..."),
        ]
        plan = format_dispatch_plan(tasks)

        assert "## Dispatch Plan" in plan
        assert "2 focused agents" in plan
        assert "P5" in plan


class TestCreateFocusBatches:
    def _tasks(self, n: int) -> list[FocusTask]:
        return [FocusTask(file_path=f"src/f{i}.py", priority=3, prompt="...") for i in range(n)]

    def test_groups_into_batches(self):
        batches = create_focus_batches(self._tasks(12), files_per_batch=5)
        assert len(batches) == 3
        assert [len(b.tasks) for b in batches] == [5, 5, 2]

    def test_batch_ids_are_sequential(self):
        batches = create_focus_batches(self._tasks(7), files_per_batch=3)
        assert [b.batch_id for b in batches] == [1, 2, 3]

    def test_complexity_propagates(self):
        batches = create_focus_batches(self._tasks(4), files_per_batch=2, complexity="high")
        assert all(b.complexity == "high" for b in batches)

    def test_complexity_auto_derives_from_top_priority(self):
        """Default ``complexity="auto"`` maps top priority to a label —
        replaces the previous flat ``medium`` for every batch, which
        made ``advisor plan --batch-size N`` reports misleading.
        """
        tasks = [
            FocusTask(file_path="low.py", priority=1, prompt=""),
            FocusTask(file_path="med.py", priority=3, prompt=""),
            FocusTask(file_path="hot.py", priority=5, prompt=""),
        ]
        batches = create_focus_batches(tasks, files_per_batch=1)
        assert [b.complexity for b in batches] == ["low", "medium", "high"]

    def test_complexity_auto_uses_top_priority_within_batch(self):
        tasks = [
            FocusTask(file_path="a.py", priority=2, prompt=""),
            FocusTask(file_path="b.py", priority=5, prompt=""),
        ]
        batches = create_focus_batches(tasks, files_per_batch=5)
        # Batch-wide complexity is driven by the hottest file in it.
        assert batches[0].complexity == "high"

    def test_empty_input(self):
        assert create_focus_batches([], files_per_batch=5) == []

    def test_rejects_invalid_batch_size(self):
        with pytest.raises(ValueError):
            create_focus_batches(self._tasks(3), files_per_batch=0)

    def test_immutability(self):
        batch = create_focus_batches(self._tasks(2), files_per_batch=5)[0]
        with pytest.raises(AttributeError):
            batch.batch_id = 99  # type: ignore

    def test_top_priority_helper(self):
        tasks = [
            FocusTask(file_path="a.py", priority=2, prompt=""),
            FocusTask(file_path="b.py", priority=5, prompt=""),
        ]
        batches = create_focus_batches(tasks, files_per_batch=10)
        assert batches[0].top_priority == 5


class TestFormatBatchPlan:
    def test_includes_batch_headers(self):
        tasks = [FocusTask(file_path="src/a.py", priority=5, prompt="...")]
        batches = create_focus_batches(tasks, files_per_batch=5, complexity="medium")
        plan = format_batch_plan(batches)

        assert "## Batch Dispatch Plan" in plan
        assert "Batch 1" in plan
        assert "complexity: medium" in plan
        assert "src/a.py" in plan


class TestDispatchPlanCosmetics:
    """Cosmetic polish on the dispatch-plan header."""

    def test_priority_mix_in_header(self):
        """Header summarises priority distribution in a compact P<n> ×N form.

        Renders inline on the ``Dispatching…`` line so a reader scanning the
        output in Claude Code can see the work shape before the full list.
        Each token uses the bare ``P<n>`` form the priority-coloriser
        recognises, so the summary is auto-painted alongside the numbered
        list below.
        """
        tasks = [
            FocusTask(file_path="a.py", priority=5, prompt="..."),
            FocusTask(file_path="b.py", priority=5, prompt="..."),
            FocusTask(file_path="c.py", priority=3, prompt="..."),
        ]
        plan = format_dispatch_plan(tasks)

        # Distribution is rendered highest-priority-first on a single line.
        assert "(P5 ×2  P3 ×1)" in plan

    def test_singular_agent_label(self):
        """Single-task plan uses ``agent`` (not ``agents``) for grammar."""
        tasks = [FocusTask(file_path="only.py", priority=4, prompt="...")]
        plan = format_dispatch_plan(tasks)

        assert "1 focused agent " in plan
        # Never say "1 focused agents" — ungrammatical in the CLI and in
        # README screenshots.
        assert "1 focused agents" not in plan

    def test_plural_agents_label_preserved(self):
        """Multi-task plan still reads ``agents`` (regression guard)."""
        tasks = [
            FocusTask(file_path="a.py", priority=4, prompt="..."),
            FocusTask(file_path="b.py", priority=4, prompt="..."),
        ]
        plan = format_dispatch_plan(tasks)

        assert "2 focused agents" in plan

    def test_batch_plan_priority_mix_and_grammar(self):
        """Batch-plan header gains the same distribution + grammar polish."""
        tasks = [
            FocusTask(file_path="a.py", priority=5, prompt="..."),
            FocusTask(file_path="b.py", priority=3, prompt="..."),
        ]
        batches = create_focus_batches(tasks, files_per_batch=5)
        plan = format_batch_plan(batches)

        assert "(P5 ×1  P3 ×1)" in plan
        # Single batch = singular "runner"; two files = plural "files".
        assert "1 runner across 2 files" in plan
