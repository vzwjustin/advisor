"""Tests for ``advisor.history`` — findings history JSONL."""

from __future__ import annotations

from pathlib import Path

from advisor.history import (
    HISTORY_SCHEMA_VERSION,
    HistoryEntry,
    append_entries,
    entry_now,
    format_history_block,
    history_path,
    load_recent,
    new_run_id,
)


def _entry(**kw: object) -> HistoryEntry:
    return HistoryEntry(
        timestamp=str(kw.get("timestamp", "2026-04-20T00:00:00+00:00")),
        file_path=str(kw.get("file_path", "a.py")),
        severity=str(kw.get("severity", "high")),
        description=str(kw.get("description", "desc")),
        status=str(kw.get("status", "CONFIRMED")),
        run_id=str(kw.get("run_id", "20260420T000000Z")),
    )


class TestAppendAndLoad:
    def test_append_then_load(self, tmp_path: Path) -> None:
        append_entries(tmp_path, [_entry(file_path="a.py"), _entry(file_path="b.py")])
        entries = load_recent(tmp_path, limit=10)
        assert len(entries) == 2
        assert {e.file_path for e in entries} == {"a.py", "b.py"}

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_recent(tmp_path) == []

    def test_empty_entries_no_file_created(self, tmp_path: Path) -> None:
        append_entries(tmp_path, [])
        assert not history_path(tmp_path).exists()

    def test_malformed_line_skipped(self, tmp_path: Path) -> None:
        import warnings

        path = history_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"broken":true}\n'
            '{"timestamp":"t","file_path":"a","severity":"s","description":"d",'
            '"status":"CONFIRMED","run_id":"r"}\n',
            encoding="utf-8",
        )
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            entries = load_recent(tmp_path)
        assert len(entries) == 1
        assert entries[0].file_path == "a"

    def test_limit_returns_most_recent(self, tmp_path: Path) -> None:
        append_entries(
            tmp_path,
            [_entry(file_path=f"f{i}.py") for i in range(5)],
        )
        entries = load_recent(tmp_path, limit=2)
        assert len(entries) == 2
        assert entries[-1].file_path == "f4.py"


class TestFormatHistoryBlock:
    def test_empty_returns_empty_string(self) -> None:
        assert format_history_block([]) == ""

    def test_non_empty_produces_markdown(self) -> None:
        s = format_history_block([_entry(file_path="x.py", severity="high")])
        assert "Recent findings" in s
        assert "x.py" in s
        assert "[high]" in s


class TestRunIdAndEntryNow:
    def test_new_run_id_format(self) -> None:
        rid = new_run_id()
        assert len(rid) == 16  # YYYYMMDDTHHMMSSZ
        assert rid.endswith("Z")

    def test_entry_now_populates_timestamp(self) -> None:
        e = entry_now(
            file_path="a.py",
            severity="high",
            description="d",
            status="CONFIRMED",
            run_id="r1",
        )
        assert e.timestamp  # non-empty ISO-8601
        assert e.schema_version == HISTORY_SCHEMA_VERSION
