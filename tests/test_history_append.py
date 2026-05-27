"""Tests for ``advisor history-append`` — agent-side write path for findings.

The advisor agent invokes this subcommand at end-of-run (and optionally
per-CONFIRM) to populate ``<target>/.advisor/history.jsonl``. Before this
existed, the file was never written by anything in the pipeline despite
being the data source for the dashboard's Findings tab — every `/advisor`
run produced CONFIRMED findings in chat that vanished on session end.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

from advisor.__main__ import build_parser
from advisor.history import (
    HISTORY_SCHEMA_VERSION,
    history_path,
    load_recent,
)


class _FakeStdin:
    """Minimal stand-in for ``sys.stdin`` exposing ``.buffer`` (bytes).

    Matches the helper pattern used in tests/test_live.py so the
    ``_read_stdin_capped`` path in ``advisor/__main__.py`` is exercised
    end-to-end — the helper prefers the byte buffer to enforce its cap
    in bytes rather than code points.
    """

    def __init__(self, payload: bytes) -> None:
        self.buffer = io.BytesIO(payload)

    def isatty(self) -> bool:
        return False


def _run(monkeypatch, payload: bytes, argv: list[str]) -> int:
    monkeypatch.setattr("sys.stdin", _FakeStdin(payload))
    args = build_parser().parse_args(argv)
    return args.func(args)


class TestHistoryAppendBasic:
    def test_single_object_writes_one_entry(self, tmp_path: Path, monkeypatch, capsys) -> None:
        payload = (
            b'{"file_path":"src/auth.py","severity":"HIGH",'
            b'"description":"missing nonce validation"}'
        )
        rc = _run(monkeypatch, payload, ["history-append", str(tmp_path), "--json"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["appended"] == 1
        assert out["schema_version"] == "1.0"
        entries = load_recent(tmp_path, limit=10)
        assert len(entries) == 1
        e = entries[0]
        assert e.file_path == "src/auth.py"
        assert e.severity == "HIGH"
        assert e.description == "missing nonce validation"
        # Defaults applied.
        assert e.status == "CONFIRMED"
        assert e.run_id  # non-empty
        assert e.timestamp  # non-empty
        assert e.schema_version == HISTORY_SCHEMA_VERSION

    def test_ndjson_writes_multiple_entries(self, tmp_path: Path, monkeypatch, capsys) -> None:
        payload = (
            b'{"file_path":"a.py","severity":"LOW","description":"x"}\n'
            b'{"file_path":"b.py","severity":"MEDIUM","description":"y"}\n'
            b'{"file_path":"c.py","severity":"HIGH","description":"z"}\n'
        )
        rc = _run(monkeypatch, payload, ["history-append", str(tmp_path), "--json"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["appended"] == 3
        files = sorted(e.file_path for e in load_recent(tmp_path, limit=10))
        assert files == ["a.py", "b.py", "c.py"]

    def test_json_array_writes_multiple_entries(self, tmp_path: Path, monkeypatch, capsys) -> None:
        payload = (
            b'[{"file_path":"a.py","severity":"LOW","description":"x"},'
            b'{"file_path":"b.py","severity":"MEDIUM","description":"y"}]'
        )
        rc = _run(monkeypatch, payload, ["history-append", str(tmp_path), "--json"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["appended"] == 2

    def test_blank_lines_in_ndjson_are_skipped(self, tmp_path: Path, monkeypatch, capsys) -> None:
        payload = (
            b"\n"
            b'{"file_path":"a.py","severity":"LOW","description":"x"}\n'
            b"\n"
            b"   \n"
            b'{"file_path":"b.py","severity":"LOW","description":"y"}\n'
        )
        rc = _run(monkeypatch, payload, ["history-append", str(tmp_path), "--json"])
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["appended"] == 2

    def test_custom_status_fixed_accepted(self, tmp_path: Path, monkeypatch, capsys) -> None:
        payload = (
            b'{"file_path":"a.py","severity":"HIGH","description":"applied patch","status":"FIXED"}'
        )
        rc = _run(monkeypatch, payload, ["history-append", str(tmp_path), "--json"])
        assert rc == 0
        assert load_recent(tmp_path, limit=1)[0].status == "FIXED"

    def test_severity_normalized_to_uppercase(self, tmp_path: Path, monkeypatch, capsys) -> None:
        # Agents sometimes write "high" instead of "HIGH". Accept and
        # normalize — rejecting on case would be hostile to the caller.
        payload = b'{"file_path":"a.py","severity":"high","description":"x"}'
        rc = _run(monkeypatch, payload, ["history-append", str(tmp_path), "--json"])
        assert rc == 0
        assert load_recent(tmp_path, limit=1)[0].severity == "HIGH"


class TestHistoryAppendRunId:
    def test_run_id_default_is_unique_per_invocation(self, tmp_path: Path, monkeypatch) -> None:
        # Two invocations without --run-id mint two different run_ids,
        # so the dedup key (which includes run_id) won't collapse them.
        _run(
            monkeypatch,
            b'{"file_path":"a.py","severity":"LOW","description":"x"}',
            ["history-append", str(tmp_path), "--quiet"],
        )
        _run(
            monkeypatch,
            b'{"file_path":"a.py","severity":"LOW","description":"x"}',
            ["history-append", str(tmp_path), "--quiet"],
        )
        entries = load_recent(tmp_path, limit=10)
        assert len(entries) == 2
        assert entries[0].run_id != entries[1].run_id

    def test_run_id_override_via_cli_flag(self, tmp_path: Path, monkeypatch) -> None:
        _run(
            monkeypatch,
            b'{"file_path":"a.py","severity":"LOW","description":"x"}',
            ["history-append", str(tmp_path), "--run-id", "my-run-123", "--quiet"],
        )
        assert load_recent(tmp_path, limit=1)[0].run_id == "my-run-123"

    def test_per_entry_run_id_overrides_cli_default(self, tmp_path: Path, monkeypatch) -> None:
        # Payload-supplied run_id wins over --run-id (which is only a
        # default for entries that omit it).
        payload = (
            b'{"file_path":"a.py","severity":"LOW","description":"x","run_id":"per-entry-run"}'
        )
        _run(
            monkeypatch,
            payload,
            ["history-append", str(tmp_path), "--run-id", "cli-default", "--quiet"],
        )
        assert load_recent(tmp_path, limit=1)[0].run_id == "per-entry-run"


class TestHistoryAppendDedup:
    def test_dedup_skips_duplicate_within_same_run_id(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        payload = b'{"file_path":"a.py","severity":"HIGH","description":"x"}'
        # First write — appends.
        _run(
            monkeypatch,
            payload,
            ["history-append", str(tmp_path), "--run-id", "shared-1", "--quiet"],
        )
        # Second write with same run_id + --dedup — must skip.
        rc = _run(
            monkeypatch,
            payload,
            [
                "history-append",
                str(tmp_path),
                "--run-id",
                "shared-1",
                "--dedup",
                "--json",
            ],
        )
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["appended"] == 0
        assert len(load_recent(tmp_path, limit=10)) == 1

    def test_dedup_lets_different_run_id_through(self, tmp_path: Path, monkeypatch) -> None:
        payload = b'{"file_path":"a.py","severity":"HIGH","description":"x"}'
        _run(
            monkeypatch,
            payload,
            ["history-append", str(tmp_path), "--run-id", "run-A", "--quiet"],
        )
        _run(
            monkeypatch,
            payload,
            [
                "history-append",
                str(tmp_path),
                "--run-id",
                "run-B",
                "--dedup",
                "--quiet",
            ],
        )
        # Same (file, severity, description) but different run_id → two
        # entries, not one. run_id is part of the dedup key by design so
        # the same finding flagged across multiple runs is countable for
        # repeat-offender ranking.
        assert len(load_recent(tmp_path, limit=10)) == 2

    def test_dedup_within_single_invocation(self, tmp_path: Path, monkeypatch, capsys) -> None:
        # Two identical entries in the same NDJSON input — dedup must
        # collapse them to one.
        payload = (
            b'{"file_path":"a.py","severity":"HIGH","description":"x"}\n'
            b'{"file_path":"a.py","severity":"HIGH","description":"x"}\n'
        )
        rc = _run(
            monkeypatch,
            payload,
            [
                "history-append",
                str(tmp_path),
                "--run-id",
                "r1",
                "--dedup",
                "--json",
            ],
        )
        assert rc == 0
        assert json.loads(capsys.readouterr().out)["appended"] == 1


class TestHistoryAppendErrors:
    def test_empty_stdin_rejected(self, tmp_path: Path, monkeypatch, capsys) -> None:
        rc = _run(monkeypatch, b"", ["history-append", str(tmp_path)])
        assert rc == 2
        assert "no JSON input on stdin" in capsys.readouterr().err

    def test_invalid_severity_rejected(self, tmp_path: Path, monkeypatch, capsys) -> None:
        rc = _run(
            monkeypatch,
            b'{"file_path":"a.py","severity":"BANANA","description":"x"}',
            ["history-append", str(tmp_path)],
        )
        assert rc == 2
        assert "BANANA" in capsys.readouterr().err
        # No file written — nothing got past validation.
        assert not history_path(tmp_path).exists()

    def test_invalid_status_rejected(self, tmp_path: Path, monkeypatch, capsys) -> None:
        rc = _run(
            monkeypatch,
            (b'{"file_path":"a.py","severity":"HIGH","description":"x","status":"WIP"}'),
            ["history-append", str(tmp_path)],
        )
        assert rc == 2
        assert "WIP" in capsys.readouterr().err

    def test_missing_file_path_rejected(self, tmp_path: Path, monkeypatch, capsys) -> None:
        rc = _run(
            monkeypatch,
            b'{"severity":"HIGH","description":"x"}',
            ["history-append", str(tmp_path)],
        )
        assert rc == 2
        assert "file_path" in capsys.readouterr().err

    def test_missing_description_rejected(self, tmp_path: Path, monkeypatch, capsys) -> None:
        rc = _run(
            monkeypatch,
            b'{"file_path":"a.py","severity":"HIGH"}',
            ["history-append", str(tmp_path)],
        )
        assert rc == 2
        assert "description" in capsys.readouterr().err

    def test_empty_string_field_rejected(self, tmp_path: Path, monkeypatch, capsys) -> None:
        rc = _run(
            monkeypatch,
            b'{"file_path":"","severity":"HIGH","description":"x"}',
            ["history-append", str(tmp_path)],
        )
        assert rc == 2

    def test_invalid_json_rejected(self, tmp_path: Path, monkeypatch, capsys) -> None:
        rc = _run(
            monkeypatch,
            b"{not json",
            ["history-append", str(tmp_path)],
        )
        assert rc == 2
        assert "invalid JSON" in capsys.readouterr().err

    def test_atomic_failure_skips_all_on_first_bad_entry(self, tmp_path: Path, monkeypatch) -> None:
        # One valid entry, one with bad severity. The validator runs over
        # all entries before any write — the bad one raises, no file is
        # created. Important so the agent gets a clean rejection rather
        # than a partial write that's harder to reason about.
        payload = (
            b'{"file_path":"a.py","severity":"HIGH","description":"good"}\n'
            b'{"file_path":"b.py","severity":"BANANA","description":"bad"}\n'
        )
        rc = _run(monkeypatch, payload, ["history-append", str(tmp_path)])
        assert rc == 2
        assert not history_path(tmp_path).exists()


class TestAdvisorPromptParity:
    """The advisor prompt is the only thing telling the agent to write
    history.jsonl. If this instruction silently disappears, the dashboard
    goes empty again — and the failure is invisible until a user notices.
    Lock the key strings."""

    def test_prompt_mentions_history_append(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parent.parent
            / "advisor"
            / "orchestrate"
            / "_prompts"
            / "advisor.txt"
        )
        text = prompt_path.read_text(encoding="utf-8")
        assert "advisor history-append" in text, (
            "advisor prompt no longer instructs the agent to persist "
            "findings via `advisor history-append` — Findings tab will "
            "be empty after every run."
        )
        assert "history.jsonl" in text
        # The `--dedup` flag is what makes the belt-and-suspenders pattern
        # safe. If the prompt example drops it, repeated runs in one
        # session duplicate rows.
        assert "--dedup" in text
