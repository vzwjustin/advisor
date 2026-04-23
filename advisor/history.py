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
import math
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

# Severity weights for repeat-offender scoring. Higher-severity findings
# should dominate the per-file score so a file with one CRITICAL
# recurrence outranks one with five LOW ones.
_SEVERITY_WEIGHTS: dict[str, float] = {
    "CRITICAL": 4.0,
    "HIGH": 2.5,
    "MEDIUM": 1.5,
    "LOW": 1.0,
}

# Default bonus cap — `rank_files` enforces the hard +1-tier cap, but
# the raw score is also clamped here so pathological history files
# (e.g. 10,000 findings on one file) don't explode intermediate
# calculations.
_MAX_FILE_SCORE = 10.0

# Allowlists for fields that flow unescaped into the advisor prompt label
# line and the dashboard CSS class suffix. A crafted newline or bracket in
# these fields would otherwise inject. Unknown values become "UNKNOWN".
_ALLOWED_SEVERITIES = frozenset({"CRITICAL", "HIGH", "MEDIUM", "LOW"})
_ALLOWED_STATUSES = frozenset({"CONFIRMED", "FIXED", "REJECTED"})


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

    .. note::
        Unlike :mod:`advisor.checkpoint`, the append is **not** fsynced.
        History is advisory — a lost tail on an OS crash is
        acceptable — and the append path runs on every finding, so the
        fsync cost per entry would dominate. Callers that need
        durability guarantees should persist elsewhere.
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
    """Return up to ``limit`` entries from the history file, newest-first.

    Malformed lines are skipped with a warning. A missing file returns ``[]``.
    Thin wrapper over :func:`load_recent_findings` that resolves the
    canonical ``<target>/.advisor/history.jsonl`` path so callers don't
    need to thread the filesystem layout.
    """
    return load_recent_findings(history_path(target), limit=limit)


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
        lines.append(f"- [{e.severity}] ({e.status}):")
        lines.append(fence(e.file_path))
        lines.append(fence(e.description))
    return "\n".join(lines)


def new_run_id() -> str:
    """Generate a collision-resistant run_id.

    Format: ``YYYYMMDDTHHMMSSZ-XXXXXXXX`` where ``XXXXXXXX`` is a random
    8-hex suffix. The leading timestamp keeps lexical order == chronological
    order (so ``sorted(..., reverse=True)`` in ``list_checkpoints`` still
    returns newest-first); the suffix prevents two back-to-back runs in
    the same second from overwriting each other's checkpoint.
    """
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{ts}-{secrets.token_hex(4)}"


def load_recent_findings(history_path: Path, *, limit: int = 500) -> list[HistoryEntry]:
    """Read the last ``limit`` entries from a JSONL history file.

    Tolerates malformed lines (logs a warning, skips). Returns
    newest-first. Missing file returns ``[]``. Never raises on IO errors
    — history is advisory, never fatal.

    ``history_path`` is the *file* path (use :func:`history_path` to
    derive it from a target dir). ``limit`` bounds the deque used to
    stream the tail.
    """
    if limit <= 0:
        return []
    if not history_path.exists():
        return []
    # Small overshoot so a handful of malformed lines in the tail still
    # yield ``limit`` well-formed entries. The previous ``limit * 2``
    # doubled memory for no good reason — this bounded margin keeps the
    # invariant while staying O(limit).
    buffer_size = limit + 16
    try:
        # utf-8-sig transparently strips a leading BOM on the first line —
        # some Windows editors write one, and plain utf-8 would otherwise
        # leave it attached to the first JSON object, triggering
        # JSONDecodeError and silently dropping what is typically the
        # newest entry after the reverse-on-read flip.
        with history_path.open("r", encoding="utf-8-sig") as f:
            lines: deque[tuple[int, str]] = deque(enumerate(f, 1), maxlen=buffer_size)
    except (OSError, UnicodeDecodeError) as exc:
        warnings.warn(f"could not read {history_path}: {exc}", UserWarning, stacklevel=2)
        return []

    entries: list[HistoryEntry] = []
    for line_num, line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
            for _f in ("timestamp", "file_path", "severity", "description", "status", "run_id"):
                if not isinstance(obj.get(_f), str):
                    raise TypeError(f"{_f} must be str, got {type(obj.get(_f)).__name__}")
            # Allowlist severity/status — they flow unescaped into the advisor
            # prompt label line (format_history_block) and a CSS class suffix in
            # the dashboard. A crafted newline in either field would inject.
            sev = obj["severity"] if obj["severity"] in _ALLOWED_SEVERITIES else "UNKNOWN"
            status = obj["status"] if obj["status"] in _ALLOWED_STATUSES else "UNKNOWN"
            entries.append(
                HistoryEntry(
                    timestamp=obj["timestamp"],
                    file_path=obj["file_path"],
                    severity=sev,
                    description=obj["description"],
                    status=status,
                    run_id=obj["run_id"],
                    schema_version=str(obj.get("schema_version", HISTORY_SCHEMA_VERSION)),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            warnings.warn(
                f"skipping malformed history entry at {history_path}:{line_num}: {exc}",
                UserWarning,
                stacklevel=2,
            )

    # Newest-first per contract. We don't know whether the file is in
    # chronological order, but by convention entries are appended — so
    # reversing the tail yields the newest-first view.
    entries.reverse()
    return entries[:limit]


def _age_days(entry: HistoryEntry, *, now: datetime | None = None) -> float:
    """Return the age of ``entry`` in days. Unparseable timestamps → +inf.

    Unparseable entries return ``math.inf`` so they decay to ~0 in
    ``file_repeat_scores`` rather than being treated as brand-new and
    inflating the score with maximum recency weight. An entry we cannot
    date is not evidence of a recent finding — it is evidence of a
    corrupted record.
    """
    now_dt = now or datetime.now(UTC)
    try:
        ts = datetime.fromisoformat(entry.timestamp)
    except ValueError:
        return math.inf
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = now_dt - ts
    return max(0.0, delta.total_seconds() / 86400.0)


def file_repeat_counts(
    findings: list[HistoryEntry],
    *,
    window_days: float = 90.0,
    now: datetime | None = None,
) -> dict[str, int]:
    """Per-file CONFIRMED-finding count within the last ``window_days``.

    Used to annotate the "repeat offender" reason with a concrete number
    so the plan reads naturally. Separate from :func:`file_repeat_scores`
    because the rank boost is an exponentially-decaying continuous score,
    while the *reason label* wants a human-readable tally.
    """
    now_dt = now or datetime.now(UTC)
    counts: dict[str, int] = {}
    for entry in findings:
        if entry.status.upper() != "CONFIRMED":
            continue
        age = _age_days(entry, now=now_dt)
        if age > window_days:
            continue
        counts[entry.file_path] = counts.get(entry.file_path, 0) + 1
    return counts


def file_repeat_scores(
    findings: list[HistoryEntry],
    *,
    half_life_days: float = 30.0,
    now: datetime | None = None,
) -> dict[str, float]:
    """Aggregate historical findings into a per-file repeat-offender score.

    Applies exponential decay with the given half-life — a finding that
    landed ``half_life_days`` days ago contributes half its severity
    weight; twice that ago, a quarter; etc. Returns
    ``{abs_path: score}`` where score > 0 for every file that appears.
    Missing files are absent.

    FIXED / REJECTED statuses are ignored — only CONFIRMED findings
    signal ongoing risk.
    """
    if half_life_days <= 0:
        raise ValueError("half_life_days must be > 0")
    now_dt = now or datetime.now(UTC)
    scores: dict[str, float] = {}
    decay_lambda = math.log(2.0) / half_life_days
    for entry in findings:
        if entry.status.upper() != "CONFIRMED":
            continue
        weight = _SEVERITY_WEIGHTS.get(entry.severity.upper(), 1.0)
        age = _age_days(entry, now=now_dt)
        contribution = weight * math.exp(-decay_lambda * age)
        if contribution <= 0:
            continue
        scores[entry.file_path] = min(
            _MAX_FILE_SCORE, scores.get(entry.file_path, 0.0) + contribution
        )
    return scores


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
