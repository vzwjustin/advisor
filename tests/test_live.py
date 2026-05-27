"""Tests for the live event-stream module + CLI + ``/api/events`` route.

Three layers exercised:

* :mod:`advisor.live` — pure-Python append / load / cursor semantics.
* :mod:`advisor.web` — the ``/api/events`` dashboard endpoint reading the
  same file.
* :mod:`advisor.__main__` — the ``advisor live record / tail / clear``
  CLI subcommands the team-lead invokes via ``Bash`` from the
  ``/advisor`` skill.

The layers are deliberately separate so a regression at any one of them
points at a single test class.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from advisor.__main__ import build_parser, cmd_live
from advisor.live import (
    EVENT_KINDS_CORE,
    LIVE_SCHEMA_VERSION,
    append_event,
    latest_seq,
    live_events_path,
    load_recent_events,
)
from advisor.web import build_app_state
from advisor.web.server import _events_payload

# ---------------------------------------------------------------------------
# advisor.live module — pure-Python file format
# ---------------------------------------------------------------------------


class TestAppendEvent:
    def test_creates_dir_and_file_on_first_use(self, tmp_path):
        path = append_event(tmp_path, "run_start", {"run_id": "r1"})
        assert path == live_events_path(tmp_path)
        assert path.exists()
        assert (tmp_path / ".advisor" / "live").is_dir()

    def test_seq_starts_at_one_and_increments(self, tmp_path):
        append_event(tmp_path, "run_start", {})
        append_event(tmp_path, "runner_spawn", {})
        append_event(tmp_path, "report_relay", {})
        events = load_recent_events(tmp_path)
        assert [e["seq"] for e in events] == [1, 2, 3]

    def test_ts_uses_z_suffix_not_offset(self, tmp_path):
        append_event(tmp_path, "run_start", {})
        events = load_recent_events(tmp_path)
        assert events[0]["ts"].endswith("Z")
        assert "+00:00" not in events[0]["ts"]

    def test_explicit_ts_passthrough(self, tmp_path):
        ts = "2026-05-26T17:41:57.892Z"
        append_event(tmp_path, "run_start", {}, ts=ts)
        events = load_recent_events(tmp_path)
        assert events[0]["ts"] == ts

    def test_schema_version_stamped(self, tmp_path):
        append_event(tmp_path, "run_start", {})
        events = load_recent_events(tmp_path)
        assert events[0]["schema_version"] == LIVE_SCHEMA_VERSION

    def test_rejects_empty_kind(self, tmp_path):
        with pytest.raises(ValueError, match="non-empty"):
            append_event(tmp_path, "", {})

    def test_rejects_non_dict_data(self, tmp_path):
        with pytest.raises(ValueError, match="dict"):
            append_event(tmp_path, "x", "not a dict")  # type: ignore[arg-type]

    def test_data_none_becomes_empty_dict(self, tmp_path):
        append_event(tmp_path, "x", None)
        events = load_recent_events(tmp_path)
        assert events[0]["data"] == {}

    def test_oversize_line_rejected(self, tmp_path):
        # Construct a payload that pushes past the 64 KiB per-line cap.
        huge = "x" * 70_000
        with pytest.raises(ValueError, match="too large"):
            append_event(tmp_path, "x", {"big": huge})

    def test_seq_continues_after_reopen(self, tmp_path):
        """A fresh process picks up where the prior writer left off."""
        append_event(tmp_path, "a", {})
        append_event(tmp_path, "b", {})
        # Simulate restart: nothing in memory, but file persists.
        append_event(tmp_path, "c", {})
        events = load_recent_events(tmp_path)
        assert [e["seq"] for e in events] == [1, 2, 3]
        assert [e["kind"] for e in events] == ["a", "b", "c"]

    def test_seq_continues_after_long_valid_event(self, tmp_path):
        """A valid event larger than 8 KiB must not reset the cursor."""
        append_event(tmp_path, "run_start", {"summary": "x" * 9000})
        append_event(tmp_path, "report_relay", {})
        events = load_recent_events(tmp_path)
        assert [(e["seq"], e["kind"]) for e in events] == [
            (1, "run_start"),
            (2, "report_relay"),
        ]
        assert latest_seq(tmp_path) == 2
        assert [e["kind"] for e in load_recent_events(tmp_path, since=1)] == ["report_relay"]


class TestLoadRecentEvents:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_recent_events(tmp_path) == []

    def test_cursor_excludes_seqs_at_or_below(self, tmp_path):
        append_event(tmp_path, "a", {})
        append_event(tmp_path, "b", {})
        append_event(tmp_path, "c", {})
        new = load_recent_events(tmp_path, since=1)
        assert [e["kind"] for e in new] == ["b", "c"]

    def test_cursor_strict_greater_than(self, tmp_path):
        append_event(tmp_path, "a", {})
        # ``since=seq`` of the latest must return nothing — strictly greater.
        new = load_recent_events(tmp_path, since=1)
        assert new == []

    def test_limit_zero_returns_empty(self, tmp_path):
        append_event(tmp_path, "a", {})
        assert load_recent_events(tmp_path, limit=0) == []

    def test_chronological_order(self, tmp_path):
        for k in ("a", "b", "c", "d"):
            append_event(tmp_path, k, {})
        events = load_recent_events(tmp_path)
        assert [e["kind"] for e in events] == ["a", "b", "c", "d"]

    def test_malformed_line_skipped_with_warning(self, tmp_path):
        append_event(tmp_path, "good_before", {})
        # Inject a malformed line in the middle.
        events_file = live_events_path(tmp_path)
        with events_file.open("a", encoding="utf-8") as f:
            f.write("not json\n")
        append_event(tmp_path, "good_after", {})
        with pytest.warns(UserWarning, match="malformed"):
            events = load_recent_events(tmp_path)
        assert [e["kind"] for e in events] == ["good_before", "good_after"]


class TestLatestSeq:
    def test_zero_when_absent(self, tmp_path):
        assert latest_seq(tmp_path) == 0

    def test_matches_highest_seq(self, tmp_path):
        append_event(tmp_path, "a", {})
        append_event(tmp_path, "b", {})
        assert latest_seq(tmp_path) == 2


class TestCoreKindsContract:
    def test_canonical_kinds_documented(self):
        # The dashboard JS gives these kinds specialized rendering. The
        # set IS the API contract between the team-lead and the
        # dashboard — adding/removing a name here is a breaking change.
        assert (
            frozenset({"run_start", "runner_spawn", "report_relay", "fix_dispatch", "run_end"})
            == EVENT_KINDS_CORE
        )


# ---------------------------------------------------------------------------
# /api/events endpoint
# ---------------------------------------------------------------------------


class TestEventsPayload:
    def test_empty_when_no_file(self, tmp_path):
        state = build_app_state(tmp_path)
        payload = _events_payload(state, {})
        assert payload["count"] == 0
        assert payload["events"] == []
        assert payload["next_token"] == 0

    def test_returns_events_in_chronological_order(self, tmp_path):
        append_event(tmp_path, "run_start", {"run_id": "r1"})
        append_event(tmp_path, "runner_spawn", {"runner_name": "runner-1"})
        state = build_app_state(tmp_path)
        payload = _events_payload(state, {})
        kinds = [e["kind"] for e in payload["events"]]
        assert kinds == ["run_start", "runner_spawn"]
        assert payload["next_token"] == 2

    def test_since_cursor_advances(self, tmp_path):
        for k in ("a", "b", "c"):
            append_event(tmp_path, k, {})
        state = build_app_state(tmp_path)
        payload = _events_payload(state, {"since": ["1"]})
        assert [e["kind"] for e in payload["events"]] == ["b", "c"]
        assert payload["next_token"] == 3

    def test_invalid_since_treated_as_initial_poll(self, tmp_path):
        append_event(tmp_path, "a", {})
        state = build_app_state(tmp_path)
        payload = _events_payload(state, {"since": ["not-an-int"]})
        # Malformed cursor falls back to "from the start", not 500.
        assert payload["count"] == 1

    def test_limit_caps_response(self, tmp_path):
        for i in range(5):
            append_event(tmp_path, f"k{i}", {})
        state = build_app_state(tmp_path)
        payload = _events_payload(state, {"limit": ["2"]})
        assert payload["count"] == 2
        # ``next_token`` is the latest on disk, NOT the latest returned,
        # so the client advances past the clipped window correctly.
        assert payload["next_token"] == 5

    def test_negative_since_clamps_to_zero(self, tmp_path):
        append_event(tmp_path, "a", {})
        state = build_app_state(tmp_path)
        payload = _events_payload(state, {"since": ["-5"]})
        # Negative cursor → since=0 → strict-greater → returns everything.
        assert payload["count"] == 1


# ---------------------------------------------------------------------------
# advisor live CLI
# ---------------------------------------------------------------------------


class TestLiveCli:
    def test_record_appends_event(self, tmp_path, capsys):
        parser = build_parser()
        args = parser.parse_args(
            [
                "live",
                "record",
                "--kind",
                "run_start",
                "--data",
                '{"run_id":"r1","pool_size_advisory":4}',
                "--quiet",
                str(tmp_path),
            ]
        )
        assert cmd_live(args) == 0
        events = load_recent_events(tmp_path)
        assert len(events) == 1
        assert events[0]["kind"] == "run_start"
        assert events[0]["data"]["run_id"] == "r1"

    def test_record_accepts_custom_kind(self, tmp_path):
        parser = build_parser()
        args = parser.parse_args(
            ["live", "record", "--kind", "budget_tick", "--quiet", str(tmp_path)]
        )
        assert cmd_live(args) == 0
        assert load_recent_events(tmp_path)[0]["kind"] == "budget_tick"

    def test_record_rejects_non_object_data(self, tmp_path):
        parser = build_parser()
        args = parser.parse_args(
            ["live", "record", "--kind", "run_start", "--data", "[1,2,3]", str(tmp_path)]
        )
        # JSON arrays / scalars are not allowed — must be an object.
        assert cmd_live(args) == 2

    def test_record_rejects_invalid_data_json(self, tmp_path):
        parser = build_parser()
        args = parser.parse_args(
            ["live", "record", "--kind", "run_start", "--data", "{not json", str(tmp_path)]
        )
        assert cmd_live(args) == 2

    def test_record_data_dash_reads_stdin(self, tmp_path, monkeypatch):
        """``--data -`` reads the JSON object from stdin. Lets the
        team-lead pipe in payloads (long summaries, JSON-escaped
        multi-line bodies) that don't fit comfortably as a CLI flag."""
        import io

        class _FakeStdin:
            def __init__(self, payload: bytes):
                self.buffer = io.BytesIO(payload)

            def isatty(self) -> bool:
                return False

        payload = b'{"runner_name":"runner-1","summary":"piped from stdin"}'
        monkeypatch.setattr("sys.stdin", _FakeStdin(payload))
        parser = build_parser()
        args = parser.parse_args(
            ["live", "record", "--kind", "report_relay", "--data", "-", "--quiet", str(tmp_path)]
        )
        assert cmd_live(args) == 0
        events = load_recent_events(tmp_path)
        assert len(events) == 1
        assert events[0]["data"]["runner_name"] == "runner-1"
        assert events[0]["data"]["summary"] == "piped from stdin"

    def test_record_data_dash_preserves_multiline_summary(self, tmp_path, monkeypatch):
        """The motivation for ``--data -`` is payloads that don't fit
        as a CLI flag — multi-line ``report_relay.summary`` bodies are
        the canonical case. Embedded ``\\n`` inside the JSON string
        must survive the stdin read + parse + append round-trip."""
        import io

        class _FakeStdin:
            def __init__(self, payload: bytes):
                self.buffer = io.BytesIO(payload)

            def isatty(self) -> bool:
                return False

        # Multi-line summary uses ``\n`` inside the JSON string literal —
        # what a shell heredoc emits when piping a real runner report.
        payload = b'{"summary":"finding 1: leak\\nfinding 2: race\\nfinding 3: nil deref"}'
        monkeypatch.setattr("sys.stdin", _FakeStdin(payload))
        parser = build_parser()
        args = parser.parse_args(
            ["live", "record", "--kind", "report_relay", "--data", "-", "--quiet", str(tmp_path)]
        )
        assert cmd_live(args) == 0
        events = load_recent_events(tmp_path)
        assert events[0]["data"]["summary"] == (
            "finding 1: leak\nfinding 2: race\nfinding 3: nil deref"
        )

    def test_record_data_dash_rejects_tty(self, tmp_path, monkeypatch, capsys):
        """``--data -`` with no stdin pipe (interactive TTY) errors loudly
        instead of silently waiting on a never-coming read."""

        class _TtyStdin:
            buffer = None

            def isatty(self) -> bool:
                return True

        monkeypatch.setattr("sys.stdin", _TtyStdin())
        parser = build_parser()
        args = parser.parse_args(
            ["live", "record", "--kind", "run_start", "--data", "-", str(tmp_path)]
        )
        assert cmd_live(args) == 2
        err = capsys.readouterr().err
        assert "no data on stdin" in err

    def test_record_data_dash_rejects_empty_pipe(self, tmp_path, monkeypatch, capsys):
        """Regression: an empty stdin pipe (broken upstream / typo in
        the producer) used to silently record an event with empty data
        because ``if data_raw:`` is falsy for ``""``. The explicit
        ``--data -`` flag means "I have data on stdin"; empty is now
        treated as the operator error it almost always is."""
        import io

        class _FakeStdin:
            def __init__(self) -> None:
                self.buffer = io.BytesIO(b"")  # empty pipe

            def isatty(self) -> bool:
                return False

        monkeypatch.setattr("sys.stdin", _FakeStdin())
        parser = build_parser()
        args = parser.parse_args(
            ["live", "record", "--kind", "report_relay", "--data", "-", str(tmp_path)]
        )
        assert cmd_live(args) == 2
        err = capsys.readouterr().err
        assert "stdin was empty" in err
        # No event was recorded.
        assert load_recent_events(tmp_path) == []

    def test_record_data_dash_rejects_whitespace_only_pipe(self, tmp_path, monkeypatch, capsys):
        """Stripping whitespace before the empty check means a pipe of
        just newlines / spaces is also caught — same operator-error
        class as a fully-empty pipe."""
        import io

        class _FakeStdin:
            def __init__(self) -> None:
                self.buffer = io.BytesIO(b"   \n\n\t  \n")

            def isatty(self) -> bool:
                return False

        monkeypatch.setattr("sys.stdin", _FakeStdin())
        parser = build_parser()
        args = parser.parse_args(
            ["live", "record", "--kind", "report_relay", "--data", "-", str(tmp_path)]
        )
        assert cmd_live(args) == 2
        assert "stdin was empty" in capsys.readouterr().err

    def test_record_json_output(self, tmp_path, capsys):
        parser = build_parser()
        args = parser.parse_args(["live", "record", "--kind", "run_start", "--json", str(tmp_path)])
        assert cmd_live(args) == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["kind"] == "run_start"
        assert payload["seq"] == 1
        assert Path(payload["path"]).exists()

    def test_tail_empty(self, tmp_path, capsys):
        parser = build_parser()
        args = parser.parse_args(["live", "tail", str(tmp_path)])
        assert cmd_live(args) == 0
        captured = capsys.readouterr()
        assert "no live events" in captured.out

    def test_tail_prints_recorded_events(self, tmp_path, capsys):
        append_event(tmp_path, "run_start", {"run_id": "r1"})
        append_event(tmp_path, "runner_spawn", {"runner_name": "runner-1"})
        parser = build_parser()
        args = parser.parse_args(["live", "tail", str(tmp_path)])
        assert cmd_live(args) == 0
        captured = capsys.readouterr()
        assert "run_start" in captured.out
        assert "runner_spawn" in captured.out
        assert "runner-1" in captured.out

    def test_tail_json_output(self, tmp_path, capsys):
        append_event(tmp_path, "run_start", {})
        parser = build_parser()
        args = parser.parse_args(["live", "tail", "--json", str(tmp_path)])
        assert cmd_live(args) == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["count"] == 1
        assert payload["events"][0]["kind"] == "run_start"
        assert payload["next_token"] == 1

    def test_tail_since_cursor(self, tmp_path, capsys):
        append_event(tmp_path, "a", {})
        append_event(tmp_path, "b", {})
        append_event(tmp_path, "c", {})
        parser = build_parser()
        args = parser.parse_args(["live", "tail", "--since", "1", "--json", str(tmp_path)])
        assert cmd_live(args) == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert [e["kind"] for e in payload["events"]] == ["b", "c"]

    def test_tail_since_rejects_non_integer_at_argparse(self, tmp_path):
        """Regression: ``--since`` was ``type=str`` with manual int
        conversion inside ``cmd_live``, so a typo like ``--since abc``
        slipped past argparse and hit a custom error. Now argparse
        fails the usage-help path consistently with ``--limit``."""
        import pytest

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["live", "tail", "--since", "abc", str(tmp_path)])

    def test_tail_since_rejects_negative_at_argparse(self, tmp_path):
        """``--since`` must be a cursor (>=0)."""
        import pytest

        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["live", "tail", "--since", "-1", str(tmp_path)])

    def test_clear_removes_file(self, tmp_path):
        append_event(tmp_path, "x", {})
        path = live_events_path(tmp_path)
        assert path.exists()
        parser = build_parser()
        args = parser.parse_args(["live", "clear", "--quiet", str(tmp_path)])
        assert cmd_live(args) == 0
        assert not path.exists()

    def test_clear_idempotent_when_absent(self, tmp_path):
        parser = build_parser()
        args = parser.parse_args(["live", "clear", "--quiet", str(tmp_path)])
        assert cmd_live(args) == 0  # no-op, exit 0

    def test_subcommand_missing_prints_usage(self, tmp_path, capsys):
        parser = build_parser()
        # Parser sets live_sub=None when no subcommand given. cmd_live
        # should print guidance to stderr and exit 2.
        args = parser.parse_args(["live"])
        assert cmd_live(args) == 2
        captured = capsys.readouterr()
        assert "subcommand" in captured.err.lower()
