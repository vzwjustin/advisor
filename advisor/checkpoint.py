"""Run checkpoints — save/resume the advisor's plan mid-pipeline.

A checkpoint captures the *pre-execution state*: the ranked files, the
chosen batches, the target config, the run id, and a UTC timestamp. It
lets a user resume an expensive review if the Claude Code session dies
between ``plan`` and the live dispatch, without paying for re-discovery.

Checkpoints live at ``<target>/.advisor/run-<run_id>.json`` alongside the
history file. They are human-readable JSON — diffable and auditable.
"""

from __future__ import annotations

import json
import os
import tempfile
import warnings
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .focus import FocusBatch, FocusTask
from .history import HISTORY_DIR_NAME

UTC = timezone.utc

CHECKPOINT_SCHEMA_VERSION = "1.0"
CHECKPOINT_PREFIX = "run-"
CHECKPOINT_SUFFIX = ".json"


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """A persisted snapshot of a planned advisor run."""

    run_id: str
    created_at: str
    target: str
    team_name: str
    file_types: str
    min_priority: int
    max_runners: int
    advisor_model: str
    runner_model: str
    max_fixes_per_runner: int
    large_file_line_threshold: int
    large_file_max_fixes: int
    test_command: str
    context: str
    tasks: list[dict[str, object]]
    batches: list[dict[str, object]]
    schema_version: str = CHECKPOINT_SCHEMA_VERSION


def _dir(target: str | Path) -> Path:
    return Path(target) / HISTORY_DIR_NAME


def checkpoint_path(target: str | Path, run_id: str) -> Path:
    """Absolute path for a checkpoint file with the given run_id."""
    return _dir(target) / f"{CHECKPOINT_PREFIX}{run_id}{CHECKPOINT_SUFFIX}"


def _atomic_write_text(target: Path, text: str) -> None:
    """Write ``text`` to ``target`` atomically via a same-dir tmpfile + rename.

    A SIGINT / disk-full mid-write leaves the original file untouched —
    ``os.replace`` is atomic on POSIX and Windows when source and target
    sit on the same filesystem. We write to a tmpfile in the target's
    parent directory to guarantee that property. Unlike the hardened
    ``install._atomic_write_text`` (which defends against symlink TOCTOU
    on shared $HOME), checkpoints live under the user's own target dir
    and don't warrant the same ceremony — but atomic rename is still the
    right reliability primitive.
    """
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(parent))
    tmp = Path(tmp_name)
    try:
        fh = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    try:
        with fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def save_checkpoint(
    target: str | Path,
    *,
    run_id: str,
    tasks: list[FocusTask],
    batches: list[FocusBatch] | None,
    team_name: str,
    file_types: str,
    min_priority: int,
    max_runners: int,
    advisor_model: str,
    runner_model: str,
    max_fixes_per_runner: int = 5,
    large_file_line_threshold: int = 800,
    large_file_max_fixes: int = 3,
    test_command: str = "",
    context: str = "",
) -> Path:
    """Write a checkpoint JSON file and return its path.

    Overwrites an existing checkpoint with the same ``run_id`` (idempotent
    — lets the advisor update a plan mid-flight).
    """
    task_dicts = [
        {"file_path": t.file_path, "priority": t.priority, "prompt": t.prompt} for t in tasks
    ]
    batch_dicts: list[dict[str, object]] = []
    if batches:
        for b in batches:
            batch_dicts.append(
                {
                    "batch_id": b.batch_id,
                    "complexity": b.complexity,
                    "top_priority": b.top_priority,
                    "tasks": [
                        {"file_path": t.file_path, "priority": t.priority, "prompt": t.prompt}
                        for t in b.tasks
                    ],
                }
            )
    checkpoint = Checkpoint(
        run_id=run_id,
        created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        target=str(target),
        team_name=team_name,
        file_types=file_types,
        min_priority=min_priority,
        max_runners=max_runners,
        advisor_model=advisor_model,
        runner_model=runner_model,
        max_fixes_per_runner=max_fixes_per_runner,
        large_file_line_threshold=large_file_line_threshold,
        large_file_max_fixes=large_file_max_fixes,
        test_command=test_command,
        context=context,
        tasks=task_dicts,
        batches=batch_dicts,
    )
    path = checkpoint_path(target, run_id)
    _atomic_write_text(path, json.dumps(asdict(checkpoint), indent=2))
    return path


def load_checkpoint(target: str | Path, run_id: str) -> Checkpoint:
    """Load a checkpoint by run_id. Raises :class:`FileNotFoundError` or
    :class:`ValueError` on missing / malformed content.
    """
    path = checkpoint_path(target, run_id)
    if not path.exists():
        raise FileNotFoundError(f"no checkpoint at {path}")
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read checkpoint {path}: {exc}") from exc

    version = str(obj.get("schema_version", CHECKPOINT_SCHEMA_VERSION))
    if version and version != CHECKPOINT_SCHEMA_VERSION:
        warnings.warn(
            f"{path}: schema_version {version!r} does not match "
            f"expected {CHECKPOINT_SCHEMA_VERSION!r}; parsing anyway",
            UserWarning,
            stacklevel=2,
        )

    try:
        return Checkpoint(
            run_id=str(obj["run_id"]),
            created_at=str(obj["created_at"]),
            target=str(obj["target"]),
            team_name=str(obj["team_name"]),
            file_types=str(obj["file_types"]),
            min_priority=int(obj["min_priority"]),
            max_runners=int(obj["max_runners"]),
            advisor_model=str(obj["advisor_model"]),
            runner_model=str(obj["runner_model"]),
            max_fixes_per_runner=int(obj.get("max_fixes_per_runner", 5)),
            large_file_line_threshold=int(obj.get("large_file_line_threshold", 800)),
            large_file_max_fixes=int(obj.get("large_file_max_fixes", 3)),
            test_command=str(obj.get("test_command", "")),
            context=str(obj.get("context", "")),
            tasks=list(obj.get("tasks", [])),
            batches=list(obj.get("batches", [])),
            schema_version=version,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"checkpoint {path} is missing required fields: {exc}") from exc


def list_checkpoints(target: str | Path) -> list[str]:
    """Return all run_ids with saved checkpoints in ``target``, newest first.

    Ordering is lexical by filename — since run_ids are UTC ISO-like
    timestamps, lexical equals chronological.
    """
    d = _dir(target)
    if not d.is_dir():
        return []
    ids: list[str] = []
    for p in d.iterdir():
        name = p.name
        if name.startswith(CHECKPOINT_PREFIX) and name.endswith(CHECKPOINT_SUFFIX):
            ids.append(name[len(CHECKPOINT_PREFIX) : -len(CHECKPOINT_SUFFIX)])
    return sorted(ids, reverse=True)
