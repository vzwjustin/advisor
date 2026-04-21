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
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

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


def append_entries(target: str | Path, entries: list[HistoryEntry]) -> Path:
    """Append ``entries`` to the history file, creating it if needed.

    Creates ``.advisor/`` on first use. Returns the path written to.
    Empty ``entries`` is a no-op (no file is created).
    """
    if not entries:
        return history_path(target)
    path = history_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for entry in entries:
            f.write(entry.to_json_line())
            f.write("\n")
    return path


def load_recent(target: str | Path, limit: int = 20) -> list[HistoryEntry]:
    """Return the ``limit`` most recent entries from the history file.

    Malformed lines are skipped with a warning. A missing file returns ``[]``.
    """
    path = history_path(target)
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        warnings.warn(f"could not read {path}: {exc}", UserWarning, stacklevel=2)
        return []

    entries: list[HistoryEntry] = []
    for line_num, line in enumerate(lines, 1):
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
        lines.append(f"- `{e.file_path}` [{e.severity}] — {e.description} ({e.status})")
    return "\n".join(lines)


def new_run_id() -> str:
    """Generate a UTC ISO-8601 timestamp suitable as a run_id.

    Collision-free at second granularity; if multiple runs can start in
    the same second, callers should append a random suffix.
    """
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


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
