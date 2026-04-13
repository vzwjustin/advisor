"""Tests for advisor.focus module."""

from advisor.rank import RankedFile
from advisor.focus import FocusTask, create_focus_tasks, format_dispatch_plan


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
        try:
            task.file_path = "other.py"  # type: ignore
            assert False, "Should have raised"
        except AttributeError:
            pass


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
