"""Persistent findings history — write-once JSONL log of confirmed issues.

Every advisor run that produces CONFIRMED findings appends them to
``.advisor/history.jsonl`` in the target directory (one JSON object per
line). On subsequent runs, the advisor prompt can reference recent history
to detect recurring findings — the same issue flagged twice is a process
gap, not just a code bug.

Schema is additive: each record carries ``schema_version`` so newer
advisor releases can evolve the shape without breaking older parsers.
Unreadable / malformed lines are skipped with a warning — history is
advisory, never fatal.
"""

from __future__ import annotations

import json
import secrets
import sys
import warnings
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from advisor.orchestrate._fence import fence

UTC = timezone.utc

HISTORY_DIR_NAME = ".advisor"
HISTORY_FILE_NAME = "history.jsonl"
HISTORY_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """A single recorded finding from a past run."""

    timestamp: str  # ISO-8601 UTC
    file_path: str
    severity: str
    description: str
    status: str  # CONFIRMED / FIXED / REJECTED
    run_id: str
    schema_version: str = HISTORY_SCHEMA_VERSION

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


def history_path(target: str | Path) -> Path:
    """Return the absolute path to ``<target>/.advisor/history.jsonl``."""
    return Path(target) / HISTORY_DIR_NAME / HISTORY_FILE_NAME


def _lock_exclusive(fh: IO[str]) -> None:
    """Acquire an exclusive advisory lock on ``fh``, best-effort.

    Used by :func:`append_entries` so two concurrent ``advisor`` processes
    cannot interleave partial JSON lines in ``history.jsonl``. ``fcntl``
    is Unix-only; on Windows we fall back to ``msvcrt.locking``. Any
    platform that exposes neither (or where the call fails — e.g. NFS
    without lock support) silently proceeds without a lock. Locking is
    a best-effort nicety, not a correctness gate: Python's own buffered
    ``.write`` typically flushes short records atomically, so the
    unlocked path is still well-behaved in practice.
    """
    # Branch on platform so mypy can see both paths — ``fcntl`` is
    # unconditionally importable on POSIX and unconditionally absent on
    # Windows; the reverse holds for ``msvcrt``.
    if sys.platform != "win32":
        try:
            import fcntl
        except ImportError:
            return
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError:
            pass
        return
    # Windows branch — mypy on POSIX considers this unreachable because
    # ``sys.platform`` is a literal in the stubs. The runtime still
    # executes it on actual Windows.
    _lock_windows(fh)  # type: ignore[unreachable]


def _lock_windows(fh: IO[str]) -> None:
    if sys.platform != "win32":  # pragma: no cover - platform guard
        return
    try:  # type: ignore[unreachable]
        import msvcrt
    except ImportError:
        return
    try:
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 0x7FFFFFFF)
    except OSError:
        pass


def append_entries(target: str | Path, entries: list[HistoryEntry]) -> Path:
    """Append ``entries`` to the history file, creating it if needed.

    Creates ``.advisor/`` on first use. Returns the path written to.
    Empty ``entries`` is a no-op (no file is created).

    The append holds an exclusive advisory lock for the duration of the
    write so two parallel ``advisor`` processes cannot interleave
    partial JSON lines. Locking is best-effort — see
    :func:`_lock_exclusive`.
    """
    if not entries:
        return history_path(target)
    path = history_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Pre-serialize the whole batch so the locked critical section is as
    # short as possible — one ``write`` call per process rather than
    # ``len(entries)`` of them.
    payload = "".join(entry.to_json_line() + "\n" for entry in entries)
    with path.open("a", encoding="utf-8") as f:
        _lock_exclusive(f)
        f.write(payload)
    return path


def load_recent(target: str | Path, limit: int = 20) -> list[HistoryEntry]:
    """Return the ``limit`` most recent entries from the history file.

    Malformed lines are skipped with a warning. A missing file returns ``[]``.

    Streams the file through a bounded :class:`collections.deque` so only
    the tail fits in memory — important once ``.advisor/history.jsonl``
    accumulates months of findings. We over-sample by ``limit * 2`` lines
    so that a pathological run of malformed lines near the tail still
    yields ``limit`` valid entries when possible.
    """
    path = history_path(target)
    if not path.exists():
        return []
    if limit <= 0:
        return []
    buffer_size = max(limit * 2, limit + 8)
    try:
        with path.open("r", encoding="utf-8") as f:
            lines: deque[tuple[int, str]] = deque(enumerate(f, 1), maxlen=buffer_size)
    except (OSError, UnicodeDecodeError) as exc:
        warnings.warn(f"could not read {path}: {exc}", UserWarning, stacklevel=2)
        return []

    entries: list[HistoryEntry] = []
    for line_num, line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
            entries.append(
                HistoryEntry(
                    timestamp=str(obj["timestamp"]),
                    file_path=str(obj["file_path"]),
                    severity=str(obj["severity"]),
                    description=str(obj["description"]),
                    status=str(obj["status"]),
                    run_id=str(obj["run_id"]),
                    schema_version=str(obj.get("schema_version", HISTORY_SCHEMA_VERSION)),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            warnings.warn(
                f"skipping malformed history entry at {path}:{line_num}: {exc}",
                UserWarning,
                stacklevel=2,
            )

    return entries[-limit:]


def format_history_block(entries: list[HistoryEntry]) -> str:
    """Format recent history as a Markdown block for the advisor prompt.

    Empty ``entries`` returns an empty string so the caller can skip the
    whole section. Otherwise produces a bulleted list keyed by file path
    with severity and short description — enough for the advisor to notice
    recurrences without bloating its context.
    """
    if not entries:
        return ""
    lines = ["## Recent findings from prior runs", ""]
    for e in entries:
        lines.append(f"- `{e.file_path}` [{e.severity}] ({e.status}):")
        lines.append(fence(e.description))
    return "\n".join(lines)


def new_run_id() -> str:
    """Generate a collision-resistant run_id.

    Format: ``YYYYMMDDTHHMMSSZ-XXXX`` where ``XXXX`` is a random 4-hex
    suffix. The leading timestamp keeps lexical order == chronological
    order (so ``sorted(..., reverse=True)`` in ``list_checkpoints`` still
    returns newest-first); the suffix prevents two back-to-back runs in
    the same second from overwriting each other's checkpoint.
    """
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{secrets.token_hex(2)}"


def entry_now(
    *,
    file_path: str,
    severity: str,
    description: str,
    status: str,
    run_id: str,
) -> HistoryEntry:
    """Convenience builder for a history entry timestamped ``now``."""
    return HistoryEntry(
        timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
        file_path=file_path,
        severity=severity,
        description=description,
        status=status,
        run_id=run_id,
    )
