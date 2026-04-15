"""Orchestrator — Glasswing pipeline via Claude Code native agent teams.

No external API calls. Uses Claude Code's TeamCreate, Agent, and SendMessage
tools to coordinate a two-model team:

  - Sonnet (explorer) — fast codebase discovery
  - Opus   (advisor)  — strategic ranking, dispatch planning, verification
  - Sonnet (runner)   — focused single-file analysis, parallel

(Earlier drafts used Haiku for the explorer; it was dropped after empirical
testing showed Claude Code's built-in Explore subagent never actually honored
the Haiku model override. Sonnet runs exploration now — Opus still ranks and
verifies, Sonnet still runs per-file analysis in parallel.)

The Python functions here generate the prompts and configs. The actual
orchestration happens in Claude Code by following the CLAUDE.md protocol.
"""

from dataclasses import dataclass

from .rank import RankedFile
from .focus import FocusTask


# ── Team Config ──────────────────────────────────────────────────


@dataclass(frozen=True)
class TeamConfig:
    """Configuration for the Glasswing team."""
    team_name: str
    target_dir: str
    file_types: str
    max_runners: int
    min_priority: int
    context: str


def default_team_config(
    target_dir: str,
    team_name: str = "glasswing",
    file_types: str = "*.py",
    max_runners: int = 5,
    min_priority: int = 3,
    context: str = "",
) -> TeamConfig:
    """Create a default team configuration."""
    return TeamConfig(
        team_name=team_name,
        target_dir=target_dir,
        file_types=file_types,
        max_runners=max_runners,
        min_priority=min_priority,
        context=context,
    )


# ── Step 1: Explorer (Sonnet) ───────────────────────────────────


def build_explore_prompt(config: TeamConfig) -> str:
    """Sonnet explorer prompt — inventory only, no analysis."""
    return (
        f"Explore the codebase at `{config.target_dir}`.\n\n"
        f"1. Glob for `{config.file_types}` files "
        f"(skip __pycache__, .venv, node_modules, .git, dist, build).\n"
        f"2. For each file, read the first 50 lines to capture imports and signatures.\n"
        f"3. Return a list, one per line:\n"
        f"   `<path>` — <one-line summary of what the file does>\n\n"
        f"Be fast. Inventory only — no opinions, no analysis.\n\n"
        f"When done, send your complete output to the team lead via "
        f"SendMessage(to='team-lead')."
    )


def build_explore_agent(config: TeamConfig) -> dict:
    """Claude Code Agent call spec for the Sonnet explorer."""
    return {
        "description": "Explore codebase for file inventory",
        "name": "explorer",
        "subagent_type": "Explore",
        "model": "sonnet",
        "team_name": config.team_name,
        "prompt": build_explore_prompt(config),
    }


# ── Step 2: Advisor ranking (Opus) ──────────────────────────────


def build_rank_prompt(file_inventory: str, config: TeamConfig) -> str:
    """Opus advisor prompt — rank files, decide dispatch plan."""
    ctx = f"\n\n## Goal\n{config.context}" if config.context else ""
    return (
        "You are the strategic advisor for a Glasswing analysis pipeline.\n\n"
        "## Task\n"
        "Review this file inventory and rank each file by priority.\n\n"
        "## Priority Scale\n"
        "- P5: auth, tokens, sessions, passwords, secrets, credentials\n"
        "- P4: user input, uploads, forms, parsing, deserialization\n"
        "- P3: HTTP handlers, API routes, DB queries, shell/exec, middleware\n"
        "- P2: config, env vars, crypto, caching, error handling, logging\n"
        "- P1: utilities, constants, types, models, tests, fixtures\n\n"
        "## File Inventory\n"
        f"{file_inventory}\n"
        f"{ctx}\n\n"
        "## Output\n"
        "1. One line per file: `P<n> path — reason`\n"
        f"2. ## Dispatch Plan: top {config.max_runners} files (P{config.min_priority}+) "
        "to analyze, in priority order\n"
        "3. For each dispatched file, one sentence of guidance for the runner: "
        "what to look for specifically\n\n"
        "Be decisive. No hedging.\n\n"
        "When done, send your complete output to the team lead via "
        "SendMessage(to='team-lead')."
    )


def build_rank_agent(
    file_inventory: str,
    config: TeamConfig,
) -> dict:
    """Claude Code Agent call spec for the advisor ranking pass."""
    return {
        "description": "Rank files and plan dispatch",
        "name": "advisor",
        "subagent_type": "deep-reasoning",
        "model": "opus",
        "team_name": config.team_name,
        "prompt": build_rank_prompt(file_inventory, config),
    }


# ── Step 3: Runners (Sonnet — parallel) ─────────────────────────


def build_runner_prompt(task: FocusTask, advisor_guidance: str = "") -> str:
    """Sonnet runner prompt — focused on one file."""
    guidance = (
        f"\n\n## Advisor Guidance\n{advisor_guidance}"
        if advisor_guidance else ""
    )
    return (
        f"You are a focused analysis agent. Review ONLY this file:\n"
        f"  `{task.file_path}` (priority P{task.priority})\n\n"
        f"## Process\n"
        f"1. Read the entire file\n"
        f"2. Hypothesize potential issues (bugs, security, logic errors, edge cases)\n"
        f"3. Trace call paths and data flow to confirm or reject each hypothesis\n"
        f"4. For each confirmed issue, report:\n"
        f"   - **File**: path:line_number\n"
        f"   - **Severity**: CRITICAL / HIGH / MEDIUM / LOW\n"
        f"   - **Description**: what the issue is\n"
        f"   - **Evidence**: the code path or proof\n"
        f"   - **Fix**: suggested remediation\n"
        f"5. If no issues found, state that explicitly\n\n"
        f"Do NOT review other files. Stay focused on this one.\n\n"
        f"When done, send your complete output to the team lead via "
        f"SendMessage(to='team-lead')."
        f"{guidance}"
    )


def build_runner_agents(
    tasks: list[FocusTask],
    config: TeamConfig,
    advisor_guidance: dict[str, str] | None = None,
) -> list[dict]:
    """Claude Code Agent call specs for all runners (dispatch in parallel).

    Args:
        tasks: FocusTasks from create_focus_tasks().
        config: Team configuration.
        advisor_guidance: Optional dict of file_path -> guidance string
                         from the advisor's dispatch plan.

    Returns:
        List of Agent call specs, one per runner.
    """
    guidance = advisor_guidance or {}
    agents = []
    for i, task in enumerate(tasks, 1):
        agents.append({
            "description": f"Analyze {task.file_path}",
            "name": f"runner-{i}",
            "subagent_type": "code-review",
            "model": "sonnet",
            "team_name": config.team_name,
            "run_in_background": True,
            "prompt": build_runner_prompt(
                task,
                guidance.get(task.file_path, ""),
            ),
        })
    return agents


# ── Step 4: Verification (Opus via SendMessage) ─────────────────


def build_verify_prompt(
    all_findings: str,
    file_count: int,
    runner_count: int,
) -> str:
    """Opus verification prompt — confirm or reject findings."""
    return (
        f"You dispatched {runner_count} analysis agents across {file_count} files. "
        f"Below are their combined findings.\n\n"
        f"## All Findings\n\n"
        f"{all_findings}\n\n"
        f"## Verification Instructions\n\n"
        f"For each finding:\n"
        f"1. Read the cited file and line to verify the issue exists\n"
        f"2. Check if it's exploitable or impactful in practice\n"
        f"3. Mark as **CONFIRMED** or **REJECTED** with a one-line reason\n\n"
        f"Reject:\n"
        f"- False positives (code is actually safe)\n"
        f"- Theoretical only (unrealistic conditions)\n"
        f"- Duplicates of another finding\n"
        f"- Trivial nits not worth fixing\n\n"
        f"## Required Output\n"
        f"1. Each finding: CONFIRMED/REJECTED + reason\n"
        f"2. ## Summary: X confirmed, Y rejected out of {runner_count} agents\n"
        f"3. ## Top 3 Actions: most critical fixes, in priority order\n\n"
        f"Be strict. Only confirm issues worth acting on.\n\n"
        f"When done, send your complete output to the team lead via "
        f"SendMessage(to='team-lead')."
    )


def build_verify_message(
    all_findings: str,
    file_count: int,
    runner_count: int,
) -> dict:
    """Claude Code SendMessage spec to resume the advisor for verification."""
    return {
        "to": "advisor",
        "message": build_verify_prompt(all_findings, file_count, runner_count),
    }


# ── Full Pipeline Reference ─────────────────────────────────────


def render_pipeline(config: TeamConfig) -> str:
    """Render the full pipeline as Claude Code tool calls for reference."""
    return f"""## Glasswing Pipeline — {config.team_name}
Target: {config.target_dir} ({config.file_types})
Max runners: {config.max_runners} | Min priority: P{config.min_priority}

### Step 1: Create team + Explore (Sonnet)
TeamCreate(name="{config.team_name}")
Agent(name="explorer", model="sonnet", subagent_type="Explore", team_name="{config.team_name}")

### Step 2: Rank (Opus)
Agent(name="advisor", model="opus", subagent_type="deep-reasoning", team_name="{config.team_name}")
→ Advisor outputs priority ranking + dispatch plan with per-file guidance

### Step 3: Analyze (Sonnet × N — parallel, background)
Agent(name="runner-1", model="sonnet", subagent_type="code-review", team_name="{config.team_name}", run_in_background=true)
Agent(name="runner-2", ...)
...up to {config.max_runners} runners for files P{config.min_priority}+

### Step 4: Verify (Opus — reuse advisor via SendMessage)
SendMessage(to="advisor", message=<all runner findings>)
→ Advisor confirms/rejects each finding, outputs Top 3 Actions
"""
