"""Local-only HTTP server for the optional dashboard.

Pure stdlib — :mod:`http.server` + :mod:`json` — so enabling the dashboard
doesn't add a single line to ``pip install advisor-agent``. The server binds
to loopback by default (``127.0.0.1``) because it exposes read-only views of
files under the target directory and should not be reachable from other hosts.

Endpoints
---------

* ``GET /``                  — single-page dashboard HTML
* ``GET /static/app.css``    — styles
* ``GET /static/app.js``     — client logic
* ``GET /api/target``        — the target directory the server is bound to
* ``GET /api/history``       — recent findings from ``.advisor/history.jsonl``
* ``GET /api/plan``          — ranked files (same data the ``plan`` command emits)
* ``GET /api/cost``          — token/cost estimate for the current plan

All responses are JSON (application/json) except static assets.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..cost import estimate_cost
from ..focus import create_focus_tasks
from ..history import HISTORY_SCHEMA_VERSION, load_recent
from ..rank import CONTENT_SCAN_LIMIT, rank_files
from .assets import APP_CSS, APP_JS, INDEX_HTML

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


@dataclass(frozen=True, slots=True)
class AppState:
    """Immutable bundle of the target directory and default run config.

    Passed into each request handler via a class attribute on a dynamically
    subclassed :class:`BaseHTTPRequestHandler` so request handling stays
    stateless and thread-safe.
    """

    target: Path
    default_file_types: str = "*.py"
    default_min_priority: int = 3
    default_advisor_model: str = "opus"
    default_runner_model: str = "sonnet"


def build_app_state(
    target: str | Path,
    *,
    file_types: str = "*.py",
    min_priority: int = 3,
    advisor_model: str = "opus",
    runner_model: str = "sonnet",
) -> AppState:
    """Resolve and validate the target directory, then freeze defaults."""
    path = Path(target).resolve()
    if not path.exists():
        raise FileNotFoundError(f"target not found: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"target is not a directory: {path}")
    return AppState(
        target=path,
        default_file_types=file_types,
        default_min_priority=min_priority,
        default_advisor_model=advisor_model,
        default_runner_model=runner_model,
    )


def _read_head(path: str, limit: int = CONTENT_SCAN_LIMIT) -> str:
    try:
        return Path(path).read_text(errors="ignore")[:limit]
    except OSError:
        return ""


def _safe_rglob(target: Path, pattern: str) -> list[str]:
    try:
        return [str(p) for p in target.rglob(pattern) if p.is_file()]
    except (OSError, ValueError):
        return []


def _first(qs: dict[str, list[str]], key: str, default: str) -> str:
    vals = qs.get(key)
    if vals and vals[0]:
        return vals[0]
    return default


def _first_int(qs: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int(_first(qs, key, str(default)))
    except ValueError:
        return default


def _plan_payload(state: AppState, qs: dict[str, list[str]]) -> dict[str, Any]:
    file_types = _first(qs, "file_types", state.default_file_types)
    min_priority = _first_int(qs, "min_priority", state.default_min_priority)
    paths = _safe_rglob(state.target, file_types)
    ranked = rank_files(paths, read_fn=_read_head)
    tasks = create_focus_tasks(ranked, max_tasks=None, min_priority=min_priority)
    return {
        "target": str(state.target),
        "file_types": file_types,
        "min_priority": min_priority,
        "task_count": len(tasks),
        "tasks": [{"file_path": t.file_path, "priority": t.priority} for t in tasks],
    }


def _cost_payload(state: AppState, qs: dict[str, list[str]]) -> dict[str, Any]:
    advisor_model = _first(qs, "advisor_model", state.default_advisor_model)
    runner_model = _first(qs, "runner_model", state.default_runner_model)
    max_fixes = _first_int(qs, "max_fixes_per_runner", 5)
    file_types = _first(qs, "file_types", state.default_file_types)
    min_priority = _first_int(qs, "min_priority", state.default_min_priority)
    paths = _safe_rglob(state.target, file_types)
    ranked = rank_files(paths, read_fn=_read_head)
    tasks = create_focus_tasks(ranked, max_tasks=None, min_priority=min_priority)
    if not tasks:
        return {
            "target": str(state.target),
            "advisor_model": advisor_model,
            "runner_model": runner_model,
            "task_count": 0,
            "estimate": None,
        }
    estimate = estimate_cost(
        tasks,
        None,
        advisor_model=advisor_model,
        runner_model=runner_model,
        max_fixes_per_runner=max_fixes,
    )
    return {
        "target": str(state.target),
        "advisor_model": advisor_model,
        "runner_model": runner_model,
        "task_count": len(tasks),
        "estimate": estimate.to_dict(),
    }


def _history_payload(state: AppState, qs: dict[str, list[str]]) -> dict[str, Any]:
    limit = _first_int(qs, "limit", 100)
    entries = load_recent(state.target, limit=limit)
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "target": str(state.target),
        "count": len(entries),
        "entries": [
            {
                "timestamp": e.timestamp,
                "file_path": e.file_path,
                "severity": e.severity,
                "description": e.description,
                "status": e.status,
                "run_id": e.run_id,
            }
            for e in entries
        ],
    }


def _target_payload(state: AppState) -> dict[str, Any]:
    return {
        "target": str(state.target),
        "defaults": {
            "file_types": state.default_file_types,
            "min_priority": state.default_min_priority,
            "advisor_model": state.default_advisor_model,
            "runner_model": state.default_runner_model,
        },
    }


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP handler wired to an :class:`AppState` attached by :func:`run_server`.

    Subclasses override :attr:`state` before the server starts. ``do_GET`` is
    the only verb handled — the dashboard is read-only.
    """

    state: AppState
    # Silences the default one-line-per-request access log; users running this
    # in the foreground don't want their terminal spammed. Override via
    # ``log_requests=True`` in :func:`run_server` if you need it for debugging.
    log_requests_enabled: bool = False

    def log_message(self, format: str, *args: Any) -> None:
        if self.log_requests_enabled:
            super().log_message(format, *args)

    # --- response helpers -------------------------------------------------
    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json({"error": message}, status=status)

    # --- routing ----------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if route == "/" or route == "/index.html":
                self._send_text(INDEX_HTML, "text/html")
                return
            if route == "/static/app.css":
                self._send_text(APP_CSS, "text/css")
                return
            if route == "/static/app.js":
                self._send_text(APP_JS, "application/javascript")
                return
            if route == "/api/target":
                self._send_json(_target_payload(self.state))
                return
            if route == "/api/history":
                self._send_json(_history_payload(self.state, qs))
                return
            if route == "/api/plan":
                self._send_json(_plan_payload(self.state, qs))
                return
            if route == "/api/cost":
                self._send_json(_cost_payload(self.state, qs))
                return
            self._send_error(HTTPStatus.NOT_FOUND, f"no route {route!r}")
        except BrokenPipeError:
            # Client hung up while we were writing — common when switching
            # tabs in the browser. Nothing actionable.
            return
        except Exception as exc:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")


def _make_handler_class(state: AppState, log_requests: bool) -> type[DashboardHandler]:
    """Bind a fresh ``AppState`` onto a subclass so each server instance is isolated.

    Using a subclass rather than an instance attribute avoids the
    ``BaseHTTPRequestHandler`` constructor signature (it expects to be
    instantiated by the server itself, one per request).
    """

    class _Bound(DashboardHandler):
        pass

    _Bound.state = state
    _Bound.log_requests_enabled = log_requests
    return _Bound


def run_server(
    state: AppState,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    log_requests: bool = False,
) -> None:
    """Block and serve the dashboard until Ctrl-C.

    Raises :class:`OSError` if the port is already bound — we refuse to
    fall through to an alternate port silently because that would make the
    user's ``advisor ui`` invocation non-reproducible.
    """
    handler_cls = _make_handler_class(state, log_requests=log_requests)
    try:
        server = ThreadingHTTPServer((host, port), handler_cls)
    except OSError as exc:
        raise OSError(f"could not bind {host}:{port} ({exc})") from exc

    url = f"http://{host}:{port}"
    print(f"advisor dashboard serving {state.target} at {url}")
    print("press Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        print("shutting down")
    finally:
        server.server_close()


def find_free_port(host: str = DEFAULT_HOST, start: int = DEFAULT_PORT, tries: int = 20) -> int:
    """Scan forward from ``start`` for the first unused port.

    Only used by tests and by the CLI when ``--port 0`` is requested. The
    user-facing default is a fixed port so invocations are reproducible.
    """
    for candidate in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, candidate))
            except OSError:
                continue
            return candidate
    raise OSError(f"no free port in range {start}..{start + tries}")
