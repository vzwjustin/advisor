"""Advisor CLI — for `python -m advisor` and the `advisor` script entry point.

Thin wrapper over the existing builders. Prints prompts/plans to stdout so a
"vibe coder" can paste them into Claude Code without touching Python.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .focus import create_focus_tasks, format_dispatch_plan
from .install import (
    ensure_nudge,
    install as install_nudge,
    uninstall as uninstall_nudge,
)
from .orchestrate import (
    TeamConfig,
    build_explore_prompt,
    build_rank_prompt,
    build_runner_prompt,
    build_verify_prompt,
    default_team_config,
    render_pipeline,
)
from .rank import rank_files


def _config_from_args(args: argparse.Namespace) -> TeamConfig:
    return default_team_config(
        target_dir=args.target,
        team_name=args.team,
        file_types=args.file_types,
        max_runners=args.max_runners,
        min_priority=args.min_priority,
        context=args.context or "",
    )


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", help="Target directory to analyze")
    parser.add_argument("--team", default="glasswing", help="Team name")
    parser.add_argument("--file-types", default="*.py", help="Glob pattern")
    parser.add_argument("--max-runners", type=int, default=5)
    parser.add_argument("--min-priority", type=int, default=3)
    parser.add_argument("--context", default="", help="Extra goal context")


def cmd_pipeline(args: argparse.Namespace) -> int:
    """Print the full pipeline reference for the given target."""
    print(render_pipeline(_config_from_args(args)))
    return 0


def _read_head(path: str, limit: int = 4000) -> str:
    try:
        return Path(path).read_text(errors="ignore")[:limit]
    except OSError:
        return ""


def cmd_plan(args: argparse.Namespace) -> int:
    """Rank local files and print a dispatch plan — no agents spawned."""
    target = Path(args.target)
    if not target.exists():
        print(f"error: target not found: {target}", file=sys.stderr)
        return 2

    paths = [str(p) for p in target.rglob(args.file_types) if p.is_file()]
    ranked = rank_files(paths, read_fn=_read_head)
    tasks = create_focus_tasks(
        ranked,
        max_tasks=args.max_runners,
        min_priority=args.min_priority,
    )
    if not tasks:
        print(f"no files at priority P{args.min_priority}+ in {target}")
        return 0
    print(format_dispatch_plan(tasks))
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    """Print a specific step's prompt so it can be pasted into Claude Code."""
    config = _config_from_args(args)
    if args.step == "explore":
        print(build_explore_prompt(config))
    elif args.step == "rank":
        inventory = sys.stdin.read() if not sys.stdin.isatty() else "<paste inventory here>"
        print(build_rank_prompt(inventory, config))
    elif args.step == "verify":
        findings = sys.stdin.read() if not sys.stdin.isatty() else "<paste findings here>"
        print(build_verify_prompt(findings, file_count=args.max_runners, runner_count=args.max_runners))
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    """Append a sentinel-wrapped advisor nudge to ~/.claude/CLAUDE.md."""
    target = Path(args.path) if args.path else None
    result = install_nudge(path=target)
    print(f"{result.action}: {result.path}")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove the advisor nudge block from CLAUDE.md."""
    target = Path(args.path) if args.path else None
    result = uninstall_nudge(path=target)
    print(f"{result.action}: {result.path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="advisor",
        description="Glasswing agent-team pipeline helpers for Claude Code.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_pipeline = sub.add_parser("pipeline", help="Print the full pipeline reference")
    _add_common(p_pipeline)
    p_pipeline.set_defaults(func=cmd_pipeline)

    p_plan = sub.add_parser("plan", help="Rank files locally and print a dispatch plan")
    _add_common(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_prompt = sub.add_parser("prompt", help="Print a step prompt for pasting into Claude Code")
    p_prompt.add_argument("step", choices=["explore", "rank", "verify"])
    _add_common(p_prompt)
    p_prompt.set_defaults(func=cmd_prompt)

    p_install = sub.add_parser(
        "install",
        help="Append an advisor nudge block to ~/.claude/CLAUDE.md (idempotent)",
    )
    p_install.add_argument("--path", default="", help="Override target CLAUDE.md path")
    p_install.set_defaults(func=cmd_install)

    p_uninstall = sub.add_parser(
        "uninstall",
        help="Remove the advisor nudge block from CLAUDE.md",
    )
    p_uninstall.add_argument("--path", default="", help="Override target CLAUDE.md path")
    p_uninstall.set_defaults(func=cmd_uninstall)

    return parser


_NUDGE_SKIP_COMMANDS = {"install", "uninstall"}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in _NUDGE_SKIP_COMMANDS:
        ensure_nudge()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
