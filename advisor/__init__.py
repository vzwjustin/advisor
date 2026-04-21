"""Advisor tool pattern — native Claude Code implementation.

This project uses Claude Code's Agent tool to replicate the Anthropic API
advisor pattern. See CLAUDE.md for the workflow protocol.

Core building blocks:
  rank        — Priority-rank files by likelihood of containing issues
  focus       — Batched file review for parallel analysis
  verify      — Verification pass to filter noise from findings
  orchestrate — Team config, prompt builders, dispatch message specs
"""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # ``advisor-agent`` is the distribution name on PyPI; ``advisor`` is the
    # import name. ``importlib.metadata`` is the PEP 566-aligned source of
    # truth — keeping ``__version__`` derived from it means we never drift
    # from the number declared in ``pyproject.toml``.
    __version__ = _pkg_version("advisor-agent")
except PackageNotFoundError:  # pragma: no cover — editable, not-installed fallback
    __version__ = "0+unknown"

from .checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    Checkpoint,
    checkpoint_path,
    list_checkpoints,
    load_checkpoint,
    save_checkpoint,
)
from .cost import CostEstimate, estimate_cost, format_estimate
from .doctor import DoctorReport, run_doctor
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
    HISTORY_SCHEMA_VERSION,
    HistoryEntry,
    append_entries,
    entry_now,
    format_history_block,
    history_path,
    load_recent,
    new_run_id,
)
from .install import (
    ComponentStatus,
    InstallAction,
    InstallResult,
    Status,
    apply_nudge,
    ensure_nudge,
    get_installed_skill_version,
    install,
    install_skill,
    parse_badge,
    remove_nudge,
    render_block,
    should_auto_nudge,
    status,
    uninstall,
    uninstall_skill,
)
from .orchestrate import (
    TeamConfig,
    build_advisor_agent,
    build_advisor_prompt,
    build_runner_agents,
    build_runner_batch_message,
    build_runner_dispatch_messages,
    build_runner_handoff_message,
    build_runner_pool_agents,
    build_runner_pool_prompt,
    build_runner_prompt,
    build_verify_dispatch_prompt,
    build_verify_message,
    default_team_config,
    is_known_model,
    render_pipeline,
)
from .rank import (
    CONTENT_SCAN_LIMIT,
    LANGUAGE_EXTRA_KEYWORDS,
    PRIORITY_KEYWORDS,
    RankedFile,
    language_for_path,
    load_advisorignore,
    rank_files,
    rank_to_prompt,
)
from .skill_asset import SKILL_MD
from .verify import (
    Finding,
    build_verify_prompt,
    format_findings_block,
    parse_findings_from_text,
)

__all__ = [
    # version
    "__version__",
    # rank
    "CONTENT_SCAN_LIMIT",
    "LANGUAGE_EXTRA_KEYWORDS",
    "PRIORITY_KEYWORDS",
    "RankedFile",
    "language_for_path",
    "load_advisorignore",
    "rank_files",
    "rank_to_prompt",
    # focus
    "FocusBatch",
    "FocusTask",
    "create_focus_batches",
    "create_focus_tasks",
    "format_batch_plan",
    "format_dispatch_plan",
    # verify
    "Finding",
    "build_verify_prompt",
    "format_findings_block",
    "parse_findings_from_text",
    # orchestrate
    "TeamConfig",
    "default_team_config",
    "is_known_model",
    "build_advisor_agent",
    "build_advisor_prompt",
    "build_runner_agents",
    "build_runner_batch_message",
    "build_runner_dispatch_messages",
    "build_runner_handoff_message",
    "build_runner_pool_agents",
    "build_runner_pool_prompt",
    "build_runner_prompt",
    "build_verify_dispatch_prompt",
    "build_verify_message",
    "render_pipeline",
    # install
    "ComponentStatus",
    "InstallAction",
    "InstallResult",
    "Status",
    "apply_nudge",
    "ensure_nudge",
    "get_installed_skill_version",
    "install",
    "install_skill",
    "parse_badge",
    "remove_nudge",
    "render_block",
    "should_auto_nudge",
    "status",
    "uninstall",
    "uninstall_skill",
    # git scope
    "GitScopeError",
    "resolve_git_scope",
    # cost
    "CostEstimate",
    "estimate_cost",
    "format_estimate",
    # doctor
    "DoctorReport",
    "run_doctor",
    # history
    "HISTORY_SCHEMA_VERSION",
    "HistoryEntry",
    "append_entries",
    "entry_now",
    "format_history_block",
    "history_path",
    "load_recent",
    "new_run_id",
    # checkpoint
    "CHECKPOINT_SCHEMA_VERSION",
    "Checkpoint",
    "checkpoint_path",
    "list_checkpoints",
    "load_checkpoint",
    "save_checkpoint",
    # skill asset
    "SKILL_MD",
]
