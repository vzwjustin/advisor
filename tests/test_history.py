"""Tests for ``advisor.history`` — findings history JSONL."""

from __future__ import annotations

import json
from pathlib import Path

from advisor.__main__ import build_parser
from advisor.history import (
    HISTORY_SCHEMA_VERSION,
    HistoryEntry,
    append_entries,
    entry_now,
    format_history_block,
    history_path,
    load_recent,
    new_run_id,
    summarize,
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

    def test_append_flushes_before_unlocking(self, tmp_path: Path, monkeypatch) -> None:
        """The advisory lock must cover the buffered write itself.

        On Windows ``_unlock_exclusive`` runs before close. Without an
        explicit flush before unlock, another process could acquire the lock
        while this process still had the JSONL payload in Python's text
        buffer, defeating the lock's ordering guarantee.
        """
        events: list[str] = []

        class TrackingFile:
            def __init__(self, wrapped):
                self._wrapped = wrapped

            def __enter__(self):
                self._wrapped.__enter__()
                return self

            def __exit__(self, *args):
                return self._wrapped.__exit__(*args)

            def write(self, payload: str) -> int:
                events.append("write")
                return self._wrapped.write(payload)

            def flush(self) -> None:
                events.append("flush")
                self._wrapped.flush()

            def fileno(self) -> int:
                return self._wrapped.fileno()

        real_open = Path.open

        def tracking_open(self: Path, *args, **kwargs):
            opened = real_open(self, *args, **kwargs)
            if self == history_path(tmp_path) and args and args[0] == "a":
                return TrackingFile(opened)
            return opened

        def fake_unlock(_fh) -> None:
            events.append("unlock")

        monkeypatch.setattr(Path, "open", tracking_open)
        monkeypatch.setattr("advisor.history._unlock_exclusive", fake_unlock)
        monkeypatch.setattr("advisor.history._lock_exclusive", lambda _fh: events.append("lock"))

        append_entries(tmp_path, [_entry(file_path="a.py")])

        assert events[:4] == ["lock", "write", "flush", "unlock"]

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
        # Newest-first per the documented contract — f4 was appended
        # last, so it leads, followed by f3.
        assert entries[0].file_path == "f4.py"
        assert entries[1].file_path == "f3.py"


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
        # YYYYMMDDTHHMMSSZ-XXXXXXXX  (timestamp + "-" + 8 hex chars)
        assert len(rid) == 25
        ts, sep, suffix = rid.partition("-")
        assert sep == "-"
        assert ts.endswith("Z")
        assert len(ts) == 16
        assert len(suffix) == 8
        # 8-hex suffix ⇒ only 0-9a-f
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_new_run_id_is_unique_within_a_second(self) -> None:
        """Regression: back-to-back runs in the same second used to
        collide on the run_id and silently overwrite each other's
        checkpoint. The random suffix makes collisions vanishingly rare.
        """
        ids = {new_run_id() for _ in range(50)}
        # With 32 bits of entropy, 50 draws colliding would be a freak event
        # (birthday bound ≈ 50²/2³² ≈ 0). Require at least 48 unique.
        assert len(ids) >= 48

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


class TestSummarize:
    def test_empty_history_is_all_zeros(self) -> None:
        s = summarize([])
        assert s["total"] == 0
        assert s["confirm_rate"] == 0.0  # no ZeroDivision
        assert s["by_status"] == {}
        assert s["by_severity"] == {}
        assert s["run_count"] == 0
        assert s["top_files"] == []

    def test_counts_rate_and_top_files(self) -> None:
        entries = [
            _entry(file_path="auth.py", severity="HIGH", status="CONFIRMED", run_id="r1"),
            _entry(file_path="auth.py", severity="LOW", status="CONFIRMED", run_id="r1"),
            _entry(file_path="db.py", severity="LOW", status="REJECTED", run_id="r2"),
            _entry(file_path="db.py", severity="HIGH", status="CONFIRMED", run_id="r2"),
        ]
        s = summarize(entries)
        assert s["total"] == 4
        assert s["by_status"] == {"CONFIRMED": 3, "REJECTED": 1}
        assert s["by_severity"] == {"HIGH": 2, "LOW": 2}
        assert s["confirm_rate"] == 0.75
        assert s["run_count"] == 2
        # auth.py has 2 CONFIRMED, db.py has 1 CONFIRMED → auth first.
        assert s["top_files"] == [
            {"file_path": "auth.py", "count": 2},
            {"file_path": "db.py", "count": 1},
        ]

    def test_top_files_tiebreak_is_path_sorted(self) -> None:
        entries = [
            _entry(file_path="z.py", status="CONFIRMED"),
            _entry(file_path="a.py", status="CONFIRMED"),
        ]
        s = summarize(entries)
        assert [tf["file_path"] for tf in s["top_files"]] == ["a.py", "z.py"]

    def test_top_n_caps_results(self) -> None:
        entries = [_entry(file_path=f"f{i}.py", status="CONFIRMED") for i in range(15)]
        assert len(summarize(entries, top_n=5)["top_files"]) == 5


class TestHistoryStatsCLI:
    def _run(self, capsys, argv: list[str]) -> str:
        args = build_parser().parse_args(argv)
        rc = args.func(args)
        assert rc == 0
        return capsys.readouterr().out

    def test_stats_json_payload_shape(self, tmp_path: Path, capsys) -> None:
        append_entries(
            tmp_path,
            [
                _entry(file_path="a.py", status="CONFIRMED"),
                _entry(file_path="a.py", status="REJECTED"),
            ],
        )
        out = self._run(capsys, ["history", str(tmp_path), "--stats", "--json"])
        payload = json.loads(out)
        assert payload["schema_version"]
        assert payload["target"].endswith(str(tmp_path.name))
        assert payload["stats"]["total"] == 2
        assert payload["stats"]["confirm_rate"] == 0.5

    def test_stats_ignores_limit(self, tmp_path: Path, capsys) -> None:
        append_entries(
            tmp_path,
            [_entry(file_path=f"f{i}.py", status="CONFIRMED") for i in range(30)],
        )
        out = self._run(capsys, ["history", str(tmp_path), "--stats", "--json", "--limit", "1"])
        # --limit caps the recent-list view only; --stats aggregates the
        # 500-entry window, so all 30 entries are counted despite --limit 1.
        assert json.loads(out)["stats"]["total"] == 30

    def test_stats_caps_at_500_most_recent(self, tmp_path: Path, capsys) -> None:
        # Locks the documented contract: --stats aggregates up to the 500
        # most recent findings (the ranker's window), not the entire file.
        append_entries(
            tmp_path,
            [_entry(file_path=f"f{i}.py", status="CONFIRMED") for i in range(510)],
        )
        out = self._run(capsys, ["history", str(tmp_path), "--stats", "--json"])
        assert json.loads(out)["stats"]["total"] == 500

    def test_stats_empty_history_friendly_message(self, tmp_path: Path, capsys) -> None:
        out = self._run(capsys, ["history", str(tmp_path), "--stats"])
        assert "no history yet" in out
