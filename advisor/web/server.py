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
import logging
import socket
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .. import _style
from .._fs import read_head as _read_head
from .._fs import safe_rglob_paths as _safe_rglob
from .._fs import validate_file_types as _validate_file_types
from ..cost import estimate_cost
from ..focus import FocusTask, create_focus_tasks
from ..history import HISTORY_SCHEMA_VERSION, history_path, load_recent
from ..rank import load_advisorignore, rank_files
from .assets import APP_CSS, APP_JS, INDEX_HTML

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# Window during which the history file's mtime is considered "fresh" and the
# dashboard paints an ACTIVE indicator. 15s is a balance: long enough that the
# LIVE pill doesn't flicker between a runner's finds, short enough that a
# completed run stops looking active within one UI tick or two.
_ACTIVE_WINDOW_SECONDS = 15.0


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
    default_max_runners: int = 5
    default_advisor_model: str = "opus"
    default_runner_model: str = "sonnet"


def build_app_state(
    target: str | Path,
    *,
    file_types: str = "*.py",
    min_priority: int = 3,
    max_runners: int = 5,
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
        default_max_runners=max(1, max_runners),
        default_advisor_model=advisor_model,
        default_runner_model=runner_model,
    )


def _first(qs: dict[str, list[str]], key: str, default: str) -> str:
    vals = qs.get(key)
    if vals and vals[0]:
        return vals[0]
    return default


def _first_int(qs: dict[str, list[str]], key: str, default: int, *, min_value: int = 0) -> int:
    """Parse a query-string int with a lower bound.

    Negative or malformed values fall back to ``default`` — downstream
    handlers assume non-negative counts/limits and sanity-clamp the
    upper bound separately. The returned value is also clamped to at
    least ``min_value`` so a caller that accidentally passes an invalid
    ``default`` (e.g. ``default=-1, min_value=1``) still gets a legal
    result rather than propagating the bug downstream.
    """
    try:
        value = int(_first(qs, key, str(default)))
    except ValueError:
        return max(default, min_value)
    if value < min_value:
        return max(default, min_value)
    return value


def _rank_target(state: AppState, file_types: str, min_priority: int) -> list[FocusTask]:
    """Discover + rank + filter files under the target, honoring ``.advisorignore``.

    Shared by the ``/api/plan`` and ``/api/cost`` handlers so they can't
    drift on which files are in scope. ``file_types`` may be a comma-
    separated list (e.g. ``*.py,*.ts``); each piece is expanded
    independently and the results are merged + deduped, matching the
    CLI's ``--file-types`` behavior.
    """
    min_priority = max(1, min(5, min_priority))
    _validate_file_types(file_types)
    patterns = [p.strip() for p in file_types.split(",") if p.strip()]
    seen: set[str] = set()
    paths: list[str] = []
    for pat in patterns:
        for fp in _safe_rglob(state.target, pat):
            if fp not in seen:
                seen.add(fp)
                paths.append(fp)
    ignore_patterns = load_advisorignore(state.target)
    ranked = rank_files(paths, read_fn=_read_head, ignore_patterns=ignore_patterns)
    return create_focus_tasks(ranked, max_tasks=None, min_priority=min_priority)


def _plan_payload(state: AppState, qs: dict[str, list[str]]) -> dict[str, Any]:
    file_types = _first(qs, "file_types", state.default_file_types)
    min_priority = _first_int(qs, "min_priority", state.default_min_priority)
    tasks = _rank_target(state, file_types, min_priority)
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
    max_runners = _first_int(qs, "max_runners", state.default_max_runners, min_value=1)
    max_fixes = _first_int(qs, "max_fixes_per_runner", 5, min_value=1)
    file_types = _first(qs, "file_types", state.default_file_types)
    min_priority = _first_int(qs, "min_priority", state.default_min_priority)
    tasks = _rank_target(state, file_types, min_priority)
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
        max_runners=max_runners,
    )
    return {
        "target": str(state.target),
        "advisor_model": advisor_model,
        "runner_model": runner_model,
        "task_count": len(tasks),
        "estimate": estimate.to_dict(),
    }


def _history_payload(state: AppState, qs: dict[str, list[str]]) -> dict[str, Any]:
    # Cap to 1000 to keep single responses bounded — an unbounded ?limit=
    # could otherwise be used to exhaust memory on a large history file.
    # Also reject non-positive values so invalid limits fall back to the
    # default window instead of silently looking like "no history" in the UI.
    limit = min(_first_int(qs, "limit", 100, min_value=1), 1000)
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


def _status_payload(state: AppState) -> dict[str, Any]:
    """Cheap "has anything changed?" probe for the live dashboard poller.

    Returns the file's mtime (stable cache key for the client's
    ``lastMtime`` check) and whether the file was touched in the last
    :data:`_ACTIVE_WINDOW_SECONDS` (drives the ``LIVE`` indicator).
    Intentionally does not read file contents — the whole point is that
    this endpoint fires every few seconds and must be nearly free.
    """
    path = history_path(state.target)
    empty: dict[str, Any] = {"last_mtime": None, "is_active": False}
    if not path.exists():
        return empty
    try:
        stat = path.stat()
    except OSError:
        return empty
    mtime_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    # Clamp to 0 because on Windows the NTFS mtime resolution (100 ns) is
    # finer than ``time.time()`` rounding, which can leave ``st_mtime``
    # microseconds in the "future" of ``datetime.now()`` for a file we
    # just wrote — yielding a tiny negative age and mis-reporting the
    # LIVE pill as inactive. A just-touched file is always active.
    age_seconds = max(0.0, (datetime.now(timezone.utc) - mtime_dt).total_seconds())
    return {
        "last_mtime": mtime_dt.isoformat(),
        "is_active": age_seconds < _ACTIVE_WINDOW_SECONDS,
    }


def _target_payload(state: AppState) -> dict[str, Any]:
    return {
        "target": str(state.target),
        "defaults": {
            "file_types": state.default_file_types,
            "min_priority": state.default_min_priority,
            "max_runners": state.default_max_runners,
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
        # ``ensure_ascii=False`` keeps non-ASCII characters readable in the
        # browser (UTF-8 is declared in the Content-Type) — matches the
        # convention used by ``HistoryEntry.to_json_line``.
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        # CSP on JSON responses too — defense in depth in case a browser
        # sniffs and renders the JSON as HTML despite the nosniff header.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; base-uri 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; form-action 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
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
            if route in ("/", "/index.html"):
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
            if route == "/api/status":
                self._send_json(_status_payload(self.state))
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
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except OSError:
            # Filesystem races (deleted target, permission changes, stale
            # symlink) are operational server failures. Keep the response
            # generic while preserving details in logs.
            logger.error("filesystem error serving %s\n%s", route, traceback.format_exc())
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")
        except Exception:
            # Full traceback goes to the server log so developers can
            # debug. The response body carries only a generic message —
            # even on localhost, avoiding raw exception text keeps the
            # surface narrow if someone tunnels the port.
            logger.error("unhandled error serving %s\n%s", route, traceback.format_exc())
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal server error")


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

    Raises :class:`OSError` on bind failure — including when ``port`` is out
    of the valid 0..65535 range. We intentionally do NOT fall through to an
    alternate port on EADDRINUSE because that would make ``advisor ui``
    invocations non-reproducible; passing ``port=0`` is the opt-in to let
    the OS pick, and the bound port is printed after the fact.
    """
    if not 0 <= port <= 65535:
        raise OSError(f"could not bind {host}:{port} (port out of range 0..65535)")

    handler_cls = _make_handler_class(state, log_requests=log_requests)
    try:
        server = ThreadingHTTPServer((host, port), handler_cls)
    except (OSError, OverflowError) as exc:
        # ``OverflowError`` is what ``socket.bind`` raises for ports that
        # are integer-valid but outside the 0..65535 range — wrap it so
        # the CLI's OSError-handling path catches it uniformly.
        raise OSError(f"could not bind {host}:{port} ({exc})") from exc

    # When the caller passed port=0 the OS picked an ephemeral port; the
    # ``server_address`` tuple is the only reliable source for what we
    # actually bound to, so report that instead of the request value.
    actual_port = server.server_address[1]
    # Sanitize the user-supplied host before echoing into the printed
    # URL. Allow-list rather than blocklist: only characters legal in a
    # URL host component (alphanumerics, ``.``, ``-``, ``_``, ``:``,
    # ``[``, ``]``) survive. This rejects C0/C1 control bytes (ESC, CR,
    # LF, the C1 CSI ``\x9b``) AND printable-but-URL-significant bytes
    # like space, ``@``, ``?``, ``#`` that would otherwise produce a
    # banner URL whose authority a browser parses differently than the
    # bound socket. NOTE: banner only — the bind above used the original
    # ``host`` string and either succeeded or failed on its own merits.
    host_safe = "".join(ch for ch in host if ch.isalnum() or ch in "._-:[]")
    url = f"http://{host_safe}:{actual_port}"
    print(_style.success_box(f"advisor dashboard serving {state.target} at {url}"))
    print(_style.tip("press Ctrl-C to stop"))
    print(_style.cta("open in browser", url))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
        print(_style.info_box("shutting down"))
    finally:
        server.server_close()


def find_free_port(host: str = DEFAULT_HOST) -> int:
    """Return a free port assigned by the OS.

    Binds with port 0 so the kernel picks an available port atomically,
    eliminating the bind-close-return TOCTOU race of scanning candidates.

    Only used by tests and by the CLI when ``--port 0`` is requested.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        port: int = sock.getsockname()[1]
        return port
