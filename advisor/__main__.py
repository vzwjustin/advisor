"""Advisor CLI — for `python -m advisor` and the `advisor` script entry point.

Thin wrapper over the existing builders. Prints prompts/plans to stdout so a
"vibe coder" can paste them into Claude Code without touching Python.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import warnings
from collections.abc import Callable, Sequence
from dataclasses import fields as _dc_fields
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

from . import _style
from ._fs import atomic_write_text as _atomic_write
from ._fs import read_head as _read_head
from ._fs import validate_file_types
from .audit import AuditReport, audit_to_dict, audit_transcript, format_audit_report
from .checkpoint import (
    Checkpoint,
    checkpoint_path,
    list_checkpoints,
    load_checkpoint,
    save_checkpoint,
)
from .cost import estimate_cost, format_estimate, load_pricing
from .doctor import format_report, run_doctor
from .focus import (
    FocusBatch,
    FocusTask,
    create_focus_batches,
    create_focus_tasks,
    format_batch_plan,
    format_dispatch_plan,
)
from .git_scope import GitScopeError, resolve_git_scope
from .history import (
    file_repeat_scores,
    format_history_block,
    history_path,
    load_recent,
    load_recent_findings,
    new_run_id,
)
from .install import (
    OPT_OUT_ENV,
    ComponentStatus,
    InstallAction,
    InstallResult,
    Status,
    ensure_nudge,
    get_installed_skill_version,
    install_skill,
    uninstall_skill,
)
from .install import (
    install as install_nudge,
)
from .install import (
    status as get_status,
)
from .install import (
    uninstall as uninstall_nudge,
)
from .orchestrate import (
    TeamConfig,
    build_advisor_prompt,
    build_runner_pool_prompt,
    build_verify_dispatch_prompt,
    check_batch_fix_budget,
    default_team_config,
    render_pipeline,
)
from .rank import load_advisorignore, rank_files
from .sarif import findings_to_sarif

# Top-level schema version for JSON outputs. Bump when the shape of any
# ``--json`` payload changes in a way that would break downstream parsers.
# Individual payload modules (history, checkpoint) carry their own
# schema_version fields for fine-grained evolution.
JSON_SCHEMA_VERSION = "1.0"


def _team_config_default(name: str) -> int:
    """Return the default value of a ``TeamConfig`` int field by name.

    Single source of truth for argparse defaults and ``getattr`` fallbacks
    in ``_config_from_args`` — without this, ``5`` / ``800`` / ``3``
    appear in three places (dataclass, argparse, getattr) and silently
    drift apart on changes.
    """
    for f in _dc_fields(TeamConfig):
        if f.name == name:
            assert isinstance(f.default, int)
            return f.default
    raise KeyError(name)


_DEFAULT_MAX_FIXES = _team_config_default("max_fixes_per_runner")
_DEFAULT_LARGE_FILE_THRESHOLD = _team_config_default("large_file_line_threshold")
_DEFAULT_LARGE_FILE_MAX_FIXES = _team_config_default("large_file_max_fixes")


def _get_version() -> str:
    try:
        return pkg_version("advisor-agent")
    except PackageNotFoundError:
        # Keep in sync with ``advisor/__init__.py`` so ``advisor --version``,
        # ``advisor status --json`` and ``advisor.__version__`` all agree when
        # the wheel isn't installed (e.g. running from an editable checkout
        # without metadata). Tests compare the two and will flag any drift.
        from . import __version__

        return __version__


def _fmt_action(component: str, action: str, path: object) -> str:
    label, fancy, plain, color = _style.ACTION_GLYPHS.get(action, (action, "?", "?", None))
    mark = _style.glyph(fancy, plain)
    if color:
        mark = _style.paint(mark, color)
    # Pad before coloring so columns line up regardless of ANSI width.
    component_col = f"{component:<6}"
    label_col = f"{label:<10}"
    component_col = _style.paint(component_col, "cyan", "bold")
    if color:
        label_col = _style.paint(label_col, color)
    path_str = _style.dim(str(path))
    return f"{mark} {component_col} {label_col} {path_str}"


def _relative_age(mtime_epoch: float, now_epoch: float | None = None) -> str:
    """Render an approximate age like ``2m ago`` / ``3h ago`` / ``5d ago``.

    Used by ``advisor checkpoints`` to surface staleness at a glance so a
    reader can pick the most recent run_id without parsing the 20-char
    ISO timestamp embedded in the id. Falls back to ``just now`` for
    sub-second deltas and caps at ``99d ago`` for very old files.
    ``now_epoch`` is an injection seam for tests — defaults to
    :func:`time.time` when omitted.
    """
    now = time.time() if now_epoch is None else now_epoch
    delta = max(0.0, now - mtime_epoch)
    if delta < 1.0:
        return "just now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    days = int(delta // 86400)
    return f"{min(days, 99)}d ago"


_MAX_RUNNERS_CEILING = 20


def _resolve_max_runners(raw: int | None) -> int:
    """Resolve the CLI ``--max-runners`` value to a concrete int.

    Mirrors :func:`default_team_config`'s env-var fallback so subcommands
    that need the runner count directly (without building a full
    :class:`TeamConfig`) see the same value the config would. Argparse
    defaults to ``None``; env var ``ADVISOR_MAX_RUNNERS`` (if set to a
    valid positive int) fills in; final fallback is 5. The result is
    clamped to ``_MAX_RUNNERS_CEILING`` so a typo or experimental value
    can't spawn an unbounded runner pool.
    """
    if raw is not None and raw >= 1:
        return min(raw, _MAX_RUNNERS_CEILING)
    env_raw = os.environ.get("ADVISOR_MAX_RUNNERS", "").strip()
    if env_raw:
        try:
            parsed = int(env_raw)
        except ValueError:
            parsed = 5
        resolved = parsed if parsed >= 1 else 5
        return min(resolved, _MAX_RUNNERS_CEILING)
    return 5


def _config_from_args(args: argparse.Namespace) -> TeamConfig:
    # Allow piping a large scope description into any subcommand via
    # `--context -` (or the literal string "-"), matching POSIX stdin
    # conventions. Explicit flag required so callers that accidentally
    # pipe into the CLI don't silently swallow stdin as context.
    context = args.context or ""
    if context == "-":
        if sys.stdin.isatty():
            print(
                _style.warning_box(
                    "--context -: no data on stdin; pipe the context in or drop the flag",
                    stream=sys.stderr,
                ),
                file=sys.stderr,
            )
            context = ""
        else:
            context = sys.stdin.read().strip()
    return default_team_config(
        target_dir=args.target,
        team_name=args.team,
        file_types=args.file_types,
        max_runners=args.max_runners,
        min_priority=args.min_priority,
        context=context,
        advisor_model=args.advisor_model,
        runner_model=args.runner_model,
        max_fixes_per_runner=getattr(args, "max_fixes_per_runner", _DEFAULT_MAX_FIXES),
        large_file_line_threshold=getattr(
            args, "large_file_line_threshold", _DEFAULT_LARGE_FILE_THRESHOLD
        ),
        large_file_max_fixes=getattr(args, "large_file_max_fixes", _DEFAULT_LARGE_FILE_MAX_FIXES),
        test_command=getattr(args, "test_cmd", "") or "",
        preset=getattr(args, "preset", None),
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Target directory to analyze (default: current directory)",
    )
    parser.add_argument("--team", default="review", help="Team name")
    parser.add_argument(
        "--file-types",
        default="*.py",
        help=(
            "Glob pattern matched against each path's filename during rglob "
            "(recursive). `*.py` already descends into subdirectories — do "
            "NOT pass `**/*.py`. Examples: `*.py`, `*.{py,pyi}`, `*.ts`."
        ),
    )
    parser.add_argument(
        "--max-runners",
        type=int,
        default=None,
        help=(
            "Advisory runner count. Opus may exceed this for large codebases. "
            "Env-var default: ADVISOR_MAX_RUNNERS (falls back to 5 when unset)."
        ),
    )
    parser.add_argument(
        "--min-priority",
        type=int,
        choices=range(1, 6),
        default=3,
        help="Minimum priority tier (1=utilities, 5=auth/secrets, default: %(default)s)",
    )
    parser.add_argument("--context", default="", help="Extra goal context")
    parser.add_argument(
        "--advisor-model",
        default="opus",
        help="Model for the advisor agent (default: %(default)s)",
    )
    parser.add_argument(
        "--runner-model",
        default="sonnet",
        help="Model for the runner pool agents (default: %(default)s)",
    )
    parser.add_argument(
        "--max-fixes-per-runner",
        type=int,
        default=_DEFAULT_MAX_FIXES,
        help=(
            "Hard cap on sequential fix assignments per runner before the "
            "advisor rotates to a fresh runner. Lower this (e.g. 3) if "
            "runners are exhausting context mid-fix-wave. Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--large-file-line-threshold",
        type=int,
        default=_DEFAULT_LARGE_FILE_THRESHOLD,
        help=(
            "Line count above which a file is considered 'large' for the "
            "tighter per-runner fix cap (see --large-file-max-fixes). "
            "Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--large-file-max-fixes",
        type=int,
        default=_DEFAULT_LARGE_FILE_MAX_FIXES,
        help=(
            "Effective fix cap for any batch containing a file at or above "
            "--large-file-line-threshold lines. Lowest applicable cap wins. "
            "Default: %(default)s"
        ),
    )
    parser.add_argument(
        "--preset",
        default=None,
        metavar="NAME",
        help=(
            "Apply a rule-pack preset (python-web, python-cli, node-api, "
            "typescript-react, go-service, rust-crate). See `advisor "
            "presets`."
        ),
    )


def cmd_pipeline(args: argparse.Namespace) -> int:
    """Print the full pipeline reference for the given target."""
    text = render_pipeline(_config_from_args(args))
    if getattr(args, "json", False):
        print(json.dumps({"schema_version": JSON_SCHEMA_VERSION, "text": text}))
        return 0
    print(_style.colorize_markdown(text))
    if not getattr(args, "quiet", False):
        print()
        print(_style.cta(f"/advisor {args.target}", "run the live pipeline in Claude Code"))
    return 0


def _safe_rglob(target: Path, pattern: str) -> tuple[list[str] | None, str | None]:
    """Return (paths, error). `error` is non-None on a malformed glob pattern
    or a filesystem error (e.g. symlink loops, permission denied).

    ``pattern`` may be a comma-separated list (e.g. ``"*.js,*.ts"``); each
    sub-pattern is expanded independently and results are merged, deduped,
    and sorted. Symlinks pointing outside ``target`` are skipped silently
    (per-entry ``.resolve().is_relative_to`` check, mirroring
    :func:`advisor._fs.safe_rglob_paths`). Per-entry ELOOP / permission
    errors skip that single entry rather than aborting the whole scan."""
    patterns = [p.strip() for p in pattern.split(",") if p.strip()]
    if not patterns:
        return [], None

    try:
        validate_file_types(pattern)
    except ValueError as exc:
        return None, f"invalid --file-types pattern: {exc}"

    try:
        # Resolve before walking — `Path.rglob` on an unresolved path
        # containing symlink cycles can hang on Python <3.13 instead of
        # raising. Resolving first canonicalizes the start point and lets
        # the OS-level walk surface ELOOP as an OSError we already catch.
        resolved_target = target.resolve()
    except OSError as exc:
        return None, f"filesystem error scanning {target}: {exc}"

    seen: set[str] = set()
    results: list[str] = []
    for pat in patterns:
        try:
            iterator = resolved_target.rglob(pat)
        except (ValueError, NotImplementedError) as exc:
            return None, f"invalid --file-types pattern {pat!r}: {exc}"
        except OSError as exc:
            return None, f"filesystem error scanning {target}: {exc}"
        for p in iterator:
            try:
                if p.is_file() and p.resolve().is_relative_to(resolved_target):
                    s = str(p)
                    if s not in seen:
                        seen.add(s)
                        results.append(s)
            except (OSError, ValueError):
                # Single broken symlink or permission-denied entry — skip it
                # rather than aborting the whole scan. Mirrors _fs.safe_rglob_paths.
                continue
    return results, None


def _pos_int_arg(value: str) -> int:
    """argparse ``type=`` validator — accept positive (>=1) integers only.

    Centralized so flags like ``--runner-id`` reject ``0`` instead of
    silently re-mapping it to ``1`` via a falsy guard at the call site.
    """
    try:
        n = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected positive integer, got {value!r}") from exc
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {n}")
    return n


def _valid_port(raw: str) -> int:
    """argparse ``type=`` validator for TCP port numbers (0..65535).

    Raises ``argparse.ArgumentTypeError`` on out-of-range or non-integer
    input so the CLI shows a clean usage error instead of a cryptic OS
    bind failure.
    """
    try:
        n = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid port {raw!r}: {exc}") from exc
    if not 0 <= n <= 65535:
        raise argparse.ArgumentTypeError(f"port {n} out of range — must be between 0 and 65535")
    return n


def _apply_exclude_patterns(target: Path, paths: list[str], patterns: list[str]) -> list[str]:
    """Filter ``paths`` by ``--exclude`` glob patterns.

    Patterns are evaluated against the target-relative path (``tests/foo.py``
    rather than ``/abs/…/tests/foo.py``) so user-written patterns match
    the way they intuitively expect. Reuses
    :func:`advisor.rank._matches_any_pattern` for parity with the
    ``.advisorignore`` semantics (including ``**`` globs and trailing-slash
    directory patterns).
    """
    from .rank import _matches_any_pattern

    try:
        target_resolved = target.resolve()
    except OSError:
        target_resolved = target
    kept: list[str] = []
    for fp in paths:
        try:
            rel = str(Path(fp).resolve().relative_to(target_resolved))
        except (ValueError, OSError):
            rel = fp
        # Check both the relative and absolute form — the user might
        # paste a literal absolute path for a one-off exclude.
        if _matches_any_pattern(rel, patterns) or _matches_any_pattern(fp, patterns):
            continue
        kept.append(fp)
    return kept


def _gitignore_missing_advisor_entry(target: Path) -> bool:
    """True when ``target/.gitignore`` exists but doesn't ignore ``.advisor/``.

    Used to emit a one-shot tip when the user first checkpoints a plan.
    We only nag when there's already a ``.gitignore`` in the target —
    projects that aren't tracked in git have nothing to accidentally
    commit. Common accepted forms (``.advisor``, ``.advisor/``,
    ``.advisor/*``) are all recognized to avoid false positives.
    """
    gi = target / ".gitignore"
    if not gi.is_file():
        return False
    try:
        content = gi.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    accepted = {
        ".advisor",
        ".advisor/",
        ".advisor/*",
        ".advisor/**",
        ".advisor/**/*",
        "/.advisor",
        "/.advisor/",
        "/.advisor/**",
    }
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s in accepted:
            return False
    return True


def _plan_to_dict(
    target: Path,
    tasks: list[FocusTask],
    batches: list[FocusBatch] | None = None,
    estimate: object | None = None,
    run_id: str | None = None,
) -> dict[str, object]:
    """Serialize a ranking/plan to a JSON-friendly dict for ``--json`` output.

    ``estimate`` should be a :class:`advisor.cost.CostEstimate` when cost
    estimation is enabled, ``None`` otherwise. ``run_id`` is only emitted
    when the plan was checkpointed so callers can pass it to ``--resume``.
    """
    task_data = [
        {
            "file_path": t.file_path,
            "priority": t.priority,
        }
        for t in tasks
    ]
    payload: dict[str, object] = {
        "schema_version": JSON_SCHEMA_VERSION,
        "target": str(target),
        "task_count": len(tasks),
        "tasks": task_data,
    }
    if batches is not None:
        payload["batches"] = [
            {
                "batch_id": b.batch_id,
                "complexity": b.complexity,
                "top_priority": b.top_priority,
                "tasks": [{"file_path": t.file_path, "priority": t.priority} for t in b.tasks],
            }
            for b in batches
        ]
    if estimate is not None and hasattr(estimate, "to_dict"):
        payload["estimate"] = estimate.to_dict()
    if run_id is not None:
        payload["run_id"] = run_id
    return payload


def _resolve_plan_files(
    target: Path,
    args: argparse.Namespace,
) -> tuple[list[str] | None, str | None]:
    """Resolve the file list for ``cmd_plan`` — git-scoped or full rglob.

    Returns ``(paths, error_text)``. ``paths`` is None when an error
    prevented resolution. Git-scope selectors (``--since``, ``--staged``,
    ``--branch``) take precedence over the recursive scan and are mutually
    exclusive with each other.

    Git-returned paths are filtered against ``--file-types`` using the same
    fnmatch semantics the recursive scan applies, so
    ``advisor plan --since main --file-types '*.py'`` does not surface
    unrelated markdown/yaml changes that happen to share the diff.
    """
    since = getattr(args, "since", None)
    staged = getattr(args, "staged", False)
    branch = getattr(args, "branch", None)
    if any([since, staged, branch]):
        try:
            files = resolve_git_scope(target, since=since, staged=staged, branch=branch)
        except GitScopeError as exc:
            return None, str(exc)
        if files is None:
            return [], None
        pattern = args.file_types
        if pattern and pattern != "*":
            import fnmatch

            pats = [p.strip() for p in pattern.split(",") if p.strip()]
            files = [p for p in files if any(fnmatch.fnmatch(Path(p).name, pat) for pat in pats)]
        return files, None
    return _safe_rglob(target, args.file_types)


def cmd_plan(args: argparse.Namespace) -> int:
    """Rank local files and print a batch dispatch plan — no agents spawned."""
    target = Path(args.target)
    if not target.exists():
        print(_style.error_box(f"target not found: {target}", stream=sys.stderr), file=sys.stderr)
        return 2

    # Resume: load a previously-saved plan from .advisor/run-<id>.json and
    # emit it verbatim. Skips discovery + ranking entirely — the whole
    # point of checkpointing is to not redo that work.
    resume_id = getattr(args, "resume", None)
    if resume_id:
        try:
            cp = load_checkpoint(target, resume_id)
        except (FileNotFoundError, ValueError) as exc:
            print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
            return 2
        tasks = [
            FocusTask(
                file_path=str(t["file_path"]),
                priority=int(str(t["priority"])),
                prompt=str(t.get("prompt", "")),
            )
            for t in cp.tasks
            if isinstance(t, dict) and "file_path" in t and "priority" in t
        ]
        batches_from_cp = _batches_from_checkpoint(cp) if cp.batches else None
        # Rebuild the TeamConfig from the checkpoint so downstream surfaces
        # (cost estimation, test_command) reflect the run being resumed
        # rather than whatever defaults happen to be on the current invocation.
        checkpoint_cfg = default_team_config(
            target_dir=str(target),
            team_name=cp.team_name,
            file_types=cp.file_types,
            max_runners=cp.max_runners,
            min_priority=cp.min_priority,
            context=cp.context,
            advisor_model=cp.advisor_model,
            runner_model=cp.runner_model,
            max_fixes_per_runner=cp.max_fixes_per_runner,
            large_file_line_threshold=cp.large_file_line_threshold,
            large_file_max_fixes=cp.large_file_max_fixes,
            test_command=cp.test_command,
            warn_unknown_model=False,
        )
        return _emit_plan(
            args,
            target,
            tasks,
            batches_from_cp,
            run_id=cp.run_id,
            context="resumed",
            resolved_config=checkpoint_cfg,
        )

    _quiet = getattr(args, "quiet", False)
    if not _quiet and sys.stderr.isatty():
        sys.stderr.write(_style.dim(f"scanning {target}…") + "\r")
        sys.stderr.flush()

    paths, glob_err = _resolve_plan_files(target, args)

    if not _quiet and sys.stderr.isatty():
        sys.stderr.write("\033[2K\r")
        sys.stderr.flush()

    if glob_err is not None:
        print(_style.error_box(glob_err, stream=sys.stderr), file=sys.stderr)
        return 2

    # ``--exclude`` complements ``.advisorignore`` for ad-hoc filtering.
    # Patterns are applied against the target-relative path so users can
    # write ``--exclude 'tests/**'`` without worrying about the absolute
    # prefix. We filter here (not inside ``rank_files``) because the
    # rank matcher anchors at the start of the path, which doesn't play
    # nicely with the absolute paths ``_safe_rglob`` returns. Falls back
    # to absolute matching when relativization fails (symlinks, etc.).
    exclude_patterns: list[str] = list(getattr(args, "exclude", []) or [])
    if exclude_patterns and paths:
        paths = _apply_exclude_patterns(target, paths, exclude_patterns)

    # History-informed ranking — E9 history gets consumed here so repeat
    # offenders float up the plan. ``--no-history`` disables the boost
    # for deterministic CI (and for users who don't want the coupling).
    history_bonus: dict[str, float] | None = None
    history_count_map: dict[str, int] | None = None
    if not getattr(args, "no_history", False):
        hp = history_path(target)
        entries = load_recent_findings(hp, limit=500)
        if entries:
            history_bonus = file_repeat_scores(entries)
            from .history import file_repeat_counts

            history_count_map = file_repeat_counts(entries, window_days=90.0)

    # Preset-based keyword overlay — layered on top of language-aware
    # baseline when --preset is given. The file-type / min-priority
    # merging happens in default_team_config; the keyword overlay is
    # applied here because rank_files takes it as a parameter.
    preset_extras: dict[int, tuple[str, ...]] | None = None
    preset_name = getattr(args, "preset", None)
    # Reject an explicit empty-string preset (e.g. ``--preset=``) instead of
    # silently skipping — argparse's default is None, so an empty string here
    # means the caller typed ``--preset=`` and almost certainly wants an
    # error, not a stealth "no preset applied".
    if preset_name == "":
        print(
            _style.error_box(
                "--preset requires a non-empty value; "
                "run 'advisor presets' to list available packs",
                stream=sys.stderr,
            ),
            file=sys.stderr,
        )
        return 2
    if preset_name:
        from .presets import get_preset

        try:
            rule_pack = get_preset(preset_name)
        except ValueError as exc:
            print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
            return 2
        preset_extras = dict(rule_pack.extra_keywords_by_tier)

    ranked = rank_files(
        paths or [],
        read_fn=_read_head,
        ignore_patterns=load_advisorignore(target),
        extra_keywords=preset_extras,
        history_scores=history_bonus,
        history_counts=history_count_map,
    )
    tasks = create_focus_tasks(
        ranked,
        max_tasks=None,  # no hard cap; advisor decides in the live pipeline
        min_priority=args.min_priority,
    )

    batches: list[FocusBatch] | None = None
    if args.batch_size and args.batch_size >= 1:
        batches = create_focus_batches(tasks, files_per_batch=args.batch_size)

    # Resolve config exactly once. _config_from_args may consume stdin
    # (when ``--context -``); a second resolution would silently see an
    # empty context. Building it up-front lets every downstream caller
    # — checkpoint write, SARIF stub, _emit_plan's cost estimate — share
    # one resolved view of the args.
    cfg = _config_from_args(args)

    # Optional persistence: ``--checkpoint`` writes the full plan to
    # ``.advisor/run-<id>.json`` so a later invocation can ``--resume``.
    saved_run_id: str | None = None
    if getattr(args, "checkpoint", False):
        saved_run_id = new_run_id()
        save_checkpoint(
            target,
            run_id=saved_run_id,
            tasks=tasks,
            batches=batches,
            team_name=cfg.team_name,
            file_types=cfg.file_types,
            min_priority=cfg.min_priority,
            max_runners=cfg.max_runners,
            advisor_model=cfg.advisor_model,
            runner_model=cfg.runner_model,
            max_fixes_per_runner=cfg.max_fixes_per_runner,
            large_file_line_threshold=cfg.large_file_line_threshold,
            large_file_max_fixes=cfg.large_file_max_fixes,
            test_command=cfg.test_command,
            context=cfg.context,
        )
        # One-shot gitignore nudge: checkpoints live under ``.advisor/``,
        # which users often don't realize is a writable dir until they see
        # it in ``git status``. We only hint when there's already a
        # ``.gitignore`` (i.e. git-tracked project) that doesn't cover it.
        if not getattr(args, "quiet", False) and _gitignore_missing_advisor_entry(target):
            print(
                _style.tip("add `.advisor/` to .gitignore so checkpoints + history stay local"),
                file=sys.stderr,
            )

    sarif_path = getattr(args, "sarif", None)
    if sarif_path is not None:
        # Plan runs before the live pipeline produces findings, so we emit
        # an empty-results SARIF document representing "advisor ran, no
        # findings recorded". CI workflows can still upload it to keep the
        # Code Scanning artifact slot populated; the dirty-run artifact is
        # produced later by ``advisor audit --sarif``.
        rc = _write_sarif(Path(sarif_path), [], target)
        if rc is not None:
            return rc

    return _emit_plan(args, target, tasks, batches, run_id=saved_run_id, resolved_config=cfg)


def _batches_from_checkpoint(cp: Checkpoint) -> list[FocusBatch]:
    """Reconstruct :class:`FocusBatch` objects from a loaded checkpoint."""
    out: list[FocusBatch] = []
    for b in cp.batches:
        raw_tasks = b.get("tasks", [])
        if not isinstance(raw_tasks, list):
            continue
        batch_tasks = tuple(
            FocusTask(
                file_path=str(t["file_path"]),
                priority=int(str(t["priority"])),
                prompt=str(t.get("prompt", "")),
            )
            for t in raw_tasks
            if isinstance(t, dict) and "file_path" in t and "priority" in t
        )
        try:
            batch_id_val = int(str(b.get("batch_id", 0)))
        except ValueError:
            warnings.warn(
                f"skipping checkpoint batch with non-numeric batch_id {b.get('batch_id')!r}",
                UserWarning,
                stacklevel=2,
            )
            continue
        out.append(
            FocusBatch(
                batch_id=batch_id_val,
                tasks=batch_tasks,
                complexity=str(b.get("complexity", "")),
            )
        )
    return out


def _count_lines(target: Path, file_path: str) -> int:
    """Return the line count for a file under ``target``, or 0 on error.

    Used by the pre-flight budget validator to decide which files trip
    the ``large_file_line_threshold``. Failing reads return 0 (i.e. the
    file isn't treated as large) rather than raising — a missing file
    count is a soft signal, not a hard block.
    """
    try:
        fp = Path(file_path)
        is_abs = fp.is_absolute() or (
            len(file_path) >= 2 and file_path[1] == ":" and file_path[0].isalpha()
        )
        p = fp if is_abs else (target / file_path)
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _emit_plan(
    args: argparse.Namespace,
    target: Path,
    tasks: list[FocusTask],
    batches: list[FocusBatch] | None,
    *,
    run_id: str | None = None,
    context: str = "",
    resolved_config: TeamConfig | None = None,
) -> int:
    """Shared rendering logic — JSON or pretty, optional --output FILE.

    ``resolved_config`` lets resume paths hand in the checkpoint's config so
    cost estimates (and anything else that derives from the run config) use
    the resumed-run values rather than the current CLI defaults.
    """

    def _cfg() -> TeamConfig:
        return resolved_config if resolved_config is not None else _config_from_args(args)

    def _budget_warnings() -> list[str]:
        """Pre-flight: flag batches that could over-run the per-runner fix cap.

        Only runs when we actually have batches — a raw task list has no
        per-batch cap to check against. Reads line counts lazily so the
        large-file cap is honored too.
        """
        if not batches:
            return []
        cfg = _cfg()
        counts = {t.file_path: _count_lines(target, t.file_path) for t in tasks}
        return check_batch_fix_budget(batches, cfg, file_line_counts=counts)

    # ``--pricing FILE`` overrides the default per-family pricing table
    # for cost estimates. Loaded once so both the JSON and pretty branches
    # see the same override (and parse errors fail fast with a clear box).
    pricing_path = getattr(args, "pricing", None)
    pricing_override: dict[str, tuple[int, int]] | None = None
    if pricing_path:
        try:
            pricing_override = load_pricing(pricing_path)
        except ValueError as exc:
            print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
            return 2

    if getattr(args, "output", "") and not getattr(args, "json", False):
        print(
            _style.warning_box(
                "--output is ignored without --json; pretty output still goes to stdout",
                stream=sys.stderr,
            ),
            file=sys.stderr,
        )

    if getattr(args, "json", False):
        estimate = None
        if getattr(args, "estimate", False):
            cfg = _cfg()
            estimate = estimate_cost(
                tasks,
                batches,
                advisor_model=cfg.advisor_model,
                runner_model=cfg.runner_model,
                max_fixes_per_runner=cfg.max_fixes_per_runner,
                max_runners=cfg.max_runners,
                pricing=pricing_override,
            )
        payload = _plan_to_dict(target, tasks, batches, estimate=estimate, run_id=run_id)
        warnings = _budget_warnings()
        if warnings:
            payload["budget_warnings"] = warnings
        rendered = json.dumps(payload, indent=2)
        output_file = getattr(args, "output", None)
        if output_file:
            try:
                _atomic_write(Path(output_file), rendered + "\n")
            except OSError as exc:
                print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
                return 2
            if not getattr(args, "quiet", False):
                print(_style.dim(f"wrote plan to {output_file}"))
        else:
            print(rendered)
        return 0

    if not tasks:
        # The git-scope hint applies to any non-resumed run that used a
        # git scope flag — earlier this gated on ``context == ""`` which
        # masked the hint on the ``"resumed"`` path even when the resumed
        # plan was itself git-scoped. Use a positive predicate instead.
        hint = (
            "--since/--staged/--branch selection"
            if context != "resumed" and _is_git_scoped(args)
            else None
        )
        if hint:
            print(_style.dim(f"no files matched the {hint}"))
            print(_style.tip("try a broader git scope or remove the filter"))
        else:
            print(_style.dim(f"no files at priority P{args.min_priority}+ in {target}"))
            print(_style.tip("try --min-priority 1 to include all files"))
            print(_style.tip("or adjust --file-types to match your file extensions"))
        return 0

    if batches:
        print(_style.colorize_markdown(format_batch_plan(batches)))
    else:
        print(_style.colorize_markdown(format_dispatch_plan(tasks)))

    # Budget warnings print on stderr (so they don't pollute pipes) but
    # are also included in --json output for programmatic consumers.
    for warning in _budget_warnings():
        print(_style.warning_box(f"budget: {warning}"), file=sys.stderr)

    if getattr(args, "estimate", False):
        cfg = _cfg()
        est = estimate_cost(
            tasks,
            batches,
            advisor_model=cfg.advisor_model,
            runner_model=cfg.runner_model,
            max_fixes_per_runner=cfg.max_fixes_per_runner,
            max_runners=cfg.max_runners,
            pricing=pricing_override,
        )
        print()
        print(_style.colorize_markdown(format_estimate(est)))

    if run_id:
        print()
        # Green success glyph + bold run_id for fast scanning in Claude
        # Code transcripts, with an immediate hint for the next step so
        # users don't have to guess the exact ``--resume`` flag name.
        print(_style.success_box(f"checkpoint saved: {run_id}"))
        print(_style.tip(f"resume with: advisor plan --resume {run_id}"))

    print()
    print(_style.cta(f"/advisor {target}", "run the live pipeline in Claude Code"))
    return 0


def _is_git_scoped(args: argparse.Namespace) -> bool:
    return bool(
        getattr(args, "since", None)
        or getattr(args, "staged", False)
        or getattr(args, "branch", None)
    )


def cmd_prompt(args: argparse.Namespace) -> int:
    """Print a specific step's prompt so it can be pasted into Claude Code."""
    config = _config_from_args(args)
    quiet = getattr(args, "quiet", False)
    as_json = getattr(args, "json", False)
    # TTY-only framing: interactive users see a dim banner announcing what
    # they're looking at; piped output (curl, redirect, pbcopy) stays clean
    # so the prompt can be consumed programmatically.
    show_frame = sys.stdout.isatty() and not quiet and not as_json
    if args.step == "advisor":
        if show_frame:
            print(_style.dim(f"# advisor prompt — paste into Claude Code (target: {args.target})"))
            print()
        # Include recent history if available — gives the advisor longitudinal
        # awareness of past findings. Disabled via --no-history.
        history_block = ""
        if not getattr(args, "no_history", False):
            try:
                entries = load_recent(args.target, limit=20)
                if entries:
                    history_block = "\n\n" + format_history_block(entries)
            except (OSError, ValueError):
                history_block = ""
        text = build_advisor_prompt(config, history_block=history_block)
    elif args.step == "runner":
        runner_id = getattr(args, "runner_id", 1)
        if show_frame:
            print(_style.dim(f"# runner-{runner_id} prompt — paste into Claude Code"))
            print()
        text = build_runner_pool_prompt(runner_id, config)
    else:  # verify
        # Consistent no-findings behavior: whether stdin is a TTY (no data
        # piped) or an empty pipe, fall back to the same placeholder string
        # so the rendered prompt is always a valid template. Only emit the
        # warning when the user actually piped something empty — a TTY
        # invocation is the intended "print the template" path.
        if sys.stdin.isatty():
            findings = "<paste findings here>"
        else:
            piped = sys.stdin.read()
            if not piped.strip():
                print(
                    _style.warning_box(
                        "--step verify received no findings on stdin; output will be a template",
                        stream=sys.stderr,
                    ),
                    file=sys.stderr,
                )
                findings = "<paste findings here>"
            else:
                findings = piped
        resolved_max_runners = _resolve_max_runners(args.max_runners)
        text = build_verify_dispatch_prompt(
            findings,
            file_count=args.file_count or resolved_max_runners,
            runner_count=resolved_max_runners,
        )
    if as_json:
        print(json.dumps({"schema_version": JSON_SCHEMA_VERSION, "step": args.step, "text": text}))
        return 0
    print(text)
    if show_frame:
        print()
        print(_style.cta("next", "paste into Claude Code"))
    return 0


# Exit codes for install/uninstall:
#   0 — changed (installed / updated / removed)
#   0 — no-op under idempotent semantics (unchanged / absent) by default
#   3 — no-op when --strict is passed (lets automation distinguish)
_STRICT_NOOP_EXIT = 3
_NOOP_ACTIONS = frozenset({InstallAction.UNCHANGED.value, InstallAction.ABSENT.value})


def _component_line(c: ComponentStatus) -> str:
    key = "ok" if c.present and c.current else "outdated" if c.present else "missing"
    _, fancy, ascii_, color = _style.STATE_GLYPHS[key]
    # Use component-status vocabulary ("installed") rather than STATE_GLYPHS label ("ok").
    component_label = {"ok": "installed", "outdated": "outdated", "missing": "missing"}[key]
    mark = (
        _style.paint(_style.glyph(fancy, ascii_), color) if color else _style.glyph(fancy, ascii_)
    )
    state = _style.paint(component_label, color) if color else component_label
    name_col = _style.paint(f"{c.name:<6}", "cyan", "bold")
    return f"  {mark} {name_col} {state:<10} {_style.dim(str(c.path))}"


def _format_status(s: Status, version: str) -> str:
    lines = [
        _style.header_block(f"advisor {version}", [], width=52),
        _component_line(s.nudge),
        _component_line(s.skill),
    ]
    if s.opt_out:
        warn = _style.paint(_style.glyph("⚠", "!"), "yellow")
        lines.append(f"  {warn} auto-install disabled ({OPT_OUT_ENV} set)")
    if not (s.nudge.present and s.skill.present):
        lines.append(_style.cta("fix", "advisor install"))
    elif not (s.nudge.current and s.skill.current):
        lines.append(_style.cta("fix", "advisor install  (refresh outdated bits)"))
    return "\n".join(lines)


def _status_to_dict(
    s: Status, version: str, installed_skill_version: str | None = None
) -> dict[str, object]:
    """Serialize status to a JSON-friendly dict for ``--json`` output."""

    def _c(c: ComponentStatus) -> dict[str, object]:
        return {
            "name": c.name,
            "path": str(c.path),
            "present": c.present,
            "current": c.current,
        }

    skill_block = _c(s.skill)
    # Surface the version declared by the installed skill's badge (if any) so
    # scripts can distinguish "outdated" from "brand new". None = predates
    # the badge convention (<= 0.4.0) or file unreadable.
    skill_block["installed_version"] = installed_skill_version

    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "version": version,
        "nudge": _c(s.nudge),
        "skill": skill_block,
        "opt_out": s.opt_out,
        "healthy": (s.nudge.present and s.nudge.current and s.skill.present and s.skill.current),
    }


def cmd_status(args: argparse.Namespace) -> int:
    """Print a colored health summary of the local advisor install."""
    nudge_target = Path(args.path) if args.path else None
    skill_target = Path(args.skill_path) if args.skill_path else None
    s = get_status(nudge_path=nudge_target, skill_path=skill_target)
    installed = get_installed_skill_version(path=skill_target)
    healthy = s.nudge.present and s.nudge.current and s.skill.present and s.skill.current

    if getattr(args, "json", False):
        print(json.dumps(_status_to_dict(s, _get_version(), installed), indent=2))
    else:
        print(_format_status(s, _get_version()))
        if healthy:
            print()
            print(_style.cta("/advisor <path>", "run the advisor on a codebase"))

    if getattr(args, "strict", False) and not healthy:
        return _STRICT_NOOP_EXIT
    return 0


def _run_install_op(
    args: argparse.Namespace,
    nudge_fn: Callable[..., InstallResult],
    skill_fn: Callable[..., InstallResult],
    trailing_cta: tuple[str, str] | None,
) -> int:
    """Shared body for ``install`` / ``uninstall``: call nudge + skill ops,
    print per-component status lines, honor ``--skip-skill``/``--strict``
    /``--quiet`` flags, and emit a trailing call-to-action.
    """
    nudge_target = Path(args.path) if args.path else None
    skill_target = Path(args.skill_path) if args.skill_path else None
    quiet = getattr(args, "quiet", False)

    try:
        nudge_result = nudge_fn(path=nudge_target)
    except (OSError, UnicodeDecodeError) as exc:
        print(_style.error_box(f"nudge: {exc}", stream=sys.stderr), file=sys.stderr)
        return 1
    if not quiet:
        print(_fmt_action("nudge", nudge_result.action, nudge_result.path))

    if args.skip_skill:
        skill_action: str = InstallAction.SKIPPED.value
    else:
        try:
            skill_result = skill_fn(path=skill_target)
        except (OSError, UnicodeDecodeError) as exc:
            print(_style.error_box(f"skill: {exc}", stream=sys.stderr), file=sys.stderr)
            return 1
        skill_action = skill_result.action
        if not quiet:
            print(_fmt_action("skill", skill_result.action, skill_result.path))

    if args.strict and (
        nudge_result.action in _NOOP_ACTIONS
        and skill_action in (*_NOOP_ACTIONS, InstallAction.SKIPPED.value)
    ):
        return _STRICT_NOOP_EXIT
    if trailing_cta and not quiet:
        print()
        print(_style.cta(*trailing_cta))
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Install the /advisor skill AND append the CLAUDE.md nudge."""
    if args.check:
        nudge_target = Path(args.path) if args.path else None
        skill_target = Path(args.skill_path) if args.skill_path else None
        s = get_status(nudge_path=nudge_target, skill_path=skill_target)
        installed = get_installed_skill_version(path=skill_target)
        quiet = getattr(args, "quiet", False)
        if getattr(args, "json", False):
            print(json.dumps(_status_to_dict(s, _get_version(), installed), indent=2))
        elif not quiet:
            print(_format_status(s, _get_version()))
        ok = s.nudge.present and s.nudge.current and s.skill.present and s.skill.current
        return 0 if ok else _STRICT_NOOP_EXIT

    return _run_install_op(
        args,
        install_nudge,
        install_skill,
        ("/advisor <path>", "run the advisor on a codebase"),
    )


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove the /advisor skill AND the CLAUDE.md nudge block."""
    return _run_install_op(
        args,
        uninstall_nudge,
        uninstall_skill,
        ("advisor install", "reinstall if you change your mind"),
    )


_PROTOCOL_TEXT = """# Advisor team lifecycle protocol

Strict sequence for any Claude Code session using the /advisor skill.
Deviating (e.g. shutting down with broadcast `"*"`, forgetting TeamDelete,
or spawning runners before the advisor) breaks the pipeline.

1. TeamCreate(name="review")

2. Spawn advisor FIRST (no runners yet):
   Agent(name="advisor", description="Investigate, rank, and dispatch runners",
         model="opus", subagent_type="deep-reasoning",
         team_name="review", prompt=<build_advisor_prompt(config)>)

3. Advisor does Glob+Grep discovery, ranks P1–P5, decides runner pool size,
   THEN tells you to spawn N runners:
   Agent(name="runner-<i>", description="Pool runner <i> — waits for advisor dispatch",
         model="sonnet", subagent_type="code-review",
         team_name="review", run_in_background=true,
         prompt=<build_runner_pool_prompt(i, config)>)

4. Advisor dispatches explore assignments, verifies each runner reply as it
   lands, optionally dispatches fix assignments, then sends the final
   structured report to team-lead.

5. Shut down teammates INDIVIDUALLY (broadcast "*" with structured messages
   fails silently):
     SendMessage({"to": "advisor",  "message": {"type": "shutdown_request"}})
     SendMessage({"to": "runner-1", "message": {"type": "shutdown_request"}})
     ...
     SendMessage({"to": "runner-N", "message": {"type": "shutdown_request"}})

6. TeamDelete()

Names and models shown here are the defaults (team "review", models
"opus" / "sonnet"). Override them via `--team`, `--advisor-model`,
`--runner-model` on the CLI; `advisor pipeline <dir>` renders the
concrete call sites for a given config.
"""


def cmd_protocol(args: argparse.Namespace) -> int:
    """Print the strict team-lifecycle protocol as an ad-hoc reference."""
    if getattr(args, "json", False):
        print(json.dumps({"schema_version": JSON_SCHEMA_VERSION, "text": _PROTOCOL_TEXT}))
        return 0
    print(_style.colorize_markdown(_PROTOCOL_TEXT))
    if not getattr(args, "quiet", False):
        print(_style.cta("next", "advisor pipeline ."))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Extended diagnostic: status + git/claude/python/env checks."""
    nudge_target = Path(args.path) if args.path else None
    skill_target = Path(args.skill_path) if args.skill_path else None
    report = run_doctor(
        nudge_path=nudge_target,
        skill_path=skill_target,
        version=_get_version(),
    )
    if getattr(args, "json", False):
        payload = {"schema_version": JSON_SCHEMA_VERSION, **report.to_dict()}
        print(json.dumps(payload, indent=2))
    else:
        print(format_report(report))
        if not getattr(args, "quiet", False):
            print()
            if report.healthy:
                print(_style.cta("run", "advisor pipeline ."))
            else:
                print(_style.cta("fix", "advisor install"))
    strict = getattr(args, "strict", False)
    if strict and not report.healthy:
        return _STRICT_NOOP_EXIT
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    """Launch the optional local web dashboard on 127.0.0.1.

    The web module is imported lazily — users who never run ``advisor ui``
    pay no import cost. Since the dashboard is built on stdlib
    :mod:`http.server`, it works out of the box with no extra install. If a
    future, heavier dashboard implementation requires a third-party package
    we surface the missing-extras hint here.
    """
    target = Path(args.target)
    if not target.exists():
        print(_style.error_box(f"target not found: {target}", stream=sys.stderr), file=sys.stderr)
        return 2
    if not target.is_dir():
        print(
            _style.error_box(f"target is not a directory: {target}", stream=sys.stderr),
            file=sys.stderr,
        )
        return 2

    if getattr(args, "json", False):
        if args.port == 0:
            print(
                _style.error_box(
                    "--json cannot be combined with --port 0 because no server is bound",
                    stream=sys.stderr,
                ),
                file=sys.stderr,
            )
            return 2
        url = f"http://{args.host}:{args.port}"
        print(json.dumps({"schema_version": JSON_SCHEMA_VERSION, "url": url}))
        return 0

    try:
        from .web import build_app_state, run_server
    except ImportError as exc:
        print(
            _style.error_box(
                "advisor ui requires the optional `ui` extra.\n"
                f"Install with: pip install 'advisor-agent[ui]'\n\n(detail: {exc})",
                stream=sys.stderr,
            ),
            file=sys.stderr,
        )
        return 1

    state = build_app_state(
        target,
        file_types=args.file_types,
        min_priority=args.min_priority,
        max_runners=_resolve_max_runners(args.max_runners),
        advisor_model=args.advisor_model,
        runner_model=args.runner_model,
    )
    try:
        run_server(
            state,
            host=args.host,
            port=args.port,
            log_requests=args.verbose,
        )
    except OSError as exc:
        print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
        return 1
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Show recent CONFIRMED findings from ``.advisor/history.jsonl``."""
    target = Path(args.target)
    entries = load_recent(target, limit=args.limit)
    if getattr(args, "json", False):
        payload = {
            "schema_version": JSON_SCHEMA_VERSION,
            "target": str(target),
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
        print(json.dumps(payload, indent=2))
        return 0
    if not entries:
        # Friendlier empty state — readers landing here for the first time
        # often don't know that history is written lazily as findings get
        # confirmed during a live run. The dim tip keeps the CTA discoverable
        # without competing visually with the primary message.
        print(_style.dim(f"no history yet at {target}/.advisor/history.jsonl"))
        print(_style.tip("findings are logged when you confirm them during a run"))
        return 0
    print(_style.colorize_markdown(format_history_block(entries)))
    if not getattr(args, "quiet", False):
        print()
        print(_style.cta("next", "advisor pipeline ."))
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    """Print version + environment details (lighter-weight than ``doctor``).

    Mirrors ``advisor --version`` but also reports the Python version,
    the install path of the package, and the JSON schema version so
    scripts can pin against both. ``--json`` produces a stable payload.
    """
    import platform

    pkg_root = Path(__file__).resolve().parent
    info: dict[str, str] = {
        "schema_version": JSON_SCHEMA_VERSION,
        "advisor_version": _get_version(),
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "install_path": str(pkg_root),
        "platform": platform.platform(),
    }
    if getattr(args, "json", False):
        print(json.dumps(info, indent=2))
        return 0
    print(
        _style.header_block(
            f"advisor {info['advisor_version']}",
            [
                ("python", f"{info['python_version']} ({info['python_implementation']})"),
                ("platform", info["platform"]),
                ("install", info["install_path"]),
                ("schema", info["schema_version"]),
            ],
            width=52,
        )
    )
    if not getattr(args, "quiet", False):
        print()
        print(_style.cta("docs", "advisor protocol"))
    return 0


def cmd_checkpoints(args: argparse.Namespace) -> int:
    """List or delete saved ``.advisor/run-<id>.json`` checkpoints.

    Default action is to list newest-first. ``--rm RUN_ID`` deletes a
    single checkpoint; ``--clear`` deletes all of them. Both destructive
    actions are idempotent — removing a nonexistent run_id is a no-op.
    ``--json`` is supported for the list form so scripts can pick a
    ``run_id`` to ``--resume``.
    """
    target = Path(args.target)
    rm_id = getattr(args, "rm", None)
    clear = getattr(args, "clear", False)
    if rm_id and clear:
        print(
            _style.error_box("--rm and --clear are mutually exclusive", stream=sys.stderr),
            file=sys.stderr,
        )
        return 2

    if rm_id:
        try:
            path = checkpoint_path(target, rm_id)
        except ValueError as exc:
            print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
            return 2
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
                return 2
            if not getattr(args, "quiet", False):
                print(_style.success_box(f"removed checkpoint {rm_id}"))
        elif not getattr(args, "quiet", False):
            print(_style.dim(f"no checkpoint {rm_id} at {path}"))
        return 0

    if clear:
        ids = list_checkpoints(target)
        removed = 0
        failed = 0
        for rid in ids:
            path = checkpoint_path(target, rid)
            try:
                path.unlink()
                removed += 1
            except OSError:
                failed += 1
        if not getattr(args, "quiet", False):
            if removed or failed:
                noun = "checkpoint" if removed == 1 else "checkpoints"
                msg = f"removed {removed} {noun}"
                if failed:
                    msg += f", failed {failed}"
                print(_style.success_box(msg) if not failed else _style.warning_box(msg))
            else:
                print(_style.dim("no checkpoints to remove"))
        return 0 if failed == 0 else 1

    ids = list_checkpoints(target)
    if getattr(args, "json", False):
        payload = {
            "schema_version": JSON_SCHEMA_VERSION,
            "target": str(target),
            "count": len(ids),
            "run_ids": ids,
        }
        print(json.dumps(payload, indent=2))
        return 0
    if not ids:
        # Empty state: point the reader at the flag that populates this
        # directory so the command isn't a dead-end when run on a fresh
        # checkout. Matches the tone of the history empty-state above.
        print(_style.dim(f"no checkpoints yet at {target}/.advisor/"))
        print(_style.tip("save one with: advisor plan --checkpoint"))
        return 0
    print(_style.colorize_markdown(f"## Checkpoints ({len(ids)})"))
    # Widest run_id + the backtick quotes, so the age/path columns line up
    # regardless of suffix length variations across checkpoints. Aligning
    # in Python space (not with a Markdown table) keeps the output
    # pipe-friendly while still reading as columns in Claude Code.
    id_col_width = max(len(rid) for rid in ids) + 2  # +2 for the backticks
    age_col_width = 10  # fits "just now" / "99d ago"
    for rid in ids:
        path = checkpoint_path(target, rid)
        try:
            age = _relative_age(path.stat().st_mtime)
        except OSError:
            age = ""
        id_cell = f"`{rid}`".ljust(id_col_width)
        age_cell = _style.dim(age.ljust(age_col_width))
        print(f"- {id_cell}  {age_cell}  {_style.dim(str(path))}")
    if not getattr(args, "quiet", False):
        print()
        print(_style.cta("resume", "advisor plan --resume <RUN_ID>"))
    return 0


# --fail-on exit-code: CI gating. ``never`` (default) keeps backward
# compatibility; any finding at or above the threshold produces exit 4
# so wrappers can differentiate a clean run from a dirty one.
_FAIL_ON_CHOICES = ("never", "low", "medium", "high", "critical")
_FAIL_ON_RANK: dict[str, int] = {
    "never": 99,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}
_SEVERITY_RANK: dict[str, int] = {
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
    "CRITICAL": 4,
}
_FAIL_ON_EXIT_CODE = 4


def _fail_on_findings(
    threshold: str | None,
    findings: Sequence[object],
) -> int | None:
    """Return ``_FAIL_ON_EXIT_CODE`` if any finding meets/exceeds ``threshold``.

    ``threshold`` is one of ``_FAIL_ON_CHOICES`` or ``None``. ``None`` or
    ``"never"`` returns ``None`` (no exit-code override). ``findings`` is
    any iterable of :class:`~advisor.verify.Finding`-shaped objects.
    """
    if not threshold or threshold == "never":
        return None
    gate = _FAIL_ON_RANK.get(threshold, 99)
    for f in findings:
        sev_attr = getattr(f, "severity", None)
        if not isinstance(sev_attr, str):
            continue
        rank = _SEVERITY_RANK.get(sev_attr.upper(), 0)
        if rank >= gate:
            return _FAIL_ON_EXIT_CODE
    return None


def _log_info(message: str) -> None:
    """One-line INFO log to stderr (no logging framework needed).

    Used for per-suppression reporting so operators can see what was
    dropped without dragging in Python's logging config for one feature.
    """
    print(_style.dim(f"info: {message}"), file=sys.stderr)


def _replace_findings(
    report: AuditReport,
    kept: Sequence[object],
) -> AuditReport:
    """Return an :class:`AuditReport` with ``findings_in_batch`` replaced.

    Separate helper so baseline / suppression wiring doesn't have to
    reach into the report's internals.
    """
    import dataclasses

    from .verify import Finding as _Finding

    typed: list[_Finding] = [f for f in kept if isinstance(f, _Finding)]
    return dataclasses.replace(report, findings_in_batch=typed)


def _write_sarif(
    sarif_path: Path,
    findings: list[object],
    target_dir: Path,
) -> int | None:
    """Write a SARIF 2.1.0 document for ``findings`` to ``sarif_path``.

    Returns ``None`` on success, a non-zero exit code on IO or validation
    failure (error message printed to stderr).
    """
    from .verify import Finding as _Finding

    typed: list[_Finding] = [f for f in findings if isinstance(f, _Finding)]
    try:
        doc = findings_to_sarif(typed, tool_version=_get_version(), target_dir=target_dir)
    except ValueError as exc:
        print(_style.error_box(f"sarif: {exc}", stream=sys.stderr), file=sys.stderr)
        return 1
    try:
        _atomic_write(sarif_path, json.dumps(doc, indent=2) + "\n")
    except OSError as exc:
        print(_style.error_box(f"sarif: {exc}", stream=sys.stderr), file=sys.stderr)
        return 1
    return None


def _load_findings_from_input(
    source: Path | None,
) -> tuple[list[object], int | None]:
    """Load findings from ``source`` (JSONL of findings), or stdin if ``None``.

    Returns (findings, error_exit_code). On error, returns ``([], code)``.
    Accepts both the raw parser format (markdown dump) and a JSON array.
    """
    from .verify import Finding, parse_findings_from_text

    try:
        if source is None:
            if sys.stdin.isatty():
                return [], None
            text = sys.stdin.read()
        else:
            text = Path(source).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
        return [], 2
    # Try JSON first (from `advisor audit --json`); fall back to markdown.
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            doc = json.loads(stripped)
        except json.JSONDecodeError:
            # Looked like JSON but isn't — fall through to the markdown
            # parser. A markdown report can legitimately start with `[` (e.g.
            # `[CRITICAL] file.py:10 ...`); silently returning an empty list
            # would swallow real findings.
            doc = None
        if doc is not None:
            unrecognized_dict_keys: list[str] | None = None
            if isinstance(doc, dict):
                if "findings_in_batch" in doc:
                    raw = doc.get("findings_in_batch") or []
                elif "findings" in doc:
                    raw = doc.get("findings") or []
                else:
                    # Top-level dict with neither expected key — record the
                    # keys we did see so the warning below can hint at the
                    # mismatch instead of silently returning zero findings.
                    raw = []
                    unrecognized_dict_keys = sorted(doc.keys())
            elif isinstance(doc, list):
                raw = doc
            else:
                raw = []
            findings: list[object] = []
            for f in raw:
                if not isinstance(f, dict):
                    continue
                try:
                    raw_rule_id = f.get("rule_id")
                    rule_id = (
                        raw_rule_id.strip()
                        if isinstance(raw_rule_id, str) and raw_rule_id.strip()
                        else None
                    )
                    findings.append(
                        Finding(
                            file_path=str(f["file_path"]),
                            severity=str(f["severity"]),
                            description=str(f["description"]),
                            evidence=str(f.get("evidence", "")),
                            fix=str(f.get("fix", "")),
                            rule_id=rule_id,
                        )
                    )
                except KeyError:
                    continue
            # Surface "parsed JSON but recognized no findings" so a
            # mis-shaped input (wrong top-level key, or an array of
            # non-dicts) doesn't silently no-op the audit/baseline/etc.
            # caller. Stays warning-only — the empty list is still
            # returned so callers' empty-handling stays in charge.
            if not findings:
                if unrecognized_dict_keys is not None:
                    print(
                        _style.warning_box(
                            "JSON input has no 'findings' or 'findings_in_batch' key; "
                            f"got keys: {unrecognized_dict_keys}",
                            stream=sys.stderr,
                        ),
                        file=sys.stderr,
                    )
                elif raw:
                    print(
                        _style.warning_box(
                            f"JSON input contained {len(raw)} entries but none were "
                            "objects with the expected fields (file_path, severity, "
                            "description); returning empty findings",
                            stream=sys.stderr,
                        ),
                        file=sys.stderr,
                    )
            return findings, None
    # Markdown fallback.
    return list(parse_findings_from_text(text)), None


def cmd_baseline(args: argparse.Namespace) -> int:
    """`advisor baseline create|diff` — snapshot and compare findings."""
    from .baseline import (
        diff_against_baseline,
        findings_to_entries,
        read_baseline,
        write_baseline,
    )
    from .verify import Finding

    action = args.action
    target = Path(args.target)
    if action == "create":
        output = Path(getattr(args, "output", None) or (target / ".advisor" / "baseline.jsonl"))
        from_file = getattr(args, "from_file", None)
        findings, rc = _load_findings_from_input(from_file)
        if rc is not None:
            return rc
        typed_findings: list[Finding] = [f for f in findings if isinstance(f, Finding)]
        # `_load_findings_from_input` returns ([], None) on a TTY stdin so the
        # shared helper's empty-result is benign for `diff`/`audit`/`sarif`,
        # but `create` writes a baseline that callers will diff against — a
        # silent zero-finding baseline would mask every finding next run.
        if not typed_findings and from_file is None and sys.stdin.isatty():
            print(
                _style.error_box(
                    "baseline create: no findings on stdin and no --from FILE; "
                    "refusing to overwrite baseline with zero findings",
                    stream=sys.stderr,
                ),
                file=sys.stderr,
            )
            return 2
        entries = findings_to_entries(typed_findings)
        try:
            write_baseline(output, entries)
        except OSError as exc:
            print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
            return 2
        if not getattr(args, "quiet", False):
            finding_word = "finding" if len(entries) == 1 else "findings"
            print(_style.success_box(f"baseline saved: {output} ({len(entries)} {finding_word})"))
        return 0
    if action == "diff":
        explicit_baseline = getattr(args, "baseline_path", None)
        baseline_path = Path(explicit_baseline or (target / ".advisor" / "baseline.jsonl"))
        if explicit_baseline and not baseline_path.exists():
            print(
                _style.error_box(
                    f"--baseline path not found: {baseline_path}",
                    stream=sys.stderr,
                ),
                file=sys.stderr,
            )
            return 2
        baseline = read_baseline(baseline_path)
        findings, rc = _load_findings_from_input(getattr(args, "from_file", None))
        if rc is not None:
            return rc
        typed_findings = [f for f in findings if isinstance(f, Finding)]
        diff = diff_against_baseline(typed_findings, baseline)
        if getattr(args, "json", False):
            payload = {
                "schema_version": JSON_SCHEMA_VERSION,
                "new": [
                    {
                        "file_path": f.file_path,
                        "severity": f.severity,
                        "description": f.description,
                    }
                    for f in diff.new
                ],
                "persisting_count": len(diff.persisting),
                "fixed": [
                    {
                        "file_path": e.file_path,
                        "rule_id": e.rule_id,
                        "description": e.description,
                    }
                    for e in diff.fixed
                ],
            }
            print(json.dumps(payload, indent=2))
            return 0
        lines = [
            "## Baseline diff",
            "",
            f"New findings: **{len(diff.new)}**",
            f"Persisting: {len(diff.persisting)}",
            f"Fixed (in baseline, not seen): {len(diff.fixed)}",
            "",
        ]
        if diff.new:
            lines.append("### New")
            for f in diff.new:
                lines.append(f"- [{f.severity}] `{f.file_path}` — {f.description}")
        print(_style.colorize_markdown("\n".join(lines).rstrip() + "\n"))
        return 0
    print(_style.error_box(f"unknown action: {action}", stream=sys.stderr), file=sys.stderr)
    return 2


def cmd_suppressions(args: argparse.Namespace) -> int:
    """`advisor suppressions` — list or inspect expired entries."""
    from .suppressions import load_suppressions

    target = Path(args.target)
    path = target / ".advisor" / "suppressions.jsonl"
    if not path.exists():
        print(_style.dim(f"no suppressions file at {path}"))
        return 0
    try:
        entries = load_suppressions(path)
    except ValueError as exc:
        print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
        return 2

    show_expired_only = getattr(args, "expired", False)
    if show_expired_only:
        entries = tuple(e for e in entries if e.expired)

    if getattr(args, "json", False):
        payload = {
            "schema_version": JSON_SCHEMA_VERSION,
            "count": len(entries),
            "entries": [
                {
                    "rule_id": e.rule_id,
                    "file": e.file,
                    "file_glob": e.file_glob,
                    "reason": e.reason,
                    "until": e.until,
                    "expired": e.expired,
                }
                for e in entries
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0
    if not entries:
        label = "expired suppressions" if show_expired_only else "suppressions"
        print(_style.dim(f"no {label} in {path}"))
        return 0
    lines = [f"## Suppressions ({len(entries)})", ""]
    for e in entries:
        scope = e.file or f"glob:{e.file_glob}"
        stamp = f" until {e.until}" if e.until else ""
        mark = " (expired)" if e.expired else ""
        lines.append(f"- `{e.rule_id}` → `{scope}`{stamp}{mark}")
        if e.reason:
            lines.append(f"  - _{e.reason}_")
    print(_style.colorize_markdown("\n".join(lines) + "\n"))
    return 0


def cmd_presets(args: argparse.Namespace) -> int:
    """List available rule-pack presets (pretty or JSON)."""
    from .presets import list_presets

    presets = list_presets()
    if getattr(args, "json", False):
        payload = {
            "schema_version": JSON_SCHEMA_VERSION,
            "count": len(presets),
            "presets": [
                {
                    "name": p.name,
                    "description": p.description,
                    "file_types": p.file_types,
                    "min_priority": p.min_priority,
                    "test_command": p.test_command,
                    "notes": list(p.notes),
                    "extra_keywords_by_tier": {
                        str(k): list(v) for k, v in p.extra_keywords_by_tier.items()
                    },
                }
                for p in presets
            ],
        }
        print(json.dumps(payload, indent=2))
        return 0
    lines = [f"## Presets ({len(presets)})", ""]
    for p in presets:
        lines.append(f"- **`{p.name}`** — {p.description}")
        lines.append(
            f"  - defaults: `file-types={p.file_types}`, "
            f"`min-priority={p.min_priority}`, "
            f"`test-cmd={p.test_command or '(none)'}`"
        )
        if p.extra_keywords_by_tier:
            tiers = ", ".join(
                f"P{k}:{len(v)}" for k, v in sorted(p.extra_keywords_by_tier.items(), reverse=True)
            )
            lines.append(f"  - extra keywords: {tiers}")
        for note in p.notes:
            lines.append(f"  - _{note}_")
        lines.append("")
    print(_style.colorize_markdown("\n".join(lines).rstrip() + "\n"))
    if not getattr(args, "quiet", False):
        print(_style.cta("use", "advisor plan . --preset <name>"))
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Analyze a transcript against a checkpoint and print an audit report.

    The transcript is opaque text read from ``--transcript FILE`` or stdin
    when ``--transcript -`` (the default). Binary transcripts are decoded
    with ``errors='replace'`` so pasted logs containing unprintable bytes
    don't kill the audit.
    """
    target = Path(args.target)
    try:
        cp = load_checkpoint(target, args.run_id)
    except (FileNotFoundError, ValueError) as exc:
        print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
        return 2

    transcript_arg = args.transcript or "-"
    if transcript_arg == "-":
        if sys.stdin.isatty():
            print(
                _style.warning_box(
                    "audit: no transcript on stdin; pipe the Claude Code "
                    f"conversation in, e.g. `pbpaste | advisor audit {args.run_id} .`",
                    stream=sys.stderr,
                ),
                file=sys.stderr,
            )
            return 2
        # Cap stdin transcripts at 50 MiB — a piped Claude Code conversation
        # tops out near a few MB, so anything larger is almost certainly a
        # mis-pipe (e.g. an entire log directory) and would otherwise be
        # buffered into RAM in one shot.
        _STDIN_LIMIT = 50 * 1024 * 1024
        transcript = sys.stdin.read(_STDIN_LIMIT + 1)
        if len(transcript) > _STDIN_LIMIT:
            print(
                _style.error_box(
                    f"audit: transcript exceeds {_STDIN_LIMIT // (1024 * 1024)} MiB cap",
                    stream=sys.stderr,
                ),
                file=sys.stderr,
            )
            return 2
    else:
        try:
            transcript = Path(transcript_arg).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
            return 2

    report = audit_transcript(transcript, cp)

    # Baseline / suppression filtering — applied to the in-batch findings.
    baseline_path = getattr(args, "baseline_path", None)
    if baseline_path:
        from .baseline import filter_against_baseline, read_baseline

        if not Path(baseline_path).exists():
            print(
                _style.error_box(
                    f"--baseline path not found: {baseline_path}",
                    stream=sys.stderr,
                ),
                file=sys.stderr,
            )
            return 2
        baseline = read_baseline(Path(baseline_path))
        kept, _ = filter_against_baseline(list(report.findings_in_batch), baseline)
        report = _replace_findings(report, kept)

    # Always consult .advisor/suppressions.jsonl if present.
    suppr_path = target / ".advisor" / "suppressions.jsonl"
    if suppr_path.exists():
        from .suppressions import apply_suppressions, load_suppressions

        try:
            entries = load_suppressions(suppr_path)
        except ValueError as exc:
            print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
            return 2
        kept, dropped = apply_suppressions(list(report.findings_in_batch), entries)
        if dropped:
            for f, s in dropped:
                _log_info(
                    f"suppressed {f.severity} {f.file_path!r} — "
                    f"rule {s.rule_id!r} ({s.reason or 'no reason'})"
                )
        report = _replace_findings(report, kept)

    fmt = getattr(args, "format", None)
    if fmt == "pr-comment":
        from .pr_comment import format_pr_comment

        print(format_pr_comment(list(report.findings_in_batch)))
        return _fail_on_findings(getattr(args, "fail_on", None), report.findings_in_batch) or 0

    sarif_path = getattr(args, "sarif", None)
    if sarif_path is not None:
        # Emit the in-batch findings for Code Scanning — out-of-batch findings
        # are drift, not results, and shouldn't be uploaded as scan hits.
        rc = _write_sarif(Path(sarif_path), list(report.findings_in_batch), target)
        if rc is not None:
            return rc

    fail_on_rc = _fail_on_findings(getattr(args, "fail_on", None), report.findings_in_batch)

    if getattr(args, "json", False):
        payload: dict[str, object] = {
            "schema_version": JSON_SCHEMA_VERSION,
            **audit_to_dict(report),
        }
        print(json.dumps(payload, indent=2))
        return fail_on_rc if fail_on_rc is not None else 0

    print(_style.colorize_markdown(format_audit_report(report)))
    if not getattr(args, "quiet", False) and (
        report.cap_overruns or report.protocol_violations or report.findings_out_of_batch
    ):
        print()
        print(
            _style.tip(
                "tighten caps with: advisor plan --max-fixes-per-runner N --large-file-max-fixes M"
            )
        )
    return fail_on_rc if fail_on_rc is not None else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="advisor",
        description="Advisor agent-team pipeline helpers for Claude Code.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_get_version()}",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        help="Disable ANSI color output (also honored via NO_COLOR env var)",
    )
    # Optional shell-completion (shtab) — ships as an extras dep so the
    # core tool stays dependency-free. When shtab is installed, users can
    # generate bash/zsh completions with `advisor --print-completion bash`.
    try:
        import shtab

        shtab.add_argument_to(parser, "--print-completion")
    except ImportError:
        parser.add_argument(
            "--print-completion",
            choices=["bash", "zsh", "tcsh"],
            default=None,
            help="Print a shell completion script (requires the `shtab` extra)",
        )
    sub = parser.add_subparsers(dest="command", required=False)

    p_pipeline = sub.add_parser("pipeline", help="Print the full pipeline reference")
    _add_common(p_pipeline)
    p_pipeline.add_argument("--json", action="store_true", help="Emit pipeline as JSON")
    p_pipeline.add_argument("--quiet", action="store_true", help="Suppress decorations (CTA/tips)")
    p_pipeline.set_defaults(func=cmd_pipeline)

    p_plan = sub.add_parser("plan", help="Rank files locally and print a dispatch plan")
    _add_common(p_plan)

    def _nonneg_int(value: str) -> int:
        n = int(value)
        if n < 0:
            raise argparse.ArgumentTypeError(f"batch-size must be >= 0, got {n}")
        return n

    p_plan.add_argument(
        "--batch-size",
        type=_nonneg_int,
        default=0,
        help="Group tasks into batches of this size (0 = flat dispatch plan, try 5 to start)",
    )
    p_plan.add_argument(
        "--json",
        action="store_true",
        help="Emit the ranked plan as JSON for scripting (no colors, no CTA)",
    )
    p_plan.add_argument(
        "--output",
        default="",
        metavar="FILE",
        help=(
            "Write the JSON plan to FILE instead of stdout "
            "(requires --json; ignored with a warning otherwise)"
        ),
    )
    p_plan.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress informational lines (errors still go to stderr)",
    )
    p_plan.add_argument(
        "--estimate",
        action="store_true",
        help="Include a token/cost estimate for the planned run",
    )
    # Test orchestration — advisor will re-dispatch fix failures to the
    # producing runner when this is set.
    p_plan.add_argument(
        "--test-cmd",
        default="",
        metavar="CMD",
        help='Shell command to run after each fix wave (e.g. "pytest -q")',
    )
    # Git-incremental scope — mutually exclusive (enforced in the cmd
    # function; argparse mutex groups interact poorly with nargs="?").
    p_plan.add_argument(
        "--since",
        default=None,
        metavar="REF",
        help="Scope to files changed since git REF (e.g. HEAD~5, main)",
    )
    p_plan.add_argument(
        "--staged",
        action="store_true",
        help="Scope to files currently staged for commit",
    )
    p_plan.add_argument(
        "--branch",
        default=None,
        metavar="BASE",
        help="Scope to files changed vs BASE ref (PR-style: BASE...HEAD)",
    )
    # Checkpoint + resume — for expensive runs that may be interrupted.
    p_plan.add_argument(
        "--checkpoint",
        action="store_true",
        help="Save the plan to .advisor/run-<id>.json for later --resume",
    )
    p_plan.add_argument(
        "--resume",
        default=None,
        metavar="RUN_ID",
        help="Resume a previously-saved checkpoint (skips discovery)",
    )
    # Ad-hoc exclusion — complements ``.advisorignore`` for one-off runs
    # without mutating the user's repo. Each ``--exclude`` is appended to
    # the ignore list used by ``rank_files``. Accepts glob patterns with
    # ``**`` (e.g. ``--exclude 'tests/**' --exclude 'docs/**'``).
    p_plan.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="Exclude paths matching PATTERN (repeatable; supports ** globs)",
    )
    # Pricing override — organizations with bespoke contracts or
    # forward-looking estimates can supply a JSON file. See
    # ``advisor.cost.load_pricing`` for the accepted shapes.
    p_plan.add_argument(
        "--pricing",
        default=None,
        metavar="FILE",
        help="Load model pricing (cents per 1M tokens) from JSON FILE",
    )
    # SARIF output — for GitHub Code Scanning / other CI consumers. The
    # plan stage emits an empty-results document (no findings yet); the
    # real findings-bearing SARIF comes from ``advisor audit --sarif``.
    p_plan.add_argument(
        "--sarif",
        metavar="PATH",
        type=Path,
        default=None,
        help=(
            "Write SARIF 2.1.0 output to PATH. For Code Scanning, pair with `actions/upload-sarif`."
        ),
    )
    # History-informed ranking — repeat-offender boost. Disabled on
    # --no-history for deterministic CI plans.
    p_plan.add_argument(
        "--no-history",
        action="store_true",
        help=(
            "Ignore .advisor/history.jsonl when ranking. Use in CI for "
            "deterministic plans independent of previous run outcomes."
        ),
    )
    p_plan.set_defaults(func=cmd_plan)

    p_prompt = sub.add_parser("prompt", help="Print a step prompt for pasting into Claude Code")
    p_prompt.add_argument("step", choices=["advisor", "runner", "verify"])
    p_prompt.add_argument(
        "--runner-id",
        type=_pos_int_arg,
        default=1,
        help="Runner ID for the runner step (>=1)",
    )
    _add_common(p_prompt)
    p_prompt.add_argument(
        "--file-count",
        type=int,
        default=0,
        help="Actual file count for verify prompt (default: use --max-runners)",
    )
    p_prompt.add_argument(
        "--no-history",
        action="store_true",
        help="Skip loading .advisor/history.jsonl into the advisor prompt",
    )
    p_prompt.add_argument("--json", action="store_true", help='Emit prompt as JSON {"text": ...}')
    p_prompt.add_argument("--quiet", action="store_true", help="Suppress frame/CTA lines")
    p_prompt.set_defaults(func=cmd_prompt)

    p_install = sub.add_parser(
        "install",
        help=(
            "Install the /advisor skill and append the advisor nudge to "
            "~/.claude/CLAUDE.md (both idempotent)"
        ),
    )
    p_install.add_argument("--path", default="", help="Override target CLAUDE.md path")
    p_install.add_argument(
        "--skill-path",
        default="",
        help="Override target SKILL.md path (default ~/.claude/skills/advisor/SKILL.md)",
    )
    p_install.add_argument(
        "--skip-skill",
        action="store_true",
        help="Only install the CLAUDE.md nudge, not the /advisor slash command skill",
    )
    p_install.add_argument(
        "--strict",
        action="store_true",
        help=f"Exit {_STRICT_NOOP_EXIT} on no-op (unchanged) so scripts can distinguish",
    )
    p_install.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-component status lines (errors still go to stderr)",
    )
    p_install.add_argument(
        "--json",
        action="store_true",
        help="When used with --check, emit status as JSON",
    )
    p_install.add_argument(
        "--check",
        action="store_true",
        help=(
            f"Dry-run — print status and exit {_STRICT_NOOP_EXIT} if anything "
            "is missing or outdated. Writes nothing."
        ),
    )
    p_install.set_defaults(func=cmd_install)

    p_status = sub.add_parser(
        "status",
        help="Print a health summary of the advisor install (writes nothing)",
    )
    p_status.add_argument("--path", default="", help="Override target CLAUDE.md path")
    p_status.add_argument(
        "--skill-path",
        default="",
        help="Override target SKILL.md path",
    )
    p_status.add_argument(
        "--strict",
        action="store_true",
        help=f"Exit {_STRICT_NOOP_EXIT} if anything is missing or outdated",
    )
    p_status.add_argument(
        "--json",
        action="store_true",
        help="Emit status as JSON for scripting (no colors, no CTA)",
    )
    p_status.add_argument("--quiet", action="store_true", help="Suppress CTA/tip lines")
    p_status.set_defaults(func=cmd_status)

    p_doctor = sub.add_parser(
        "doctor",
        help=(
            "Extended diagnostic: install status + git/claude/python/env checks (writes nothing)"
        ),
    )
    p_doctor.add_argument("--path", default="", help="Override target CLAUDE.md path")
    p_doctor.add_argument(
        "--skill-path",
        default="",
        help="Override target SKILL.md path",
    )
    p_doctor.add_argument(
        "--strict",
        action="store_true",
        help=f"Exit {_STRICT_NOOP_EXIT} if any check has level=fail",
    )
    p_doctor.add_argument(
        "--json",
        action="store_true",
        help="Emit the doctor report as JSON for scripting (no colors)",
    )
    p_doctor.add_argument("--quiet", action="store_true", help="Suppress CTA/tip lines")
    p_doctor.set_defaults(func=cmd_doctor)

    p_uninstall = sub.add_parser(
        "uninstall",
        help="Remove the /advisor skill and the advisor nudge block from CLAUDE.md",
    )
    p_uninstall.add_argument("--path", default="", help="Override target CLAUDE.md path")
    p_uninstall.add_argument(
        "--skill-path",
        default="",
        help="Override target SKILL.md path",
    )
    p_uninstall.add_argument(
        "--skip-skill",
        action="store_true",
        help="Only remove the CLAUDE.md nudge, leave the /advisor skill in place",
    )
    p_uninstall.add_argument(
        "--strict",
        action="store_true",
        help=f"Exit {_STRICT_NOOP_EXIT} on no-op (absent) so scripts can distinguish",
    )
    p_uninstall.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-component status lines (errors still go to stderr)",
    )
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_protocol = sub.add_parser(
        "protocol",
        help="Print the strict team-lifecycle protocol (TeamCreate → shutdowns → TeamDelete)",
    )
    p_protocol.add_argument(
        "--json", action="store_true", help='Emit protocol as JSON {"text": ...}'
    )
    p_protocol.add_argument("--quiet", action="store_true", help="Suppress CTA line")
    p_protocol.set_defaults(func=cmd_protocol)

    p_ui = sub.add_parser(
        "ui",
        help=(
            "Launch the optional local web dashboard "
            "(findings / plan / config / cost). Writes nothing."
        ),
    )
    _add_common(p_ui)
    p_ui.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind (default: 127.0.0.1 — loopback only)",
    )
    p_ui.add_argument(
        "--port",
        type=_valid_port,
        default=8765,
        help="Port to bind (default: %(default)s)",
    )
    p_ui.add_argument(
        "--verbose",
        action="store_true",
        help="Log every HTTP request to stderr (off by default)",
    )
    p_ui.add_argument("--json", action="store_true", help="Print server URL as JSON and exit")
    p_ui.set_defaults(func=cmd_ui)

    p_history = sub.add_parser(
        "history",
        help="Show recent CONFIRMED findings logged under <target>/.advisor/history.jsonl",
    )
    p_history.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Target directory containing the .advisor/ tree (default: current directory)",
    )

    def _pos_int(value: str) -> int:
        n = int(value)
        if n < 1:
            raise argparse.ArgumentTypeError(f"--limit must be >= 1, got {n}")
        return n

    p_history.add_argument(
        "--limit",
        type=_pos_int,
        default=20,
        help="Maximum number of recent entries to show (default: %(default)s)",
    )
    p_history.add_argument(
        "--json",
        action="store_true",
        help="Emit entries as JSON for scripting",
    )
    p_history.add_argument("--quiet", action="store_true", help="Suppress CTA/tip lines")
    p_history.set_defaults(func=cmd_history)

    p_version = sub.add_parser(
        "version",
        help="Print version + environment details (like a lighter `doctor`)",
    )
    p_version.add_argument(
        "--json",
        action="store_true",
        help="Emit version info as JSON for scripting",
    )
    p_version.add_argument("--quiet", action="store_true", help="Suppress CTA line")
    p_version.set_defaults(func=cmd_version)

    p_checkpoints = sub.add_parser(
        "checkpoints",
        help="List or delete saved plan checkpoints under <target>/.advisor/",
    )
    p_checkpoints.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Target directory containing the .advisor/ tree (default: current directory)",
    )
    p_checkpoints.add_argument(
        "--rm",
        default=None,
        metavar="RUN_ID",
        help="Remove a single checkpoint by run_id (idempotent)",
    )
    p_checkpoints.add_argument(
        "--clear",
        action="store_true",
        help="Remove all checkpoints for the target",
    )
    p_checkpoints.add_argument(
        "--json",
        action="store_true",
        help="Emit the list as JSON for scripting",
    )
    p_checkpoints.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress informational lines (errors still go to stderr)",
    )
    p_checkpoints.set_defaults(func=cmd_checkpoints)

    # Baseline subcommand (create / diff).
    p_baseline = sub.add_parser(
        "baseline",
        help="Snapshot-and-compare: create a baseline or diff current findings vs. it",
    )
    p_baseline.add_argument("action", choices=("create", "diff"), help="create or diff")
    p_baseline.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Target directory (default: current directory)",
    )
    p_baseline.add_argument(
        "--from",
        dest="from_file",
        metavar="PATH",
        default=None,
        type=Path,
        help="Read findings from PATH (JSON or markdown). Default: stdin.",
    )
    p_baseline.add_argument(
        "--output",
        metavar="PATH",
        default=None,
        help="Baseline output path (default: <target>/.advisor/baseline.jsonl)",
    )
    p_baseline.add_argument(
        "--baseline",
        dest="baseline_path",
        metavar="PATH",
        default=None,
        help="Baseline input path for diff (default: <target>/.advisor/baseline.jsonl)",
    )
    p_baseline.add_argument("--json", action="store_true", help="Emit JSON for scripting")
    p_baseline.add_argument("--quiet", action="store_true", help="Suppress CTA/tip lines")
    p_baseline.set_defaults(func=cmd_baseline)

    # Suppressions subcommand — list active / expired suppressions.
    p_suppr = sub.add_parser(
        "suppressions",
        help="List active suppressions from <target>/.advisor/suppressions.jsonl",
    )
    p_suppr.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Target directory (default: current directory)",
    )
    p_suppr.add_argument("--list", action="store_true", help="List all entries (default)")
    p_suppr.add_argument("--expired", action="store_true", help="Only show expired entries")
    p_suppr.add_argument("--json", action="store_true", help="Emit JSON for scripting")
    p_suppr.set_defaults(func=cmd_suppressions)

    p_presets = sub.add_parser(
        "presets",
        help="List available rule-pack presets for `--preset NAME`",
    )
    p_presets.add_argument("--json", action="store_true", help="Emit preset catalog as JSON")
    p_presets.add_argument("--quiet", action="store_true", help="Suppress CTA line")
    p_presets.set_defaults(func=cmd_presets)

    p_audit = sub.add_parser(
        "audit",
        help=(
            "Post-hoc audit of an advisor run — loads a checkpoint and a "
            "transcript, reports fix counts, CONTEXT_PRESSURE pings, "
            "rotations, PROTOCOL_VIOLATION strings, and scope drift."
        ),
    )
    p_audit.add_argument(
        "run_id",
        help="Checkpoint run_id (see `advisor checkpoints`)",
    )
    p_audit.add_argument(
        "target",
        nargs="?",
        default=".",
        help="Target directory containing the .advisor/ tree (default: current directory)",
    )
    p_audit.add_argument(
        "--transcript",
        default="-",
        metavar="FILE",
        help=(
            "Transcript file to analyze (default: `-` reads from stdin). "
            "Pipe the Claude Code conversation log in via `<` or `|`."
        ),
    )
    p_audit.add_argument(
        "--json",
        action="store_true",
        help="Emit the audit report as JSON for scripting",
    )
    p_audit.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress CTA/tip lines",
    )
    p_audit.add_argument(
        "--sarif",
        metavar="PATH",
        type=Path,
        default=None,
        help=(
            "Write in-batch findings as SARIF 2.1.0 to PATH. For Code "
            "Scanning, pair with `actions/upload-sarif`."
        ),
    )
    p_audit.add_argument(
        "--fail-on",
        dest="fail_on",
        choices=_FAIL_ON_CHOICES,
        default="never",
        help=("Exit 4 if any in-batch finding meets/exceeds LEVEL. Default: never (back-compat)."),
    )
    p_audit.add_argument(
        "--format",
        choices=("pretty", "json", "pr-comment"),
        default=None,
        help=("Output format. `pr-comment` emits GitHub-flavored markdown suitable for a PR body."),
    )
    p_audit.add_argument(
        "--baseline",
        dest="baseline_path",
        metavar="PATH",
        default=None,
        help=(
            "Suppress findings matching this baseline JSONL file (see `advisor baseline create`)."
        ),
    )
    p_audit.set_defaults(func=cmd_audit)

    return parser


# `install` / `uninstall` manage the nudge + skill explicitly, so calling
# ``ensure_nudge()`` before them is redundant (and would produce a confusing
# setup-complete banner right before the user's actual install command runs).
# Every other subcommand — including dry-run / read-only ones like ``status``,
# ``plan``, ``doctor`` — triggers the first-run setup so the README claim
# "The first run wires up ~/.claude/CLAUDE.md automatically" actually holds.
# ``ensure_nudge`` is itself idempotent: on every run after the first it
# detects existing state and returns UNCHANGED without writing anything.
_NUDGE_SKIP_COMMANDS = {
    "install",
    "uninstall",
    "version",
    "ui",
    "presets",
    "suppressions",
    "baseline",
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "no_color", False):
        os.environ["NO_COLOR"] = "1"
        _style.reset_color_cache()
    if args.print_completion:
        try:
            import shtab
        except ImportError:
            print(
                _style.error_box(
                    "Shell completion requires the `shtab` extra.\n"
                    "Install with: pip install 'advisor-agent[completion]'",
                    stream=sys.stderr,
                ),
                file=sys.stderr,
            )
            return 1
        print(shtab.complete(parser, shell=args.print_completion))
        return 0
    if not args.command:
        parser.error("a subcommand is required (try `advisor --help`)")
    if args.command not in _NUDGE_SKIP_COMMANDS:
        ensure_nudge()
    try:
        rc = args.func(args)
        return int(rc) if rc is not None else 0
    except BrokenPipeError:
        # Downstream pipe closed (e.g. `| head`); exit quietly.
        try:
            sys.stdout.close()
        except OSError:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
