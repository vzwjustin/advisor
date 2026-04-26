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
from advisor.web.assets import APP_JS
from advisor.web.server import (
    DEFAULT_HOST,
    _cost_payload,
    _history_payload,
    _plan_payload,
    _rank_target,
    _status_payload,
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
            max_runners=7,
            advisor_model="sonnet",
            runner_model="haiku",
        )
        assert state.default_file_types == "*.ts"
        assert state.default_min_priority == 5
        assert state.default_max_runners == 7
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

    def test_respects_advisorignore(self, tmp_path):
        """Files matched by ``.advisorignore`` must not appear in the plan."""
        (tmp_path / "auth.py").write_text("password = 'x'")
        (tmp_path / "secrets.py").write_text("api_key = 'y'")
        (tmp_path / ".advisorignore").write_text("secrets.py\n")
        state = build_app_state(tmp_path, min_priority=1)
        payload = _plan_payload(state, {})
        paths = [t["file_path"] for t in payload["tasks"]]
        assert any("auth.py" in p for p in paths)
        assert not any("secrets.py" in p for p in paths)

    def test_comma_separated_file_types(self, tmp_path):
        """``file_types=*.py,*.ts`` should match BOTH extensions."""
        (tmp_path / "a.py").write_text("x = 1")
        (tmp_path / "b.ts").write_text("const x = 1;")
        (tmp_path / "c.md").write_text("# ignore me")
        state = build_app_state(tmp_path, min_priority=1)
        payload = _plan_payload(state, {"file_types": ["*.py,*.ts"]})
        names = sorted(Path(t["file_path"]).name for t in payload["tasks"])
        assert names == ["a.py", "b.ts"]

    def test_comma_split_validates_each_piece(self, tmp_path):
        """A traversal segment in any comma-piece must be rejected."""
        state = build_app_state(tmp_path, min_priority=1)
        with pytest.raises(ValueError):
            _plan_payload(state, {"file_types": ["*.py,../etc/*"]})

    def test_rejects_windows_drive_file_type_pattern(self, tmp_path):
        state = build_app_state(tmp_path, min_priority=1)
        with pytest.raises(ValueError, match="unsafe file_types pattern"):
            _plan_payload(state, {"file_types": ["C:\\*.py"]})

    def test_rank_target_discovers_files_deterministically(self, tmp_path, monkeypatch):
        a = tmp_path / "a.py"
        b = tmp_path / "b.py"
        a.write_text("x = 1")
        b.write_text("x = 1")
        state = build_app_state(tmp_path, min_priority=1)

        monkeypatch.setattr("advisor.web.server._safe_rglob", lambda *_a, **_kw: [str(b), str(a)])

        tasks = _rank_target(state, "*.py", 1)
        assert [Path(t.file_path).name for t in tasks] == ["a.py", "b.py"]


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

    def test_cost_payload_honors_max_runners(self, tmp_path):
        for i in range(4):
            (tmp_path / f"auth{i}.py").write_text("password = 'x'\n" * 10)
        state = build_app_state(tmp_path, min_priority=1)
        payload = _cost_payload(state, {"max_runners": ["2"]})

        assert payload["task_count"] == 4
        assert payload["estimate"]["runner_count"] == 2


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

    def test_negative_limit_falls_back_to_default(self, tmp_path):
        append_entries(
            tmp_path,
            [
                HistoryEntry(
                    timestamp="2026-04-21T12:00:00+00:00",
                    file_path="a.py",
                    severity="HIGH",
                    description="one",
                    status="CONFIRMED",
                    run_id="r1",
                ),
                HistoryEntry(
                    timestamp="2026-04-21T12:00:01+00:00",
                    file_path="b.py",
                    severity="HIGH",
                    description="two",
                    status="CONFIRMED",
                    run_id="r2",
                ),
            ],
        )
        state = build_app_state(tmp_path)
        payload = _history_payload(state, {"limit": ["-5"]})
        # Invalid negatives fall back to the default window so they don't
        # silently look like "no history".
        assert payload["count"] == 2


class TestStatusPayload:
    def test_empty_when_no_history_file(self, tmp_path):
        state = build_app_state(tmp_path)
        payload = _status_payload(state)
        assert payload == {"last_mtime": None, "is_active": False}

    def test_counts_entries_and_reports_mtime(self, tmp_path):
        append_entries(
            tmp_path,
            [
                HistoryEntry(
                    timestamp="2026-04-21T12:00:00+00:00",
                    file_path="a.py",
                    severity="HIGH",
                    description="one",
                    status="CONFIRMED",
                    run_id="r1",
                ),
                HistoryEntry(
                    timestamp="2026-04-21T12:00:01+00:00",
                    file_path="b.py",
                    severity="LOW",
                    description="two",
                    status="CONFIRMED",
                    run_id="r1",
                ),
            ],
        )
        state = build_app_state(tmp_path)
        payload = _status_payload(state)
        assert payload["last_mtime"] is not None
        # Just-appended history file must read as active.
        assert payload["is_active"] is True

    def test_stale_file_is_not_active(self, tmp_path):
        """A history file older than ``_ACTIVE_WINDOW_SECONDS`` must report
        ``is_active=False`` so the LIVE pill stops pulsing after a run ends."""
        import os
        import time

        append_entries(
            tmp_path,
            [
                HistoryEntry(
                    timestamp="2026-04-21T12:00:00+00:00",
                    file_path="a.py",
                    severity="LOW",
                    description="old",
                    status="CONFIRMED",
                    run_id="r1",
                )
            ],
        )
        from advisor.history import history_path

        past = time.time() - 120  # 2 min in the past; well outside the window
        os.utime(history_path(tmp_path), (past, past))
        state = build_app_state(tmp_path)
        payload = _status_payload(state)
        assert payload["is_active"] is False


class TestTargetPayload:
    def test_shape(self, tmp_path):
        state = build_app_state(tmp_path)
        payload = _target_payload(state)
        assert payload["target"] == str(tmp_path.resolve())
        assert set(payload["defaults"]) == {
            "file_types",
            "min_priority",
            "max_runners",
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
    port = find_free_port()
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

    def test_api_status_empty(self, live_server):
        host, port, _ = live_server
        status, headers, body = _get(host, port, "/api/status")
        assert status == 200
        assert "application/json" in headers["Content-Type"]
        data = json.loads(body)
        assert data == {"last_mtime": None, "is_active": False}

    def test_api_status_reflects_writes(self, live_server):
        """After appending a finding, /api/status must report count>0, a
        non-null mtime, and is_active=True (the file was just written)."""
        host, port, tmp = live_server
        append_entries(
            tmp,
            [
                HistoryEntry(
                    timestamp="2026-04-21T12:00:00+00:00",
                    file_path="a.py",
                    severity="HIGH",
                    description="x",
                    status="CONFIRMED",
                    run_id="r1",
                )
            ],
        )
        status, _, body = _get(host, port, "/api/status")
        assert status == 200
        data = json.loads(body)
        assert data["last_mtime"] is not None
        assert data["is_active"] is True

    def test_unknown_route_returns_404_json(self, live_server):
        host, port, _ = live_server
        status, headers, body = _get(host, port, "/api/nope")
        assert status == 404
        assert "application/json" in headers["Content-Type"]
        assert "error" in json.loads(body)


# ---------------------------------------------------------------------------
# Static UI behavior
# ---------------------------------------------------------------------------


class TestStaticUiState:
    def test_findings_filter_empty_state_uses_filtered_count(self):
        assert "let findingsErrorMessage = '';" in APP_JS
        assert "const hasVisibleFindings = filtered.length !== 0;" in APP_JS
        assert "$('#findings-empty').textContent = findingsErrorMessage ||" in APP_JS
        assert "No findings match the current filters." in APP_JS
        assert "$('#findings-table').hidden = !hasVisibleFindings;" in APP_JS

    def test_plan_error_clears_stale_rows(self):
        assert "function showPlanError(message)" in APP_JS
        assert "$('#plan-table tbody').innerHTML = '';" in APP_JS
        assert "$('#plan-table').hidden = true;" in APP_JS
        assert "$('#plan-count').textContent = '';" in APP_JS
        assert "Error loading plan: network error" in APP_JS

    def test_cost_empty_resets_stale_error_message(self):
        assert "function showCostError(message)" in APP_JS
        assert "No plan yet — cost estimate needs ranked files." in APP_JS
        assert "Error loading cost: network error" in APP_JS

    def test_findings_fetch_errors_show_empty_state(self):
        assert "function showFindingsError(message)" in APP_JS
        assert "findingsErrorMessage = message;" in APP_JS
        assert "findingsErrorMessage = '';" in APP_JS
        assert "Error loading findings: network error" in APP_JS

    def test_plan_and_cost_json_parse_errors_are_handled(self):
        assert "data = await r.json();" in APP_JS
        assert "let data;" in APP_JS

    def test_cost_request_sends_max_runners(self):
        assert "qs.set('max_runners', form.get('max_runners') || '5');" in APP_JS


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

    def test_parser_accepts_port_zero(self):
        parser = build_parser()
        args = parser.parse_args(["ui", ".", "--port", "0"])
        assert args.port == 0

    def test_json_rejects_port_zero(self, tmp_path, capsys):
        parser = build_parser()
        args = parser.parse_args(["ui", str(tmp_path), "--json", "--port", "0"])
        assert cmd_ui(args) == 2
        assert "--json cannot be combined with --port 0" in capsys.readouterr().err

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
            args = parser.parse_args(["ui", str(tmp_path), "--port", str(busy_port)])
            assert cmd_ui(args) == 1
            assert "could not bind" in capsys.readouterr().err
        finally:
            sock.close()


class TestRunServerKeyboardInterrupt:
    """``run_server`` should trap Ctrl-C and exit cleanly (rc=None → 0)."""

    def _fake_server_class(self, serve_exc=KeyboardInterrupt):
        class _FakeServer:
            server_address = ("127.0.0.1", 54321)

            def __init__(self, *a, **kw):
                pass

            def serve_forever(self):
                raise serve_exc

            def server_close(self):
                pass

        return _FakeServer

    def test_keyboard_interrupt_is_caught(self, tmp_path, monkeypatch, capsys):
        state = build_app_state(tmp_path)
        monkeypatch.setattr(
            "advisor.web.server.ThreadingHTTPServer",
            lambda *a, **kw: self._fake_server_class()(),
        )
        run_server(state, port=find_free_port())
        assert "shutting down" in capsys.readouterr().out


class TestRunServerPortValidation:
    """Port inputs that would normally crash deep in the socket layer should
    surface as a clean :class:`OSError` from :func:`run_server`."""

    @pytest.mark.parametrize("bad_port", [-1, 65536, 99999])
    def test_rejects_out_of_range(self, tmp_path, bad_port):
        state = build_app_state(tmp_path)
        with pytest.raises(OSError, match="could not bind"):
            run_server(state, port=bad_port)

    def test_reports_actual_bound_port_when_zero(self, tmp_path, monkeypatch, capsys):
        """When ``--port 0`` is used, the URL printed must reflect the port
        the OS actually assigned, not the literal 0."""
        state = build_app_state(tmp_path)

        # Build a real ThreadingHTTPServer bound to port 0, then swap it in
        # through a factory so run_server treats it as its server. We raise
        # KeyboardInterrupt immediately so serve_forever returns.
        from http.server import ThreadingHTTPServer

        from advisor.web.server import _make_handler_class

        real_handler = _make_handler_class(state, log_requests=False)
        real_server = ThreadingHTTPServer(("127.0.0.1", 0), real_handler)
        bound_port = real_server.server_address[1]

        class _OneShot:
            server_address = real_server.server_address

            def __init__(self, *a, **kw):
                pass

            def serve_forever(self):
                raise KeyboardInterrupt

            def server_close(self):
                real_server.server_close()

        monkeypatch.setattr("advisor.web.server.ThreadingHTTPServer", lambda *a, **kw: _OneShot())
        run_server(state, port=0)
        out = capsys.readouterr().out
        assert f":{bound_port}" in out
        assert ":0\n" not in out


class TestErrorHandler:
    """Unhandled exceptions should log a traceback and return a generic 500."""

    def test_generic_error_body(self, tmp_path, monkeypatch, caplog):
        state = build_app_state(tmp_path)

        # Force one of the handlers to blow up with a distinctive exception
        # whose message should NOT leak into the response body.
        def boom(*a, **kw):
            raise RuntimeError("supersecret internal detail")

        monkeypatch.setattr("advisor.web.server._target_payload", boom)

        from http.server import ThreadingHTTPServer

        from advisor.web.server import _make_handler_class

        handler_cls = _make_handler_class(state, log_requests=False)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with caplog.at_level("ERROR", logger="advisor.web.server"):
                status, _, body = _get("127.0.0.1", port, "/api/target")
            assert status == 500
            data = json.loads(body)
            assert data["error"] == "internal server error"
            assert "supersecret" not in body.decode()
            # The traceback must still reach the server-side log so devs can debug.
            assert any("supersecret" in rec.message for rec in caplog.records)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_filesystem_error_body(self, tmp_path, monkeypatch, caplog):
        state = build_app_state(tmp_path)

        def boom(*a, **kw):
            raise OSError("filesystem detail")

        monkeypatch.setattr("advisor.web.server._target_payload", boom)

        from http.server import ThreadingHTTPServer

        from advisor.web.server import _make_handler_class

        handler_cls = _make_handler_class(state, log_requests=False)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with caplog.at_level("ERROR", logger="advisor.web.server"):
                status, _, body = _get("127.0.0.1", port, "/api/target")
            assert status == 500
            data = json.loads(body)
            assert data["error"] == "internal server error"
            assert "filesystem detail" not in body.decode()
            assert any("filesystem error serving" in rec.message for rec in caplog.records)
            assert any("filesystem detail" in rec.message for rec in caplog.records)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
