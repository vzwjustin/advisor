"""Tests for the optional web dashboard (``advisor.web``).

These cover:

* Route handler payload correctness (no network).
* End-to-end request/response through a threaded server on an ephemeral port.
* CLI ``ui`` subcommand wiring — argparse and error paths only; we never
  actually block in :func:`run_server` from a test.
"""

from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from pathlib import Path

import pytest

from advisor.__main__ import _NUDGE_SKIP_COMMANDS, build_parser, cmd_ui
from advisor.history import HistoryEntry, append_entries
from advisor.web import build_app_state, run_server
from advisor.web.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    _cost_payload,
    _history_payload,
    _plan_payload,
    _target_payload,
    find_free_port,
)

# ---------------------------------------------------------------------------
# AppState
# ---------------------------------------------------------------------------


class TestBuildAppState:
    def test_resolves_target_to_absolute(self, tmp_path):
        state = build_app_state(tmp_path)
        assert state.target == tmp_path.resolve()

    def test_rejects_missing_target(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            build_app_state(tmp_path / "does-not-exist")

    def test_rejects_file_target(self, tmp_path):
        f = tmp_path / "a.py"
        f.write_text("x = 1")
        with pytest.raises(NotADirectoryError):
            build_app_state(f)

    def test_captures_defaults(self, tmp_path):
        state = build_app_state(
            tmp_path,
            file_types="*.ts",
            min_priority=5,
            advisor_model="sonnet",
            runner_model="haiku",
        )
        assert state.default_file_types == "*.ts"
        assert state.default_min_priority == 5
        assert state.default_advisor_model == "sonnet"
        assert state.default_runner_model == "haiku"


# ---------------------------------------------------------------------------
# Payload builders
# ---------------------------------------------------------------------------


class TestPlanPayload:
    def test_ranks_files_under_target(self, tmp_path):
        (tmp_path / "auth.py").write_text("def login(password): ...")
        (tmp_path / "utils.py").write_text("def helper(): pass")
        state = build_app_state(tmp_path, min_priority=1)
        payload = _plan_payload(state, {})
        paths = [t["file_path"] for t in payload["tasks"]]
        assert any("auth.py" in p for p in paths)
        assert payload["task_count"] == len(payload["tasks"])

    def test_honors_min_priority_query(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1")
        state = build_app_state(tmp_path, min_priority=1)
        low = _plan_payload(state, {"min_priority": ["1"]})
        high = _plan_payload(state, {"min_priority": ["5"]})
        assert high["task_count"] <= low["task_count"]

    def test_invalid_min_priority_falls_back_to_default(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1")
        state = build_app_state(tmp_path, min_priority=1)
        # Non-numeric value must not crash — falls back to state default.
        payload = _plan_payload(state, {"min_priority": ["not-a-number"]})
        assert payload["min_priority"] == 1


class TestCostPayload:
    def test_returns_null_estimate_when_empty(self, tmp_path):
        state = build_app_state(tmp_path, min_priority=5)
        payload = _cost_payload(state, {})
        assert payload["task_count"] == 0
        assert payload["estimate"] is None

    def test_includes_estimate_when_tasks_present(self, tmp_path):
        (tmp_path / "auth.py").write_text("password = 'x'\n" * 100)
        state = build_app_state(tmp_path, min_priority=1)
        payload = _cost_payload(state, {})
        assert payload["task_count"] >= 1
        assert payload["estimate"] is not None
        assert "cost_usd_min" in payload["estimate"]
        assert "cost_usd_max" in payload["estimate"]


class TestHistoryPayload:
    def test_empty_history(self, tmp_path):
        state = build_app_state(tmp_path)
        payload = _history_payload(state, {})
        assert payload["count"] == 0
        assert payload["entries"] == []

    def test_reads_appended_entries(self, tmp_path):
        append_entries(
            tmp_path,
            [
                HistoryEntry(
                    timestamp="2026-04-21T12:00:00+00:00",
                    file_path="a.py",
                    severity="HIGH",
                    description="something",
                    status="CONFIRMED",
                    run_id="r1",
                )
            ],
        )
        state = build_app_state(tmp_path)
        payload = _history_payload(state, {})
        assert payload["count"] == 1
        assert payload["entries"][0]["file_path"] == "a.py"


class TestTargetPayload:
    def test_shape(self, tmp_path):
        state = build_app_state(tmp_path)
        payload = _target_payload(state)
        assert payload["target"] == str(tmp_path.resolve())
        assert set(payload["defaults"]) == {
            "file_types",
            "min_priority",
            "advisor_model",
            "runner_model",
        }


# ---------------------------------------------------------------------------
# End-to-end HTTP
# ---------------------------------------------------------------------------


@pytest.fixture
def live_server(tmp_path):
    """Start :func:`run_server` on a free port in a background thread.

    Yields ``(host, port, target)``. The thread is a daemon so a hung test
    won't prevent pytest from exiting; we still explicitly shut down the
    server in a finally block.
    """
    port = find_free_port(start=DEFAULT_PORT + 1)
    state = build_app_state(tmp_path, min_priority=1)

    # We can't call run_server() directly because it blocks. Reimplement
    # the minimal start path here so the test controls shutdown.
    from http.server import ThreadingHTTPServer

    from advisor.web.server import _make_handler_class

    handler_cls = _make_handler_class(state, log_requests=False)
    server = ThreadingHTTPServer((DEFAULT_HOST, port), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield DEFAULT_HOST, port, tmp_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(host: str, port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    """Simple stdlib HTTP GET so tests don't depend on :mod:`requests`."""
    conn = HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, dict(resp.getheaders()), body
    finally:
        conn.close()


class TestLiveEndpoints:
    def test_index_html(self, live_server):
        host, port, _ = live_server
        status, headers, body = _get(host, port, "/")
        assert status == 200
        assert "text/html" in headers["Content-Type"]
        assert b"advisor dashboard" in body

    def test_static_assets(self, live_server):
        host, port, _ = live_server
        css_status, css_headers, css_body = _get(host, port, "/static/app.css")
        js_status, js_headers, js_body = _get(host, port, "/static/app.js")
        assert css_status == 200
        assert "text/css" in css_headers["Content-Type"]
        assert js_status == 200
        assert "javascript" in js_headers["Content-Type"]
        assert b":root" in css_body
        assert b"buildCli" in js_body

    def test_api_target(self, live_server):
        host, port, tmp = live_server
        status, _, body = _get(host, port, "/api/target")
        assert status == 200
        data = json.loads(body)
        assert Path(data["target"]) == tmp.resolve()

    def test_api_plan(self, live_server):
        host, port, tmp = live_server
        (tmp / "auth.py").write_text("password = 'x'")
        status, headers, body = _get(host, port, "/api/plan?min_priority=1")
        assert status == 200
        assert "application/json" in headers["Content-Type"]
        data = json.loads(body)
        assert data["task_count"] >= 1

    def test_api_history_empty(self, live_server):
        host, port, _ = live_server
        status, _, body = _get(host, port, "/api/history")
        assert status == 200
        assert json.loads(body)["count"] == 0

    def test_unknown_route_returns_404_json(self, live_server):
        host, port, _ = live_server
        status, headers, body = _get(host, port, "/api/nope")
        assert status == 404
        assert "application/json" in headers["Content-Type"]
        assert "error" in json.loads(body)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestUiCommand:
    def test_parser_registers_ui(self):
        parser = build_parser()
        args = parser.parse_args(["ui", ".", "--port", "9000", "--host", "127.0.0.1"])
        assert args.command == "ui"
        assert args.port == 9000
        assert args.host == "127.0.0.1"

    def test_ui_skips_nudge(self):
        assert "ui" in _NUDGE_SKIP_COMMANDS

    def test_missing_target_returns_2(self, tmp_path, capsys):
        parser = build_parser()
        args = parser.parse_args(["ui", str(tmp_path / "nope")])
        assert cmd_ui(args) == 2
        assert "target not found" in capsys.readouterr().err

    def test_file_target_returns_2(self, tmp_path, capsys):
        f = tmp_path / "x.py"
        f.write_text("")
        parser = build_parser()
        args = parser.parse_args(["ui", str(f)])
        assert cmd_ui(args) == 2
        assert "not a directory" in capsys.readouterr().err

    def test_missing_extras_path(self, monkeypatch, tmp_path, capsys):
        """If ``advisor.web`` can't be imported, the command should exit 1 with
        the install hint — not crash.

        ``advisor.web`` is already loaded into ``sys.modules`` by the rest of
        this test file, so patching ``builtins.__import__`` alone wouldn't
        fire. Force a cache miss, then rig the finder to refuse the reload.
        """
        import builtins
        import sys

        # Evict cached submodules so the ``from .web import ...`` inside
        # cmd_ui forces a re-import (which we then intercept).
        for name in list(sys.modules):
            if name == "advisor.web" or name.startswith("advisor.web."):
                monkeypatch.delitem(sys.modules, name, raising=False)

        real_import = builtins.__import__

        def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
            # Match both absolute ("advisor.web") and relative (level=1, name="web")
            # import forms. The CLI uses ``from .web import ...`` so ``level=1``.
            if name == "advisor.web" or (level == 1 and name == "web"):
                raise ImportError("simulated missing extra")
            return real_import(name, globals, locals, fromlist, level)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        parser = build_parser()
        args = parser.parse_args(["ui", str(tmp_path)])
        assert cmd_ui(args) == 1
        err = capsys.readouterr().err
        assert "advisor-agent[ui]" in err

    def test_bind_collision_returns_1(self, tmp_path, capsys):
        """When the requested port is already taken, the command surfaces a
        clean error rather than a stack trace."""
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((DEFAULT_HOST, 0))
        busy_port = sock.getsockname()[1]
        try:
            parser = build_parser()
            args = parser.parse_args(
                ["ui", str(tmp_path), "--port", str(busy_port)]
            )
            assert cmd_ui(args) == 1
            assert "could not bind" in capsys.readouterr().err
        finally:
            sock.close()


class TestRunServerKeyboardInterrupt:
    """``run_server`` should trap Ctrl-C and exit cleanly (rc=None → 0)."""

    def test_keyboard_interrupt_is_caught(self, tmp_path, monkeypatch, capsys):
        state = build_app_state(tmp_path)

        class _FakeServer:
            def __init__(self, *a, **kw):
                pass

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                pass

        monkeypatch.setattr(
            "advisor.web.server.ThreadingHTTPServer", lambda *a, **kw: _FakeServer()
        )
        run_server(state, port=find_free_port(start=DEFAULT_PORT + 100))
        assert "shutting down" in capsys.readouterr().out
