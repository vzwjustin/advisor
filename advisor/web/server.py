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
* ``GET /api/events``        — live event stream from ``.advisor/live/events.jsonl``

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
from ..live import LIVE_SCHEMA_VERSION, latest_seq, live_events_path, load_recent_events
from ..orchestrate.config import POOL_SIZE_CEILING
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


def _display_host(host: str) -> str:
    """Return a URL-safe rendering of ``host`` for the startup banner.

    Sanitize with an allow-list of characters legal in a URL host component
    (alphanumerics, ``.``, ``-``, ``_``, ``:``, ``[``, ``]``). Rejects C0 / C1
    control bytes (ESC, CR, LF, the C1 CSI ``\\x9b``) AND
    printable-but-URL-significant bytes like space, ``@``, ``?``, ``#`` that
    would otherwise produce a banner URL whose authority a browser parses
    differently than the bound socket. Bare IPv6 addresses get wrapped in
    ``[...]`` so the ``:<port>`` suffix isn't read as another IPv6 colon
    segment.

    NOTE: display only — the bind already happened with the original
    ``host`` string and either succeeded or failed on its own merits.
    """
    safe = "".join(ch for ch in host if ch.isalnum() or ch in "._-:[]")
    # Bare IPv6 (e.g. ``::1`` from ``--host ::1``) needs bracket-wrapping
    # so the ``:<port>`` suffix isn't misread as another IPv6 segment.
    # IPv4 contains no colons, so ``":" in safe`` reliably distinguishes
    # the two; skip strings that already carry brackets.
    if ":" in safe and not safe.startswith("["):
        return f"[{safe}]"
    return safe


def _first(qs: dict[str, list[str]], key: str, default: str) -> str:
    vals = qs.get(key)
    if vals and vals[0]:
        return vals[0]
    return default


def _first_int(
    qs: dict[str, list[str]],
    key: str,
    default: int,
    *,
    min_value: int = 0,
    max_value: int | None = None,
) -> int:
    """Parse a query-string int with a lower (and optional upper) bound.

    Negative or malformed values fall back to ``default``. The returned
    value is clamped to at least ``min_value`` so a caller that
    accidentally passes an invalid ``default`` (e.g. ``default=-1,
    min_value=1``) still gets a legal result rather than propagating the
    bug downstream. When ``max_value`` is supplied, values above it are
    clamped down — preventing an unbounded query-string int from flowing
    into cost-estimation math (``estimate_cost(max_runners=10**18)``)
    and producing nonsensical results.
    """

    def _clamp(value: int) -> int:
        value = max(value, min_value)
        if max_value is not None and value > max_value:
            return max_value
        return value

    try:
        value = int(_first(qs, key, str(default)))
    except ValueError:
        return _clamp(default)
    if value < min_value:
        return _clamp(default)
    return _clamp(value)


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
    # Cap query-string ints so an unbounded value can't flow into estimate_cost
    # math (e.g. max_runners=10**18) and produce nonsensical float output. The
    # max_runners ceiling matches the CLI/env-var path (POOL_SIZE_CEILING). The
    # max_fixes ceiling is generous — well above any real runner config — but
    # bounded so the cost-estimate divisor stays finite.
    max_runners = _first_int(
        qs, "max_runners", state.default_max_runners, min_value=1, max_value=POOL_SIZE_CEILING
    )
    max_fixes = _first_int(qs, "max_fixes_per_runner", 5, min_value=0, max_value=100)
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
    limit = _first_int(qs, "limit", 100, min_value=1, max_value=1000)
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


def _events_payload(state: AppState, qs: dict[str, list[str]]) -> dict[str, Any]:
    """Return live events for the dashboard's Live tab.

    Cursor-based: ``?since=<seq>`` returns events strictly newer than that
    cursor. The first poll (no ``since``) returns the tail of the file so
    the client immediately sees recent activity. Subsequent polls advance
    the cursor via the ``next_token`` field in the response.

    ``?limit=N`` caps the per-poll batch. The default of 200 is generous
    for a 2-second poll interval and well below the in-memory cap inside
    :func:`load_recent_events`.

    Always returns ``next_token`` (the highest seq in the file or 0 when
    empty) so the client's cursor can advance even when the response has
    no events — otherwise an idle gap would leave the cursor at its last
    non-empty value and replay the same tail on every poll once activity
    resumes.
    """
    # ``since=-1`` (or any negative) collapses to "from the start" by
    # leaving ``since`` as None — _first_int's min_value guards against
    # accidentally pinning the cursor below zero.
    since_raw = _first(qs, "since", "")
    since: int | None
    if since_raw == "":
        since = None
    else:
        try:
            since = max(0, int(since_raw))
        except (TypeError, ValueError):
            since = None
    limit = _first_int(qs, "limit", 200, min_value=1, max_value=1000)
    events = load_recent_events(state.target, since=since, limit=limit)
    # next_token is the max seq seen on disk, NOT the max in the returned
    # window — the client polls with this token next time, even if the
    # current response was clipped by ``limit``. Pass-through ``since``
    # when the file is currently empty (no events) so the client keeps
    # its prior cursor instead of resetting to 0.
    file_latest = latest_seq(state.target)
    next_token = file_latest if file_latest > 0 else (since if since is not None else 0)
    return {
        "schema_version": LIVE_SCHEMA_VERSION,
        "target": str(state.target),
        "count": len(events),
        "events": events,
        "next_token": next_token,
    }


def _status_payload(state: AppState) -> dict[str, Any]:
    """Cheap "has anything changed?" probe for the live dashboard poller.

    Returns the history file's mtime (stable cache key for the client's
    ``lastMtime`` check), a composite ``token`` field combining
    ``st_mtime_ns`` and ``st_size`` for higher-resolution change
    detection, and an ``is_active`` flag that reflects EITHER store
    being modified in the last :data:`_ACTIVE_WINDOW_SECONDS`.

    Why ``is_active`` considers both files: during a live ``/advisor``
    run the team-lead writes to ``.advisor/live/events.jsonl`` (which
    the Live tab polls) but NOT to ``history.jsonl`` (which only takes
    CONFIRMED findings after the run wraps). The prior implementation
    read only ``history.jsonl``'s mtime, so the Findings tab's LIVE
    pill stayed IDLE during an active run even when the Live tab was
    buzzing — users saw the disconnect and concluded "Findings isn't
    reflecting live data". Including both signals makes the indicator
    honest: it says "advisor is doing something on this target".

    The ``last_mtime`` / ``token`` fields remain history-only because
    the Findings tab's ``refetchFindings`` only needs to fire when the
    history list itself changes; new live events without new confirmed
    findings shouldn't trigger a redundant ``/api/history`` refetch.

    ``live_*`` keys are new in this revision — older client builds
    that ignore them keep working (they still have ``last_mtime`` /
    ``token`` / ``is_active``); newer client builds can render a
    "run in progress" hint by checking ``live_is_active``.

    Intentionally does not read file contents — the whole point is that
    this endpoint fires every few seconds and must be nearly free.

    ``token`` is preferred by the client for change detection because
    nanosecond precision survives the same-microsecond-write edge case
    that ``last_mtime``'s ISO-microsecond rendering can collapse, and
    the ``st_size`` tiebreaker also catches the (rare) case of a file
    rewrite that lands on the same timestamp. ``last_mtime`` is kept
    for the human-readable banner and as a fallback for older clients.
    """
    now = datetime.now(timezone.utc)

    def _read(p: Path) -> tuple[str | None, str | None, bool]:
        """Return (iso_mtime, token, is_active) for one file."""
        if not p.exists():
            return None, None, False
        try:
            stat = p.stat()
        except OSError:
            return None, None, False
        mtime_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        # Clamp to 0 because on Windows the NTFS mtime resolution
        # (100 ns) is finer than ``time.time()`` rounding, which can
        # leave ``st_mtime`` microseconds in the "future" of
        # ``datetime.now()`` for a file we just wrote — yielding a tiny
        # negative age and mis-reporting the LIVE pill as inactive. A
        # just-touched file is always active.
        age = max(0.0, (now - mtime_dt).total_seconds())
        return (
            mtime_dt.isoformat(),
            f"{stat.st_mtime_ns}:{stat.st_size}",
            age < _ACTIVE_WINDOW_SECONDS,
        )

    h_mtime, h_token, h_active = _read(history_path(state.target))
    l_mtime, l_token, l_active = _read(live_events_path(state.target))
    return {
        # Back-compat fields — Findings tab change-detection still
        # keys on history alone so a flurry of live events without
        # new confirmed findings doesn't trigger redundant refetches.
        "last_mtime": h_mtime,
        "token": h_token,
        # ``is_active`` reflects EITHER store being warm — drives the
        # Findings tab's LIVE pill so it agrees with the Live tab
        # during a /advisor run.
        "is_active": h_active or l_active,
        # New per-store fields — let smarter clients distinguish
        # "findings being confirmed" from "live events firing".
        "history_is_active": h_active,
        "live_is_active": l_active,
        "live_mtime": l_mtime,
        "live_token": l_token,
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
            "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        # ``_response_committed`` flags that headers were flushed and any
        # subsequent error should not attempt a second ``send_response``;
        # do_GET's outer except handlers consult this before falling
        # back to ``_send_error``.
        self._response_committed = True
        self.wfile.write(body)

    def _send_text(self, body: str, content_type: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self._response_committed = True
        self.wfile.write(data)

    def _send_error(self, status: int, message: str) -> None:
        # Refuse to write a second response on a connection that already
        # flushed headers. The send_response/end_headers pair would
        # otherwise inject a fresh status line into the body of the
        # half-written response and corrupt the HTTP stream.
        if getattr(self, "_response_committed", False):
            return
        self._send_json({"error": message}, status=status)

    # --- routing ----------------------------------------------------------
    def do_GET(self) -> None:
        # Reset the per-request response-committed flag. The flag is
        # consulted by ``_send_error`` so an exception raised after
        # headers were flushed does not produce a second ``send_response``
        # and corrupt the HTTP stream.
        self._response_committed = False
        # DNS-rebinding defense: reject any request whose Host header is not
        # the loopback address this server bound to. A remote page cannot read
        # our API endpoints after rebinding attacker.example→127.0.0.1 because
        # the browser sends Host: attacker.example:<port>, which is rejected here.
        # ``BaseServer.server_address`` is a typeshed union; for the TCP-based
        # ``HTTPServer`` we use, it's always a ``(host, port)`` tuple.
        _server_addr = self.server.server_address
        assert isinstance(_server_addr, tuple), "TCPServer.server_address must be a tuple"
        _bound_port = _server_addr[1]
        _host_header = self.headers.get("Host", "")
        _allowed_hosts = {
            f"127.0.0.1:{_bound_port}",
            f"localhost:{_bound_port}",
            f"[::1]:{_bound_port}",
        }
        if _host_header not in _allowed_hosts:
            self._send_error(HTTPStatus.FORBIDDEN, "forbidden")
            return
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
            if route == "/api/events":
                self._send_json(_events_payload(self.state, qs))
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
    quiet: bool = False,
) -> None:
    """Block and serve the dashboard until Ctrl-C.

    Raises :class:`OSError` on bind failure — including when ``port`` is out
    of the valid 0..65535 range. We intentionally do NOT fall through to an
    alternate port on EADDRINUSE because that would make ``advisor ui``
    invocations non-reproducible; passing ``port=0`` is the opt-in to let
    the OS pick, and the bound port is printed after the fact.

    ``host`` must be a loopback address (``127.0.0.1``, ``localhost``,
    ``::1``, ``[::1]``). Wildcard and routable binds are rejected because the
    dashboard exposes read-only local project state and the DNS-rebinding
    allowlist is loopback-only. Surfacing the constraint at bind time beats a
    silently-403-every-request server that looks like it's running fine.
    """
    if not isinstance(port, int) or isinstance(port, bool):
        # Reject non-int (e.g. ``None`` from a caller bug) before the
        # range check so the comparison doesn't raise ``TypeError`` —
        # the docstring promises ``OSError`` on bad ports. ``bool`` is
        # an ``int`` subclass and would silently accept ``True``/``False``;
        # explicitly reject it.
        raise OSError(f"could not bind {host}:{port} (port must be an integer)")
    if not 0 <= port <= 65535:
        raise OSError(f"could not bind {host}:{port} (port out of range 0..65535)")
    # DNS-rebinding defense's Host allowlist is loopback-only. Binding to a
    # routable or wildcard interface would either expose local state to the
    # network or silently 403 normal remote requests. Reject those binds at
    # entry so the documented loopback-only policy is the actual behavior.
    _ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
    if host not in _ALLOWED_BIND_HOSTS:
        raise OSError(
            f"refusing to bind non-loopback host {host!r}: only loopback "
            f"binds are supported. Pass one of {sorted(_ALLOWED_BIND_HOSTS)}."
        )

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
    url = f"http://{_display_host(host)}:{actual_port}"
    if not quiet:
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
    """Return a free port assigned by the OS — test-only helper.

    Binds with port 0 so the kernel picks an available port without the
    scanning race of probing candidates by hand. The returned port is
    then released (the socket is closed on ``with`` exit before return),
    leaving a brief use-after-close window during which another process
    can bind the same port before the caller does — on a busy host or
    CI machine, this manifests as a flaky ``EADDRINUSE`` on the caller's
    subsequent bind.

    For production code, prefer the race-free pattern used by
    :func:`run_server`: pass ``port=0`` directly to
    ``ThreadingHTTPServer((host, 0), ...)`` and read the bound port
    back from ``server.server_address[1]`` — the kernel holds the port
    on the live server socket without an intervening release.

    This helper is used **only by tests** (``tests/test_web.py``); the
    CLI's ``--port 0`` path runs through :func:`run_server` instead. The
    function is kept for the test suite's convenience but is not
    appropriate for new callers.
    """
    # Resolve the address family from ``host`` so an IPv6 literal (``::1``)
    # picks an IPv6 free port; previously hardcoded ``AF_INET`` always
    # returned an IPv4 port even for IPv6 hosts.
    family = socket.AF_INET
    try:
        infos = socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)
        if infos:
            family = infos[0][0]
    except OSError:
        pass
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        port: int = sock.getsockname()[1]
        return port
