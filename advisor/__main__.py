"""Advisor CLI — for `python -m advisor` and the `advisor` script entry point.

Thin wrapper over the existing builders. Prints prompts/plans to stdout so a
"vibe coder" can paste them into Claude Code without touching Python.
"""

from __future__ import annotations

import argparse
import sys
from importlib.metadata import version as pkg_version
from pathlib import Path


def _get_version() -> str:
    try:
        return pkg_version("advisor-agent")
    except Exception:
        return "dev"

from . import _style
from .focus import (
    create_focus_batches,
    create_focus_tasks,
    format_batch_plan,
    format_dispatch_plan,
)
from .install import (
    ensure_nudge,
    install as install_nudge,
    install_skill,
    uninstall as uninstall_nudge,
    uninstall_skill,
)
from .orchestrate import (
    TeamConfig,
    build_advisor_prompt,
    build_rank_prompt,
    build_runner_pool_prompt,
    build_verify_dispatch_prompt,
    default_team_config,
    render_pipeline,
)
from .rank import rank_files


# (action_label, fancy_glyph, ascii_glyph, color)
_ACTION_DISPLAY: dict[str, tuple[str, str, str, str | None]] = {
    "installed": ("installed", "✓", "+", "green"),
    "updated":   ("updated",   "↻", "~", "cyan"),
    "unchanged": ("unchanged", "·", "-", "dim"),
    "removed":   ("removed",   "✗", "x", "yellow"),
    "absent":    ("not found", "·", "-", "dim"),
    "skipped":   ("skipped",   "·", "-", "dim"),
}


def _fmt_action(component: str, action: str, path: object) -> str:
    label, fancy, plain, color = _ACTION_DISPLAY.get(
        action, (action, "?", "?", None)
    )
    mark = _style.glyph(fancy, plain)
    if color:
        mark = _style.paint(mark, color)
    # Pad component (max len in current set: "skill" = 5 → use 6)
    return f"{mark} {component:<6} {label:<10} {path}"


def _config_from_args(args: argparse.Namespace) -> TeamConfig:
    return default_team_config(
        target_dir=args.target,
        team_name=args.team,
        file_types=args.file_types,
        max_runners=args.max_runners,
        min_priority=args.min_priority,
        context=args.context or "",
        advisor_model=args.advisor_model,
        runner_model=args.runner_model,
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", help="Target directory to analyze")
    parser.add_argument("--team", default="glasswing", help="Team name")
    parser.add_argument("--file-types", default="*.py", help="Glob pattern")
    parser.add_argument(
        "--max-runners",
        type=int,
        default=5,
        help="Advisory runner count. Opus may exceed this for large codebases.",
    )
    parser.add_argument("--min-priority", type=int, default=3, help="Minimum priority tier (1=utilities, 5=auth/secrets, default: 3)")
    parser.add_argument("--context", default="", help="Extra goal context")
    parser.add_argument(
        "--advisor-model",
        default="opus",
        help="Model for the advisor agent (default: opus)",
    )
    parser.add_argument(
        "--runner-model",
        default="sonnet",
        help="Model for the runner pool agents (default: sonnet)",
    )


def cmd_pipeline(args: argparse.Namespace) -> int:
    """Print the full pipeline reference for the given target."""
    print(render_pipeline(_config_from_args(args)))
    return 0


def _read_head(path: str, limit: int = 4000) -> str:
    try:
        return Path(path).read_text(errors="ignore")[:limit]
    except OSError:
        return ""


def _safe_rglob(target: Path, pattern: str) -> tuple[list[str] | None, str | None]:
    """Return (paths, error). `error` is non-None on a malformed glob pattern."""
    try:
        return [str(p) for p in target.rglob(pattern) if p.is_file()], None
    except ValueError as exc:
        return None, f"invalid --file-types pattern {pattern!r}: {exc}"


def cmd_plan(args: argparse.Namespace) -> int:
    """Rank local files and print a batch dispatch plan — no agents spawned."""
    target = Path(args.target)
    if not target.exists():
        print(f"{_style.err('error:')} target not found: {target}", file=sys.stderr)
        return 2

    paths, glob_err = _safe_rglob(target, args.file_types)
    if glob_err is not None:
        print(f"{_style.err('error:')} {glob_err}", file=sys.stderr)
        return 2

    ranked = rank_files(paths or [], read_fn=_read_head)
    tasks = create_focus_tasks(
        ranked,
        max_tasks=None,  # no hard cap; advisor decides in the live pipeline
        min_priority=args.min_priority,
    )
    if not tasks:
        print(f"No files at priority P{args.min_priority}+ in {target}.")
        print(_style.dim("  hint: try --min-priority 1 to include all files."))
        return 0

    if args.batch_size and args.batch_size > 1:
        batches = create_focus_batches(tasks, files_per_batch=args.batch_size)
        print(format_batch_plan(batches))
    else:
        print(format_dispatch_plan(tasks))
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    """Print a specific step's prompt so it can be pasted into Claude Code."""
    config = _config_from_args(args)
    if args.step == "advisor":
        print(build_advisor_prompt(config))
    elif args.step == "runner":
        runner_id = getattr(args, "runner_id", 1) or 1
        print(build_runner_pool_prompt(runner_id, config))
    elif args.step == "rank":
        inventory = sys.stdin.read() if not sys.stdin.isatty() else "<paste inventory here>"
        print(build_rank_prompt(inventory, config))
    elif args.step == "verify":
        findings = sys.stdin.read() if not sys.stdin.isatty() else "<paste findings here>"
        print(build_verify_dispatch_prompt(
            findings,
            file_count=args.file_count or args.max_runners,
            runner_count=args.max_runners,
        ))
    return 0


# Exit codes for install/uninstall:
#   0 — changed (installed / updated / removed)
#   0 — no-op under idempotent semantics (unchanged / absent) by default
#   3 — no-op when --strict is passed (lets automation distinguish)
_STRICT_NOOP_EXIT = 3
_NOOP_ACTIONS = frozenset({"unchanged", "absent"})


def cmd_install(args: argparse.Namespace) -> int:
    """Install the /advisor skill AND append the CLAUDE.md nudge."""
    nudge_target = Path(args.path) if args.path else None
    skill_target = Path(args.skill_path) if args.skill_path else None

    try:
        nudge_result = install_nudge(path=nudge_target)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"{_style.err('error')} (nudge): {exc}", file=sys.stderr)
        return 1
    print(_fmt_action("nudge", nudge_result.action, nudge_result.path))

    if args.skip_skill:
        skill_action = "skipped"
    else:
        try:
            skill_result = install_skill(path=skill_target)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"{_style.err('error')} (skill): {exc}", file=sys.stderr)
            return 1
        skill_action = skill_result.action
        print(_fmt_action("skill", skill_result.action, skill_result.path))

    if args.strict and (
        nudge_result.action in _NOOP_ACTIONS
        and skill_action in (*_NOOP_ACTIONS, "skipped")
    ):
        return _STRICT_NOOP_EXIT
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove the /advisor skill AND the CLAUDE.md nudge block."""
    nudge_target = Path(args.path) if args.path else None
    skill_target = Path(args.skill_path) if args.skill_path else None

    try:
        nudge_result = uninstall_nudge(path=nudge_target)
    except (OSError, UnicodeDecodeError) as exc:
        print(f"{_style.err('error')} (nudge): {exc}", file=sys.stderr)
        return 1
    print(_fmt_action("nudge", nudge_result.action, nudge_result.path))

    if args.skip_skill:
        skill_action = "skipped"
    else:
        try:
            skill_result = uninstall_skill(path=skill_target)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"{_style.err('error')} (skill): {exc}", file=sys.stderr)
            return 1
        skill_action = skill_result.action
        print(_fmt_action("skill", skill_result.action, skill_result.path))

    if args.strict and (
        nudge_result.action in _NOOP_ACTIONS
        and skill_action in (*_NOOP_ACTIONS, "skipped")
    ):
        return _STRICT_NOOP_EXIT
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="advisor",
        description="Glasswing agent-team pipeline helpers for Claude Code.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_get_version()}",
    )
    sub = parser.add_subparsers(dest="command", required=True)

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
        help="Group tasks into batches of this size (0 = flat dispatch plan)",
    )
    p_plan.set_defaults(func=cmd_plan)

    p_prompt = sub.add_parser("prompt", help="Print a step prompt for pasting into Claude Code")
    p_prompt.add_argument("step", choices=["advisor", "runner", "rank", "verify"])
    p_prompt.add_argument("--runner-id", type=int, default=1, help="Runner ID for the runner step")
    _add_common(p_prompt)
    p_prompt.add_argument(
        "--file-count",
        type=int,
        default=0,
        help="Actual file count for verify prompt (default: use --max-runners)",
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
    p_install.set_defaults(func=cmd_install)

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
    p_uninstall.set_defaults(func=cmd_uninstall)

    return parser


_NUDGE_SKIP_COMMANDS = {"install", "uninstall"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in _NUDGE_SKIP_COMMANDS:
        ensure_nudge()
    try:
        return args.func(args)
    except BrokenPipeError:
        # Downstream pipe closed (e.g. `| head`); exit quietly.
        try:
            sys.stdout.close()
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
