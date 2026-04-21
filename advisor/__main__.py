"""Advisor CLI — for `python -m advisor` and the `advisor` script entry point.

Thin wrapper over the existing builders. Prints prompts/plans to stdout so a
"vibe coder" can paste them into Claude Code without touching Python.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path

from . import _style
from ._fs import read_head as _read_head
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
from .history import HISTORY_SCHEMA_VERSION, format_history_block, load_recent, new_run_id
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
    default_team_config,
    render_pipeline,
)
from .rank import rank_files

# Top-level schema version for JSON outputs. Bump when the shape of any
# ``--json`` payload changes in a way that would break downstream parsers.
# Individual payload modules (history, checkpoint) carry their own
# schema_version fields for fine-grained evolution.
JSON_SCHEMA_VERSION = "1.0"


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


# (action_label, fancy_glyph, ascii_glyph, color)
_ACTION_DISPLAY: dict[str, tuple[str, str, str, str | None]] = {
    "installed": ("installed", "✓", "+", "green"),
    "updated": ("updated", "↻", "~", "cyan"),
    "unchanged": ("unchanged", "·", "-", "dim"),
    "removed": ("removed", "✗", "x", "yellow"),
    "absent": ("not found", "·", "-", "dim"),
    "skipped": ("skipped", "·", "-", "dim"),
}


def _fmt_action(component: str, action: str, path: object) -> str:
    label, fancy, plain, color = _ACTION_DISPLAY.get(action, (action, "?", "?", None))
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


def _config_from_args(args: argparse.Namespace) -> TeamConfig:
    # Allow piping a large scope description into any subcommand via
    # `--context -` (or the literal string "-"), matching POSIX stdin
    # conventions. Explicit flag required so callers that accidentally
    # pipe into the CLI don't silently swallow stdin as context.
    context = args.context or ""
    if context == "-":
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
        test_command=getattr(args, "test_cmd", "") or "",
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
        default=5,
        help="Advisory runner count. Opus may exceed this for large codebases.",
    )
    parser.add_argument(
        "--min-priority",
        type=int,
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


def cmd_pipeline(args: argparse.Namespace) -> int:
    """Print the full pipeline reference for the given target."""
    print(_style.colorize_markdown(render_pipeline(_config_from_args(args))))
    print()
    print(_style.cta(f"/advisor {args.target}", "run the live pipeline in Claude Code"))
    return 0


def _safe_rglob(target: Path, pattern: str) -> tuple[list[str] | None, str | None]:
    """Return (paths, error). `error` is non-None on a malformed glob pattern
    or a filesystem error (e.g. symlink loops, permission denied)."""
    try:
        return [str(p) for p in target.rglob(pattern) if p.is_file()], None
    except ValueError as exc:
        return None, f"invalid --file-types pattern {pattern!r}: {exc}"
    except OSError as exc:
        return None, f"filesystem error scanning {target}: {exc}"


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
    accepted = {".advisor", ".advisor/", ".advisor/*", "/.advisor", "/.advisor/"}
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

            files = [p for p in files if fnmatch.fnmatch(Path(p).name, pattern)]
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

    paths, glob_err = _resolve_plan_files(target, args)
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

    ranked = rank_files(paths or [], read_fn=_read_head)
    tasks = create_focus_tasks(
        ranked,
        max_tasks=None,  # no hard cap; advisor decides in the live pipeline
        min_priority=args.min_priority,
    )

    batches: list[FocusBatch] | None = None
    if args.batch_size and args.batch_size > 1:
        batches = create_focus_batches(tasks, files_per_batch=args.batch_size)

    # Optional persistence: ``--checkpoint`` writes the full plan to
    # ``.advisor/run-<id>.json`` so a later invocation can ``--resume``.
    saved_run_id: str | None = None
    if getattr(args, "checkpoint", False):
        cfg = _config_from_args(args)
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

    return _emit_plan(args, target, tasks, batches, run_id=saved_run_id)


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
        out.append(
            FocusBatch(
                batch_id=int(str(b["batch_id"])),
                tasks=batch_tasks,
                complexity=str(b["complexity"]),
            )
        )
    return out


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
                pricing=pricing_override,
            )
        payload = _plan_to_dict(target, tasks, batches, estimate=estimate, run_id=run_id)
        rendered = json.dumps(payload, indent=2)
        output_file = getattr(args, "output", None)
        if output_file:
            Path(output_file).write_text(rendered + "\n", encoding="utf-8")
            if not getattr(args, "quiet", False):
                print(_style.dim(f"wrote plan to {output_file}"))
        else:
            print(rendered)
        return 0

    if not tasks:
        hint = (
            "--since/--staged/--branch selection"
            if context == "" and _is_git_scoped(args)
            else None
        )
        if hint:
            print(_style.warning_box(f"No files matched the {hint}"))
        else:
            print(_style.warning_box(f"No files at priority P{args.min_priority}+ in {target}"))
            print(_style.tip("Try --min-priority 1 to include all files"))
            print(_style.tip("Or adjust --file-types to match your file extensions"))
        return 0

    if batches:
        print(_style.colorize_markdown(format_batch_plan(batches)))
    else:
        print(_style.colorize_markdown(format_dispatch_plan(tasks)))

    if getattr(args, "estimate", False):
        cfg = _cfg()
        est = estimate_cost(
            tasks,
            batches,
            advisor_model=cfg.advisor_model,
            runner_model=cfg.runner_model,
            max_fixes_per_runner=cfg.max_fixes_per_runner,
            pricing=pricing_override,
        )
        print()
        print(_style.colorize_markdown(format_estimate(est)))

    if run_id:
        print()
        print(_style.dim(f"checkpoint saved: run_id={run_id}"))

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
    # TTY-only framing: interactive users see a dim banner announcing what
    # they're looking at; piped output (curl, redirect, pbcopy) stays clean
    # so the prompt can be consumed programmatically.
    show_frame = sys.stdout.isatty()
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
        print(build_advisor_prompt(config, history_block=history_block))
    elif args.step == "runner":
        runner_id = getattr(args, "runner_id", 1) or 1
        if show_frame:
            print(_style.dim(f"# runner-{runner_id} prompt — paste into Claude Code"))
            print()
        print(build_runner_pool_prompt(runner_id, config))
    elif args.step == "verify":
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
        print(
            build_verify_dispatch_prompt(
                findings,
                file_count=args.file_count or args.max_runners,
                runner_count=args.max_runners,
            )
        )
    return 0


# Exit codes for install/uninstall:
#   0 — changed (installed / updated / removed)
#   0 — no-op under idempotent semantics (unchanged / absent) by default
#   3 — no-op when --strict is passed (lets automation distinguish)
_STRICT_NOOP_EXIT = 3
_NOOP_ACTIONS = frozenset({InstallAction.UNCHANGED.value, InstallAction.ABSENT.value})


def _component_line(c: ComponentStatus) -> str:
    if c.present and c.current:
        mark = _style.paint(_style.glyph("✓", "+"), "green")
        state = _style.paint("installed", "green")
    elif c.present and not c.current:
        mark = _style.paint(_style.glyph("↻", "~"), "yellow")
        state = _style.paint("outdated", "yellow")
    else:
        mark = _style.paint(_style.glyph("✗", "x"), "red")
        state = _style.paint("missing", "red")
    name_col = _style.paint(f"{c.name:<6}", "cyan", "bold")
    return f"  {mark} {name_col} {state:<10} {_style.dim(str(c.path))}"


def _format_status(s: Status, version: str) -> str:
    header = _style.banner(f"advisor {version}", width=40)
    lines = [header, "", _component_line(s.nudge), _component_line(s.skill)]
    if s.opt_out:
        warn = _style.paint(_style.glyph("⚠", "!"), "yellow")
        lines.append(f"  {warn} auto-install disabled ({OPT_OUT_ENV} set)")
    if not (s.nudge.present and s.skill.present):
        lines.append(_style.dim("  fix: advisor install"))
    elif not (s.nudge.current and s.skill.current):
        lines.append(_style.dim("  fix: advisor install   (refresh outdated bits)"))
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
   Agent(name="advisor", model="opus", subagent_type="deep-reasoning",
         team_name="review", prompt=<build_advisor_prompt(config)>)

3. Advisor does Glob+Grep discovery, ranks P1–P5, decides runner pool size,
   THEN tells you to spawn N runners:
   Agent(name="runner-<i>", model="sonnet", subagent_type="code-review",
         team_name="review", run_in_background=true,
         prompt=<build_runner_pool_prompt(i, config)>)

4. Advisor dispatches explore assignments, verifies each runner reply as it
   lands, optionally dispatches fix assignments, then sends the final
   structured report to team-lead.

5. Shut down teammates INDIVIDUALLY (broadcast "*" with structured messages
   fails silently):
     SendMessage(to="advisor",  message={"type": "shutdown_request"})
     SendMessage(to="runner-1", message={"type": "shutdown_request"})
     ...
     SendMessage(to="runner-N", message={"type": "shutdown_request"})

6. TeamDelete()

Names and models shown here are the defaults (team "review", models
"opus" / "sonnet"). Override them via `--team`, `--advisor-model`,
`--runner-model` on the CLI; `advisor pipeline <dir>` renders the
concrete call sites for a given config.
"""


def cmd_protocol(_args: argparse.Namespace) -> int:
    """Print the strict team-lifecycle protocol as an ad-hoc reference."""
    print(_PROTOCOL_TEXT)
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
            "schema_version": HISTORY_SCHEMA_VERSION,
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
        print(_style.dim(f"no history at {target}/.advisor/history.jsonl"))
        return 0
    print(_style.colorize_markdown(format_history_block(entries)))
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
    header = _style.banner(f"advisor {info['advisor_version']}", width=40)
    lines = [
        header,
        f"  python     {info['python_version']} ({info['python_implementation']})",
        f"  platform   {info['platform']}",
        f"  install    {info['install_path']}",
        f"  schema     {info['schema_version']}",
    ]
    print("\n".join(lines))
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
        path = checkpoint_path(target, rm_id)
        if path.exists():
            try:
                path.unlink()
            except OSError as exc:
                print(_style.error_box(str(exc), stream=sys.stderr), file=sys.stderr)
                return 2
            if not getattr(args, "quiet", False):
                print(_style.dim(f"removed checkpoint {rm_id}"))
        elif not getattr(args, "quiet", False):
            print(_style.dim(f"no checkpoint {rm_id} at {path}"))
        return 0

    if clear:
        ids = list_checkpoints(target)
        removed = 0
        for rid in ids:
            path = checkpoint_path(target, rid)
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        if not getattr(args, "quiet", False):
            print(_style.dim(f"removed {removed} checkpoint(s)"))
        return 0

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
        print(_style.dim(f"no checkpoints at {target}/.advisor/"))
        return 0
    print(_style.colorize_markdown(f"## Checkpoints ({len(ids)})"))
    for rid in ids:
        path = checkpoint_path(target, rid)
        print(f"- `{rid}` — {path}")
    print()
    print(_style.tip("resume with: advisor plan --resume <RUN_ID>"))
    return 0


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
        help="Write the (JSON) plan to FILE instead of stdout",
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
    p_plan.set_defaults(func=cmd_plan)

    p_prompt = sub.add_parser("prompt", help="Print a step prompt for pasting into Claude Code")
    p_prompt.add_argument("step", choices=["advisor", "runner", "verify"])
    p_prompt.add_argument("--runner-id", type=int, default=1, help="Runner ID for the runner step")
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
        type=int,
        default=8765,
        help="Port to bind (default: %(default)s)",
    )
    p_ui.add_argument(
        "--verbose",
        action="store_true",
        help="Log every HTTP request to stderr (off by default)",
    )
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
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
