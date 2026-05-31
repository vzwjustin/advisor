"""Ephemeral live-events log for the optional web dashboard.

Separate from :mod:`advisor.history` by design. ``history.jsonl`` is the
authoritative store for CONFIRMED findings — it drives analytics, the
ranker boost, the SARIF emitter, and grouped repeat-finding detection.
``live/events.jsonl`` is an *ephemeral feed*: things that happened during
the run, written by the team-lead Claude session at run-start, on every
report relay, and at run-end, so the dashboard can render a real-time
view without coupling to Claude Code's internal team-mailbox protocol.

The two stores must NOT be merged. History rows are typed findings with
allowlisted severity/status, a normalized file path, and a hash-based
grouping key. Live events are free-form ``{ts, seq, kind, data}``
records whose ``data`` payload is opaque to advisor — the dashboard
renders whatever the team-lead emitted, no schema enforcement on the
read path.

File layout::

    <target>/.advisor/live/events.jsonl

The ``live/`` subdirectory is its own namespace so future event-stream
features (cost ticks, runner-budget telemetry) can sit alongside without
crowding the top-level ``.advisor/`` directory. The file is JSONL
(one JSON object per line) for the same reason history is:
append-friendly, partial reads survive truncation, and a single bad
line is skipped without losing the rest.

Schema (additive, version-stamped)::

    {
      "schema_version": "1.0",
      "ts":             "2026-05-26T17:41:57.892Z",  // ISO-8601 UTC, ms precision
      "seq":            42,                           // monotonic per-file counter
      "kind":           "run_start" | "runner_spawn" | "report_relay" | "fix_dispatch" | "run_end" | <free-form>,
      "data":           {...},                        // opaque payload — dashboard renders raw
    }

``seq`` is the cursor the dashboard polls with: ``?since=<seq>`` returns
events whose ``seq`` is strictly greater. Computing ``seq`` requires a
single tail-read of the existing file before each append, which is fine
because ``append_event`` is called at most a few times per run (not on
every token, not per LLM call).

Caps mirror :mod:`advisor.history`:

* :data:`_MAX_LINE` — single-line byte cap (refuse to read a line longer
  than this). Defends against an out-of-control writer crashing the
  dashboard.
* :data:`_MAX_TAIL` — most events ``load_recent_events`` will return
  in one call. The dashboard's polling cadence (every 2s) plus event
  emission frequency (per relay) keeps the working set well under this.
"""

from __future__ import annotations

import json
import warnings
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, cast

# Cross-module reuse of the history append-locking primitives. ``live.py``
# and ``history.py`` both append to JSONL files where concurrent appenders
# must not interleave: history can't tolerate partial JSON lines, and live
# can't tolerate duplicate ``seq`` values (the dashboard's ``?since=<seq>``
# cursor breaks if two appenders pick the same next-seq). The lock idiom is
# identical; the helpers stay private in ``history.py`` but the package
# itself is the right boundary for this reuse.
from .history import _lock_exclusive, _unlock_exclusive

UTC = timezone.utc

LIVE_DIR_NAME = ".advisor"
LIVE_SUBDIR = "live"
LIVE_FILE_NAME = "events.jsonl"
LIVE_SCHEMA_VERSION = "1.0"

# Per-line size cap. A misbehaving writer (e.g. pasted megabyte transcript
# into ``data``) shouldn't OOM the dashboard reader. 64 KiB matches the
# per-line cap in :mod:`advisor.history` so operators only memorize one
# number across both stores.
_MAX_LINE = 65536

# Maximum number of events ``load_recent_events`` will materialize in
# memory in one call. ``deque(maxlen=...)`` makes the streaming read
# O(limit) memory regardless of file size, so this is a defensive ceiling
# rather than a typical working set.
_MAX_TAIL = 5000

# Tail-read chunk size for ``_next_seq`` and ``append_event``. Must be at
# least ``_MAX_LINE + 1`` bytes so the tail always spans one complete record.
# A value of ``8192`` (one typical disk block) was the original choice, but
# records up to ``_MAX_LINE`` (64 KiB) are legal; a large-but-valid record
# ending the file would be partially read, fail JSON parsing, and cause
# ``_last_seq_from_tail`` to return 0 — resetting the sequence counter to 1.
_TAIL_READ_BYTES = _MAX_LINE + 1

# Allowlisted event kinds. The list is open in spirit — the dashboard
# renders any string — but the canonical set is documented here so the
# team-lead and the dashboard JS agree on which kinds get specialized
# rendering (icons, color, grouping) vs. fall through to the generic
# "informational" row template.
EVENT_KINDS_CORE = frozenset(
    {
        "run_start",
        "runner_spawn",
        "report_relay",
        "fix_dispatch",
        "run_end",
    }
)


def live_dir(target: str | Path) -> Path:
    """Return ``<target>/.advisor/live`` (does not create the directory)."""
    return Path(target) / LIVE_DIR_NAME / LIVE_SUBDIR


def live_events_path(target: str | Path) -> Path:
    """Return ``<target>/.advisor/live/events.jsonl`` (does not create the file)."""
    return live_dir(target) / LIVE_FILE_NAME


def _last_seq_from_tail(tail: bytes) -> int:
    """Extract the last ``seq`` value from a JSONL tail chunk, or 0 if absent.

    Returns ``0`` on empty / missing / malformed input so callers compute
    ``next_seq = last + 1`` and land on ``1`` for a fresh file. Shared
    between :func:`_next_seq` (path-based) and :func:`append_event`'s
    locked critical section (open-fd-based) so both parse the tail with
    identical semantics.
    """
    lines = [ln for ln in tail.splitlines() if ln.strip()]
    if not lines:
        return 0
    # Scan backwards so a truncated/corrupt final line doesn't mask valid
    # records above it — return the seq from the last parseable line.
    for line in reversed(lines):
        try:
            record = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        last_seq = record.get("seq")
        if not isinstance(last_seq, int) or last_seq < 0:
            continue
        return last_seq
    return 0


def _read_final_line_tail(fh: BinaryIO) -> bytes:
    """Return enough bytes to include the final valid event line.

    Writers cap each JSONL record at ``_MAX_LINE`` bytes, plus one trailing
    LF. Reading ``_MAX_LINE + 1`` from EOF is therefore sufficient for any
    valid final event while keeping the cursor path bounded.
    """
    try:
        fh.seek(0, 2)
        size = fh.tell()
    except OSError:
        return b""
    if size <= 0:
        return b""
    chunk_size = min(size, _TAIL_READ_BYTES)
    try:
        fh.seek(size - chunk_size, 0)
        return fh.read(chunk_size)
    except OSError:
        return b""


def _next_seq(path: Path) -> int:
    """Return ``last_seq + 1`` by reading only the file's final non-empty line.

    The dashboard's ``?since=<seq>`` cursor needs ``seq`` to be monotonic.
    Computing it from the existing file means a fresh writer (e.g. after
    a CLI restart) picks up where the prior writer left off — operators
    don't lose ordering across process boundaries.

    On a missing or empty file, returns ``1`` (first event in a new run).
    Malformed final lines fall back to ``1`` rather than raising — the
    file is advisory, not load-bearing.

    This function is the read-only path used by :func:`latest_seq` for
    the dashboard's ``/api/events`` next-token computation. The write
    path in :func:`append_event` does NOT call this; it holds an
    exclusive lock across an inline tail-read + append so concurrent
    appenders cannot pick the same next-seq.
    """
    if not path.exists():
        return 1
    try:
        with path.open("rb") as f:
            tail = _read_final_line_tail(f)
    except OSError:
        return 1
    return _last_seq_from_tail(tail) + 1


def append_event(
    target: str | Path,
    kind: str,
    data: dict[str, Any] | None = None,
    *,
    ts: str | None = None,
) -> Path:
    """Append a single event to ``<target>/.advisor/live/events.jsonl``.

    Creates the ``.advisor/live/`` directory and the events file on first
    use. Returns the absolute path written to.

    ``kind`` is a free-form string — see :data:`EVENT_KINDS_CORE` for the
    canonical set the dashboard renders with specialized icons. Unknown
    kinds render as generic informational rows.

    ``data`` is an opaque payload dict serialized verbatim. Keep it small
    (the per-line cap is 64 KiB); the dashboard is a streaming feed, not
    a transcript archive.

    ``ts`` defaults to ``datetime.now(UTC)`` rendered to millisecond
    precision. Callers may pass an explicit timestamp for testing or to
    record an event that already happened.
    """
    if not kind or not isinstance(kind, str):
        raise ValueError(f"kind must be a non-empty string, got {kind!r}")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"data must be a dict or None, got {type(data).__name__}")
    path = live_events_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    if ts is None:
        ts = datetime.now(UTC).isoformat(timespec="milliseconds")
        # ``isoformat`` emits ``+00:00`` — normalize to ``Z`` so the wire
        # format matches what JavaScript's ``Date`` prefers and what
        # downstream consumers grepping for "Z" can rely on.
        if ts.endswith("+00:00"):
            ts = ts[:-6] + "Z"
    # Open in ``a+b`` so the file is created on first use AND we can seek-
    # read the tail for next-seq computation. Hold an exclusive advisory
    # lock across the tail-read AND the append so concurrent appenders
    # cannot both observe the same ``last_seq`` and write duplicate ``seq``
    # values. The dashboard's ``?since=<seq>`` cursor breaks if two records
    # share a seq — one record becomes invisible to filtered reads where
    # ``since == the duplicate``. The pre-fix shape (separate ``_next_seq``
    # open + separate append open, no lock) had that race.
    #
    # Writing encoded bytes keeps the tail seek byte-based and forces LF-only
    # JSONL across platforms.
    with path.open("a+b") as f:
        _lock_exclusive(cast(Any, f))
        try:
            # Tail-read inside the lock so the seq we compute is the truly
            # latest one. ``a+b`` opens with position 0 on POSIX — seek to
            # the end to learn the size, then back up to read the tail.
            tail = _read_final_line_tail(f)
            seq = _last_seq_from_tail(tail) + 1 if tail else 1
            record = {
                "schema_version": LIVE_SCHEMA_VERSION,
                "ts": ts,
                "seq": seq,
                "kind": kind,
                "data": data,
            }
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
            if len(line.encode("utf-8")) > _MAX_LINE:
                raise ValueError(
                    f"live event too large ({len(line)} chars > {_MAX_LINE} per-line cap); "
                    "trim the data payload"
                )
            # ``a`` mode always appends at end on POSIX regardless of the
            # current seek position; explicit seek_to_end is belt-and-
            # suspenders for the read above leaving us mid-file.
            f.seek(0, 2)
            f.write((line + "\n").encode("utf-8"))
            f.flush()
        finally:
            _unlock_exclusive(cast(Any, f))
    return path


def load_recent_events(
    target: str | Path,
    *,
    since: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return events from ``<target>/.advisor/live/events.jsonl``.

    ``since`` is the cursor: only events with ``seq > since`` are returned.
    Pass ``None`` (the default) to return the tail of the file regardless
    of cursor — useful for the dashboard's initial page load before any
    cursor exists.

    ``limit`` caps the number of events returned. The dashboard usually
    polls with ``limit=200`` which is well below :data:`_MAX_TAIL` but
    leaves headroom for catch-up after a brief disconnect.

    Returned events are in **chronological order** (oldest first).
    Callers that want newest-first should reverse the list themselves —
    keeping the stored order natural makes the cursor logic obvious.

    Malformed JSON lines are skipped with a UserWarning, matching the
    history loader's policy: one bad line shouldn't break the feed.
    """
    if limit <= 0:
        return []
    cap = min(limit, _MAX_TAIL)
    path = live_events_path(target)
    if not path.exists():
        return []
    # ``deque(maxlen=cap)`` keeps memory bounded even if the file has
    # millions of events — only the last ``cap`` survive the streaming
    # iteration.
    keep: deque[dict[str, Any]] = deque(maxlen=cap)
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            # Use ``readline(_MAX_LINE + 1)`` rather than ``for raw in f``
            # so a single corrupted line without a newline cannot OOM the
            # reader. The plain iterator buffers the full line BEFORE the
            # length check fires; the size-bounded readline caps each read
            # at ``_MAX_LINE + 1`` bytes so an oversized line is detectable
            # (``len(chunk) > _MAX_LINE``) and we drain its tail before
            # moving on. Mirrors the same pattern in ``history.py``.
            while True:
                raw = f.readline(_MAX_LINE + 1)
                if not raw:
                    break
                if len(raw) > _MAX_LINE:
                    warnings.warn(
                        f"live event line exceeds {_MAX_LINE}-byte cap; skipping",
                        UserWarning,
                        stacklevel=2,
                    )
                    while raw and not raw.endswith("\n"):
                        raw = f.readline(_MAX_LINE + 1)
                    continue
                line = raw.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, json.JSONDecodeError):
                    warnings.warn(
                        "malformed JSON line in live events file; skipping",
                        UserWarning,
                        stacklevel=2,
                    )
                    continue
                if not isinstance(record, dict):
                    continue
                seq = record.get("seq")
                if not isinstance(seq, int) or seq < 0:
                    continue
                if since is not None and seq <= since:
                    continue
                keep.append(record)
    except OSError as exc:
        warnings.warn(
            f"unable to read live events file at {path}: {exc}",
            UserWarning,
            stacklevel=2,
        )
        return []
    return list(keep)


def latest_seq(target: str | Path) -> int:
    """Return the highest ``seq`` written to the events file, or 0 if absent.

    Used by the dashboard's ``/api/events`` handler so the client's cursor
    can advance even when the most recent poll returned zero new events
    (the dashboard still needs a stable token to send next time).
    """
    path = live_events_path(target)
    if not path.exists():
        return 0
    next_one = _next_seq(path)
    return max(0, next_one - 1)
