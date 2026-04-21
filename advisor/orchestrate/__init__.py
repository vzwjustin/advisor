"""Orchestrator package — advisor review-and-fix pipeline via Claude Code.

Split into focused submodules for maintainability:

- :mod:`advisor.orchestrate.config` — :class:`TeamConfig`, :func:`default_team_config`
- :mod:`advisor.orchestrate.advisor_prompt` — advisor prompt + agent spec
- :mod:`advisor.orchestrate.runner_prompts` — runner prompts, pool
  agent specs, batch/dispatch/handoff message builders
- :mod:`advisor.orchestrate.verify_dispatch` — verification prompt + message
- :mod:`advisor.orchestrate.pipeline` — human-readable pipeline reference

All public symbols are re-exported here for backwards compatibility with
``from advisor.orchestrate import X``.

Deprecated APIs (``build_explore_*``, ``build_rank_*``) were removed in
v0.4.0. Use :func:`build_advisor_prompt` / :func:`build_advisor_agent`
instead — the advisor now handles discovery directly.
"""

from __future__ import annotations

from .advisor_prompt import build_advisor_agent, build_advisor_prompt
from .config import KNOWN_MODEL_SHORTCUTS, TeamConfig, default_team_config, is_known_model
from .pipeline import render_pipeline
from .runner_prompts import (
    build_fix_assignment_message,
    build_runner_agents,
    build_runner_batch_message,
    build_runner_dispatch_messages,
    build_runner_handoff_message,
    build_runner_pool_agents,
    build_runner_pool_prompt,
    build_runner_prompt,
    check_batch_fix_budget,
)
from .verify_dispatch import build_verify_dispatch_prompt, build_verify_message

__all__ = [
    # config
    "KNOWN_MODEL_SHORTCUTS",
    "TeamConfig",
    "default_team_config",
    "is_known_model",
    # advisor
    "build_advisor_agent",
    "build_advisor_prompt",
    # runners
    "build_fix_assignment_message",
    "build_runner_agents",
    "build_runner_batch_message",
    "build_runner_dispatch_messages",
    "build_runner_handoff_message",
    "build_runner_pool_agents",
    "build_runner_pool_prompt",
    "build_runner_prompt",
    "check_batch_fix_budget",
    # verify
    "build_verify_dispatch_prompt",
    "build_verify_message",
    # pipeline
    "render_pipeline",
]
