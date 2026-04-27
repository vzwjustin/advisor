"""Human-readable pipeline reference rendering."""

from __future__ import annotations

from .config import TeamConfig


def _safe_str(value: str) -> str:
    """Escape a value rendered inside a double-quoted reference snippet.

    The output of :func:`render_pipeline` contains pseudo-code like
    ``TeamCreate(name="{config.team_name}")``. A team name containing a
    literal ``"`` would close the string early and corrupt the reference
    that users paste/copy. Backslash-escape both quote and backslash so
    the rendered snippet stays syntactically valid.
    """
    return value.replace("\\", "\\\\").replace('"', '\\"')


def render_pipeline(config: TeamConfig) -> str:
    """Render the full pipeline as Claude Code tool calls for reference."""
    team = _safe_str(config.team_name)
    target = _safe_str(config.target_dir)
    file_types = _safe_str(config.file_types)
    advisor_model = _safe_str(config.advisor_model)
    runner_model = _safe_str(config.runner_model)
    return f"""## Advisor Review Pipeline — {team}
Target: {target} ({file_types})
Models: advisor={advisor_model}, runners={runner_model}
Suggested runners: ~{config.max_runners} | Min priority: P{config.min_priority}

> **TL;DR** — Spawn the advisor first; it sizes the runner pool from
> its own Glob+Grep pass and authors per-runner prompts. Spawn that many
> runners using Opus's per-runner prompts verbatim — never the generic
> ``build_runner_pool_prompt`` fallback. Runners report to team-lead;
> team-lead relays each report verbatim to the advisor. End by shutting
> down each teammate individually, then `TeamDelete()`.

### Step 1: Create team
TeamCreate(name="{team}")

### Step 2: Spawn advisor FIRST (no runners yet)
Agent(
  name="advisor",
  description="Investigate, rank, and dispatch runners",
  model="{advisor_model}",
  subagent_type="advisor-executor",
  team_name="{team}",
  prompt=<build_advisor_prompt(config)>
)
→ Advisor does Glob+Grep structural discovery itself, ranks P1–P5,
  decides pool size, and produces a dispatch plan with a per-runner
  prompt for every runner.

### Step 3: Spawn right-sized runner pool with Opus's per-runner prompts
Agent(
  name="runner-N",
  description="Pool runner N — reads batch from initial prompt",
  model="{runner_model}",
  subagent_type="code-review",
  team_name="{team}",
  run_in_background=true,
  prompt=<verbatim text from Opus's "### runner-N / #### Prompt" block>
)

Use Opus's per-runner prompts verbatim from its dispatch plan — each is
tailored to the files in that runner's batch. ``build_runner_pool_prompt``
in the Python API is a *fallback* for spawning runners without an
advisor; the live pipeline never uses it. Runners are long-lived — reused
across assignments for context accumulation. Live two-way dialogue with
the advisor (via team-lead relay) throughout. Runners work ONLY on what
the advisor hands them.

### Step 4: Run the explore → reason → fix loop
Runners send reports to team-lead; team-lead relays each to the advisor
verbatim the moment it arrives. The advisor verifies each output as it
lands (not in bulk), reasons over aggregated findings, optionally
dispatches fix assignments, then sends the final structured report
back to team-lead.

### Step 5: Final report
Advisor's final message to team-lead is a structured summary:
- Top-N actions (highest impact first)
- Findings list with status (CONFIRMED / REJECTED / FIXED)
- Test results (if a fix wave ran)
- Follow-ups

### Step 6: Shutdown + clean up
Shut down each teammate individually (broadcast `"*"` with structured
messages fails), then delete the team:

  SendMessage({{"to": "advisor",  "message": {{"type": "shutdown_request"}}}})
  SendMessage({{"to": "runner-1", "message": {{"type": "shutdown_request"}}}})
  ...
  TeamDelete()
"""
