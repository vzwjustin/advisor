"""Tests for advisor.focus module."""

import pytest

from advisor.rank import RankedFile
from advisor.focus import (
    FocusBatch,
    FocusTask,
    create_focus_batches,
    create_focus_tasks,
    format_batch_plan,
    format_dispatch_plan,
)


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
        ranked = [
            RankedFile(path=f"src/f{i}.py", priority=5, reasons=("auth",))
            for i in range(20)
        ]
        tasks = create_focus_tasks(ranked, max_tasks=3)

        assert len(tasks) == 3

    def test_no_max_tasks_default(self):
        ranked = [
            RankedFile(path=f"src/f{i}.py", priority=5, reasons=("auth",))
            for i in range(50)
        ]
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
        return [
            FocusTask(file_path=f"src/f{i}.py", priority=3, prompt="...")
            for i in range(n)
        ]

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
