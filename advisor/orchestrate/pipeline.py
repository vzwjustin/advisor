"""Human-readable pipeline reference rendering."""

from __future__ import annotations

from .config import TeamConfig


def render_pipeline(config: TeamConfig) -> str:
    """Render the full pipeline as Claude Code tool calls for reference."""
    return f"""## Advisor Review Pipeline — {config.team_name}
Target: {config.target_dir} ({config.file_types})
Models: advisor={config.advisor_model}, runners={config.runner_model}
Suggested runners: ~{config.max_runners} | Min priority: P{config.min_priority}

> **TL;DR** — Spawn the advisor first; it sizes the runner pool from
> its own Glob+Grep pass. Spawn that many runners. Advisor dispatches
> explore (and optionally fix) batches over a live SendMessage dialogue,
> verifying each output as it lands. End by shutting down each teammate
> individually, then `TeamDelete()`.

### Step 1: Create team
TeamCreate(name="{config.team_name}")

### Step 2: Spawn advisor FIRST (no runners yet)
Agent(
  name="advisor",
  description="Investigate, rank, and dispatch runners",
  model="{config.advisor_model}",
  subagent_type="deep-reasoning",
  team_name="{config.team_name}",
  prompt=<build_advisor_prompt(config)>
)
→ Advisor does Glob+Grep structural discovery itself, ranks P1–P5,
  decides pool size, and produces a dispatch plan with dynamic batch sizing.

### Step 3: Spawn right-sized runner pool (when advisor tells you to)
Agent(
  name="runner-N",
  description="Pool runner N — waits for advisor dispatch",
  model="{config.runner_model}",
  subagent_type="code-review",
  team_name="{config.team_name}",
  run_in_background=true,
  prompt=<build_runner_pool_prompt(N, config)>
)

Runners are long-lived — reused across assignments for context accumulation.
Live two-way dialogue with the advisor throughout. Runners work ONLY on
what the advisor hands them.

### Step 4: Run the explore → reason → fix loop
Advisor dispatches explore assignments, verifies each runner's output as it
lands (not in bulk), reasons over aggregated findings, optionally dispatches
fix assignments, then sends the final structured report to team-lead.

### Step 5: Final report
Advisor's final message to team-lead is a structured summary:
- Top-N actions (highest impact first)
- Findings list with status (CONFIRMED / REJECTED / FIXED)
- Test results (if a fix wave ran)
- Follow-ups

### Step 6: Shutdown + clean up
Shut down each teammate individually (broadcast `"*"` with structured
messages fails), then delete the team:

  SendMessage(to="advisor",  message={{ "type": "shutdown_request" }})
  SendMessage(to="runner-1", message={{ "type": "shutdown_request" }})
  ...
  TeamDelete()
"""
