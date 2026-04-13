"""Advisor tool pattern — native Claude Code implementation.

This project uses Claude Code's Agent tool to replicate the Anthropic API
advisor pattern. See CLAUDE.md for the workflow protocol.

Glasswing-inspired techniques:
  rank  — Priority-rank files by vulnerability likelihood (technique #2)
  focus — One agent per file for diverse, parallel analysis (technique #1)
  verify — Verification pass to filter noise from findings (technique #3)
"""

from .rank import RankedFile, rank_files, rank_to_prompt
from .focus import FocusTask, create_focus_tasks, format_dispatch_plan
from .verify import (
    Finding,
    VerifiedResult,
    build_verify_prompt,
    parse_findings_from_text,
)
from .orchestrate import (
    TeamConfig,
    default_team_config,
    build_explore_agent,
    build_rank_agent,
    build_runner_agents,
    build_verify_message,
    render_pipeline,
)

__all__ = [
    # rank
    "RankedFile",
    "rank_files",
    "rank_to_prompt",
    # focus
    "FocusTask",
    "create_focus_tasks",
    "format_dispatch_plan",
    # verify
    "Finding",
    "VerifiedResult",
    "build_verify_prompt",
    "parse_findings_from_text",
    # orchestrate
    "TeamConfig",
    "default_team_config",
    "build_explore_agent",
    "build_rank_agent",
    "build_runner_agents",
    "build_verify_message",
    "render_pipeline",
]
