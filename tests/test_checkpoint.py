"""Tests for ``advisor.checkpoint`` — save/resume plan state."""

from __future__ import annotations

from pathlib import Path

import pytest

from advisor.checkpoint import (
    checkpoint_path,
    list_checkpoints,
    load_checkpoint,
    save_checkpoint,
)
from advisor.focus import FocusBatch, FocusTask


def _tasks() -> list[FocusTask]:
    return [
        FocusTask(file_path="a.py", priority=5, prompt="p1"),
        FocusTask(file_path="b.py", priority=3, prompt="p2"),
    ]


def _batches(tasks: list[FocusTask]) -> list[FocusBatch]:
    return [FocusBatch(batch_id=1, tasks=tuple(tasks), complexity="medium")]


class TestSaveAndLoad:
    def test_roundtrip(self, tmp_path: Path) -> None:
        tasks = _tasks()
        batches = _batches(tasks)
        path = save_checkpoint(
            tmp_path,
            run_id="r1",
            tasks=tasks,
            batches=batches,
            team_name="t",
            file_types="*.py",
            min_priority=3,
            max_runners=5,
            advisor_model="opus",
            runner_model="sonnet",
            max_fixes_per_runner=5,
            test_command="pytest",
            context="ctx",
        )
        assert path.exists()
        assert path == checkpoint_path(tmp_path, "r1")
        cp = load_checkpoint(tmp_path, "r1")
        assert cp.run_id == "r1"
        assert cp.test_command == "pytest"
        assert cp.context == "ctx"
        assert len(cp.tasks) == 2
        assert cp.tasks[0]["file_path"] == "a.py"
        assert cp.tasks[0]["prompt"] == "p1"
        assert len(cp.batches) == 1
        assert cp.batches[0]["batch_id"] == 1

    def test_large_file_fields_roundtrip(self, tmp_path: Path) -> None:
        save_checkpoint(
            tmp_path,
            run_id="big",
            tasks=_tasks(),
            batches=None,
            team_name="t",
            file_types="*.py",
            min_priority=3,
            max_runners=5,
            advisor_model="opus",
            runner_model="sonnet",
            max_fixes_per_runner=4,
            large_file_line_threshold=500,
            large_file_max_fixes=1,
        )
        cp = load_checkpoint(tmp_path, "big")
        assert cp.max_fixes_per_runner == 4
        assert cp.large_file_line_threshold == 500
        assert cp.large_file_max_fixes == 1

    def test_legacy_checkpoint_without_large_file_fields(self, tmp_path: Path) -> None:
        """A pre-upgrade checkpoint (no large_file_* keys) still loads with defaults."""
        import json

        path = checkpoint_path(tmp_path, "legacy")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": "legacy",
            "created_at": "2026-01-01T00:00:00+00:00",
            "target": str(tmp_path),
            "team_name": "t",
            "file_types": "*.py",
            "min_priority": 3,
            "max_runners": 5,
            "advisor_model": "opus",
            "runner_model": "sonnet",
            "max_fixes_per_runner": 5,
            "test_command": "",
            "context": "",
            "tasks": [],
            "batches": [],
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        cp = load_checkpoint(tmp_path, "legacy")
        assert cp.large_file_line_threshold == 800
        assert cp.large_file_max_fixes == 3

    def test_missing_checkpoint_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_checkpoint(tmp_path, "nope")

    def test_malformed_checkpoint_raises_value_error(self, tmp_path: Path) -> None:
        path = checkpoint_path(tmp_path, "bad")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ValueError):
            load_checkpoint(tmp_path, "bad")

    def test_missing_required_field_raises(self, tmp_path: Path) -> None:
        path = checkpoint_path(tmp_path, "missing")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"run_id":"x"}', encoding="utf-8")
        with pytest.raises(ValueError):
            load_checkpoint(tmp_path, "missing")

    def test_run_id_rejects_path_separators(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            checkpoint_path(tmp_path, "..\\..\\victim")

    def test_save_rejects_path_traversal_run_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            save_checkpoint(
                tmp_path,
                run_id="..\\..\\victim",
                tasks=_tasks(),
                batches=None,
                team_name="t",
                file_types="*.py",
                min_priority=3,
                max_runners=5,
                advisor_model="opus",
                runner_model="sonnet",
            )


class TestListCheckpoints:
    def test_empty_when_no_dir(self, tmp_path: Path) -> None:
        assert list_checkpoints(tmp_path) == []

    def test_returns_run_ids_newest_first(self, tmp_path: Path) -> None:
        for rid in ("20260101T000000Z", "20260301T000000Z", "20260201T000000Z"):
            save_checkpoint(
                tmp_path,
                run_id=rid,
                tasks=_tasks(),
                batches=None,
                team_name="t",
                file_types="*.py",
                min_priority=3,
                max_runners=5,
                advisor_model="opus",
                runner_model="sonnet",
            )
        ids = list_checkpoints(tmp_path)
        assert ids == [
            "20260301T000000Z",
            "20260201T000000Z",
            "20260101T000000Z",
        ]
