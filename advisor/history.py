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

import errno
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

from ._fs import normalize_path as _normalize_path

UTC = timezone.utc

HISTORY_DIR_NAME = ".advisor"
HISTORY_FILE_NAME = "history.jsonl"
HISTORY_SCHEMA_VERSION = "1.0"
_IS_WINDOWS = sys.platform == "win32"

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

# Process-wide latch so we warn at most once per session that the
# advisory lock is unsupported on this filesystem. Otherwise every
# ``append_entries`` call on an NFS-without-lockd mount would emit a
# warning, which is itself noisy and would mask the real signal.
_LOCK_UNSUPPORTED_WARNED = False

# errno values that indicate the filesystem itself doesn't support
# locking (rather than a transient lock-contention failure). On
# lock-unsupported filesystems we want to surface the diagnostic once
# so operators know history writes are unprotected.
_LOCK_UNSUPPORTED_ERRNOS: frozenset[int] = frozenset(
    getattr(errno, name)
    for name in ("ENOLCK", "ENOSYS", "EOPNOTSUPP", "ENOTSUP")
    if hasattr(errno, name)
)


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
    a best-effort nicety, not a correctness gate: on local filesystems
    the kernel guarantees a single ``write()`` syscall under PIPE_BUF
    (~4096 bytes on Linux) appends atomically to an ``O_APPEND`` file,
    so the unlocked path is fine for small single-entry payloads. (This
    is a kernel guarantee, not a Python-buffering one — Python's buffered
    ``.write`` may split a large payload across multiple syscalls.)

    On lock-unsupported filesystems (NFS-without-lockd is the canonical
    case), a multi-finding batch payload can exceed PIPE_BUF and split
    across concurrent appenders. We emit a one-shot UserWarning on the
    first lock-unsupported errno so operators know history writes are
    unprotected — without it, the silent ``except OSError: pass`` swallows
    the very signal that says "your distributed CI matrix is racing".
    """
    if not _IS_WINDOWS:
        try:
            import fcntl
        except ImportError:
            return
        flock = getattr(fcntl, "flock", None)
        lock_ex = getattr(fcntl, "LOCK_EX", None)
        if not callable(flock) or not isinstance(lock_ex, int):
            return
        try:
            flock(fh.fileno(), lock_ex)
        except OSError as exc:
            _maybe_warn_lock_unsupported(exc)
        return
    _lock_windows(fh)


def _maybe_warn_lock_unsupported(exc: OSError) -> None:
    """Emit a one-shot warning on lock-unsupported errno.

    Idempotent across the process — once warned, subsequent calls are
    no-ops so the warning doesn't spam every ``append_entries``. A
    transient lock-contention failure (e.g. EAGAIN, EINTR) is silently
    tolerated, matching the prior best-effort contract.
    """
    global _LOCK_UNSUPPORTED_WARNED
    if _LOCK_UNSUPPORTED_WARNED:
        return
    if exc.errno not in _LOCK_UNSUPPORTED_ERRNOS:
        return
    _LOCK_UNSUPPORTED_WARNED = True
    warnings.warn(
        "advisor history file lock not supported on this filesystem "
        f"(errno {exc.errno}); concurrent ``advisor`` processes may "
        "interleave JSON lines on writes larger than PIPE_BUF (~4096 "
        "bytes). Subsequent appends will proceed without locking.",
        UserWarning,
        stacklevel=2,
    )


def _unlock_exclusive(fh: IO[str]) -> None:
    """Release the lock acquired by :func:`_lock_exclusive`, best-effort.

    On POSIX, closing the file descriptor releases ``fcntl.flock`` so we
    only need an explicit unlock for the Windows ``msvcrt.locking`` path —
    that lock survives close until LK_UNLCK runs.
    """
    if not _IS_WINDOWS:
        return
    _unlock_windows(fh)


def _lock_windows(fh: IO[str]) -> None:
    if not _IS_WINDOWS:  # pragma: no cover - platform guard
        return
    try:
        import msvcrt
    except ImportError:
        return
    locking = getattr(msvcrt, "locking", None)
    lk_lock = getattr(msvcrt, "LK_LOCK", None)
    if not callable(locking) or not isinstance(lk_lock, int):
        return
    try:
        locking(fh.fileno(), lk_lock, 0x7FFFFFFF)
    except OSError as exc:
        _maybe_warn_lock_unsupported(exc)


def _unlock_windows(fh: IO[str]) -> None:
    """Release a Windows ``msvcrt.locking`` lock acquired by :func:`_lock_windows`.

    ``msvcrt.locking`` does not auto-release on close — the OS keeps the
    region locked until the process exits or LK_UNLCK is called. Always
    pair :func:`_lock_windows` with this in a try/finally so the next
    appender on the same file isn't blocked.
    """
    if not _IS_WINDOWS:  # pragma: no cover - platform guard
        return
    try:
        import msvcrt
    except ImportError:
        return
    locking = getattr(msvcrt, "locking", None)
    lk_unlck = getattr(msvcrt, "LK_UNLCK", None)
    if not callable(locking) or not isinstance(lk_unlck, int):
        return
    try:
        locking(fh.fileno(), lk_unlck, 0x7FFFFFFF)
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
    # ``newline=""`` disables ``\n`` → ``\r\n`` translation on Windows.
    # Critical here because ``msvcrt.locking`` uses byte offsets and the
    # JSONL file format must stay LF-only for downstream parsers.
    with path.open("a", encoding="utf-8", newline="") as f:
        _lock_exclusive(f)
        try:
            f.write(payload)
            f.flush()
        finally:
            # On Windows, msvcrt.locking does NOT auto-release on close —
            # explicit LK_UNLCK is required or the next appender blocks.
            # Flush before unlocking so the advisory lock covers the actual
            # buffered append on Windows as well as POSIX.
            _unlock_exclusive(f)
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
    whole section. Otherwise produces a list grouped by file path so the
    advisor can see cross-file recurrence patterns at a glance — multiple
    entries on the same file cluster under one header instead of being
    scattered through a flat list.

    Each ``file_path`` and ``description`` is wrapped in :func:`fence` as
    a prompt-injection guard; the upstream ``build_advisor_prompt`` adds
    a labeled outer fence on top of that. Severity and status come from
    allowlists in :class:`HistoryEntry` ingestion, so they flow into the
    label line as bare text.
    """
    if not entries:
        return ""
    # Group while preserving first-seen order so newest-first input
    # ordering (per ``load_recent_findings``) carries through to the
    # block — the file with the most recent finding stays at the top.
    #
    # Key by ``_normalize_path(e.file_path)`` rather than the raw path so
    # two entries on the same file under different spellings (``./foo.py``
    # vs ``foo.py``, BOM-prefixed paths, backslash-separated paths from
    # Windows runners) cluster under one header instead of producing two
    # ``### File`` sections. Read-side ``rank.py:_history_values_for`` already
    # normalizes for boost lookups; this aligns the grouping output with it
    # so the prompt block reads as one truth instead of two views.
    grouped: dict[str, list[HistoryEntry]] = {}
    display_path: dict[str, str] = {}
    for e in entries:
        key = _normalize_path(e.file_path) or e.file_path
        grouped.setdefault(key, []).append(e)
        display_path.setdefault(key, e.file_path)

    lines = ["## Recent findings from prior runs", ""]
    for key, file_entries in grouped.items():
        count_note = f" — {len(file_entries)} prior findings" if len(file_entries) > 1 else ""
        # Severity glyphs ([HIGH], etc.) come from an allowlist so the
        # bullet label is safe to render unfenced; ``description`` and
        # ``file_path`` stay individually fenced as the injection guard.
        # ``display_path`` is the first-seen raw spelling (preserves
        # what the user actually wrote into history); ``key`` is the
        # normalized form used only for grouping.
        lines.append(f"### File{count_note}")
        lines.append(fence(display_path[key]))
        for e in file_entries:
            lines.append(f"- [{e.severity}] ({e.status}):")
            lines.append(fence(e.description))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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
    # Start with a small overshoot so a handful of malformed lines in the
    # tail still yield ``limit`` well-formed entries. If the valid-entry
    # count falls short (more than ``buffer_size - limit`` malformed lines
    # in the tail), double the window and re-read, up to ``limit * 8``
    # lines total — ensuring repeat-offender scoring is accurate even when
    # a crash left many partial appends in the history file.
    buffer_size = limit + 16
    _max_buffer = max(limit * 8, buffer_size)
    _MAX_LINE = 65536

    while True:
        lines: deque[tuple[int, str]] = deque(maxlen=buffer_size)
        try:
            # Use strict utf-8 — utf-8-sig silently strips a BOM, masking
            # corruption from editors that write a BOM-prefixed file. We
            # write utf-8 (no BOM) and prefer to surface a UnicodeDecodeError
            # rather than silently mangle the first JSON object. If a BOM is
            # encountered, the caller will see a clear decode error via the
            # except block below, rather than a silent JSONDecodeError drop.
            # Never switch to utf-8-sig here: that would normalise away a
            # class of write-side bugs we want to stay visible.
            with history_path.open("r", encoding="utf-8") as f:
                # Stream lines directly into the bounded deque so a huge
                # history file does not materialize an O(file-size) intermediate
                # list just to truncate it on the next line.
                #
                # Use ``readline(_MAX_LINE + 1)`` instead of ``for _line in f`` so a
                # single corrupted line without a newline cannot OOM the reader.
                # The plain iterator delegates to ``readline()`` with no size cap
                # and buffers the full line BEFORE we can reject it on length —
                # ``readline(size)`` caps the read at ``size`` bytes, so an
                # oversized line is observable (``len(chunk) > _MAX_LINE``) and
                # we drain its tail with further capped reads before moving on.
                _line_no = 0
                while True:
                    chunk = f.readline(_MAX_LINE + 1)
                    if not chunk:
                        break
                    _line_no += 1
                    if len(chunk) > _MAX_LINE:
                        warnings.warn(
                            f"{history_path}:{_line_no}: line exceeds {_MAX_LINE} bytes, skipping",
                            UserWarning,
                            stacklevel=2,
                        )
                        # Drain the rest of this oversized line so the next
                        # readline call returns the start of the NEXT line, not
                        # this one's tail. EOF mid-drain is fine.
                        while chunk and not chunk.endswith("\n"):
                            chunk = f.readline(_MAX_LINE + 1)
                        continue
                    lines.append((_line_no, chunk))
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
                if not isinstance(obj, dict):
                    warnings.warn(
                        f"skipping non-dict history entry at {history_path}:{line_num}",
                        UserWarning,
                        stacklevel=2,
                    )
                    continue
                for _f in ("timestamp", "file_path", "severity", "description", "status", "run_id"):
                    if not isinstance(obj.get(_f), str):
                        raise TypeError(f"{_f} must be str, got {type(obj.get(_f)).__name__}")
                # Allowlist severity/status — they flow unescaped into the advisor
                # prompt label line (format_history_block) and a CSS class suffix in
                # the dashboard. A crafted newline in either field would inject.
                # Normalize to upper-case so foreign / hand-edited entries with
                # lowercase values don't diverge between file_repeat_counts (which
                # ignores severity entirely) and file_repeat_scores (which drops
                # UNKNOWN at line 395).
                sev_raw = obj["severity"].upper()
                status_raw = obj["status"].upper()
                sev = sev_raw if sev_raw in _ALLOWED_SEVERITIES else "UNKNOWN"
                status = status_raw if status_raw in _ALLOWED_STATUSES else "UNKNOWN"
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

        # Stop if we have enough entries, the buffer is at the cap, or the
        # file is exhausted (deque didn't fill up, meaning fewer lines exist
        # than buffer_size — doubling won't find more valid entries).
        if len(entries) >= limit or buffer_size >= _max_buffer or len(lines) < buffer_size:
            break
        buffer_size = min(buffer_size * 2, _max_buffer)

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
    delta_seconds = (now_dt - ts).total_seconds()
    # Future-dated entries beyond a 60s grace window are treated as
    # untrustworthy (mirroring the unparseable-timestamp branch above) so a
    # year-2099 timestamp cannot permanently inflate repeat-offender scores.
    # The 60s tolerance absorbs benign clock skew (e.g. a test that writes an
    # entry at T and reads it at T - epsilon).
    if delta_seconds < -60.0:
        return math.inf
    return max(0.0, delta_seconds / 86400.0)


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
        # Normalize the path key so ``./foo.py`` and ``foo.py`` (and
        # BOM-prefixed or backslash-separated variants) accumulate into a
        # single bucket. Without this, two confirmations on the same file
        # under different spellings would each contribute 1 to separate
        # buckets and ``rank.py:_history_count_for``'s ``max(...)`` of
        # the matches would return 1, not 2.
        key = _normalize_path(entry.file_path) or entry.file_path
        counts[key] = counts.get(key, 0) + 1
    return counts


def summarize(
    entries: list[HistoryEntry],
    *,
    top_n: int = 10,
    now: datetime | None = None,
) -> dict[str, object]:
    """Aggregate history entries into a JSON-friendly stats summary.

    Pure function — no I/O. ``confirm_rate`` is ``CONFIRMED / total`` and
    is ``0.0`` for an empty history (no ZeroDivision). ``top_files`` reuses
    :func:`file_repeat_counts` (CONFIRMED-only, all-time window) so the
    most-flagged ranking matches the repeat-offender bucketing the ranker
    uses, deterministically tie-broken by path.
    """
    total = len(entries)
    by_status: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    run_ids: set[str] = set()
    for e in entries:
        st = e.status.upper()
        by_status[st] = by_status.get(st, 0) + 1
        sv = e.severity.upper()
        by_severity[sv] = by_severity.get(sv, 0) + 1
        if e.run_id:
            run_ids.add(e.run_id)
    confirmed = by_status.get("CONFIRMED", 0)
    counts = file_repeat_counts(entries, window_days=float("inf"), now=now)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return {
        "total": total,
        "by_status": by_status,
        "by_severity": by_severity,
        "confirm_rate": (confirmed / total) if total else 0.0,
        "run_count": len(run_ids),
        "top_files": [{"file_path": p, "count": c} for p, c in ranked],
    }


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
        sev_key = entry.severity.upper()
        # Sanitised entries (severity coerced to "UNKNOWN" by the allowlist
        # in load_history) carry no signal — drop them rather than letting
        # them ride the dict.get default and inflate the score with LOW
        # weight. A corrupted record is not evidence of a finding.
        if sev_key not in _SEVERITY_WEIGHTS:
            continue
        weight = _SEVERITY_WEIGHTS[sev_key]
        age = _age_days(entry, now=now_dt)
        contribution = weight * math.exp(-decay_lambda * age)
        if contribution <= 0:
            continue
        # See ``file_repeat_counts`` for the rationale on key
        # normalization — same regression, same fix shape.
        key = _normalize_path(entry.file_path) or entry.file_path
        scores[key] = min(_MAX_FILE_SCORE, scores.get(key, 0.0) + contribution)
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
