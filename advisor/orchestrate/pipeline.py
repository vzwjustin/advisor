"""Human-readable pipeline reference rendering."""

from __future__ import annotations

from .config import TeamConfig

# Extra line-terminating code points beyond ``\n`` / ``\r`` that any
# Unicode-aware renderer (GitHub Markdown, VS Code preview, ``less -R``)
# treats as line breaks. The prior shape escaped only ``\n`` / ``\r``,
# so a hostile or autocorrected ``team_name`` containing U+2028 would
# visually shatter the rendered ``TeamCreate(name="...")`` line. Escape
# each to its conventional Python literal form so the rendered snippet
# stays a single visible line AND the original character round-trips
# losslessly if a downstream consumer un-escapes (matches the
# semantic of the pre-existing ``\\n`` / ``\\r`` escaping).
_EXTRA_LINEBREAK_ESCAPES: tuple[tuple[str, str], ...] = (
    ("\x0b", "\\x0b"),  # VT
    ("\x0c", "\\x0c"),  # FF
    ("\x85", "\\x85"),  # NEL (C1 line terminator)
    (" ", "\\u2028"),  # LS
    (" ", "\\u2029"),  # PS
)


def _safe_str(value: str) -> str:
    """Escape a value rendered inside a double-quoted reference snippet.

    The output of :func:`render_pipeline` contains pseudo-code like
    ``TeamCreate(name="{config.team_name}")``. A team name containing a
    literal ``"`` would close the string early; any line-terminating
    code point would shatter the snippet across lines and corrupt the
    reference that users paste/copy. Backslash-escape backslash, quote,
    CR/LF, AND the five additional Unicode line-terminator code points
    so the rendered snippet stays a single syntactically valid line on
    every renderer.
    """
    value = value.replace("\\", "\\\\").replace('"', '\\"')
    value = value.replace("\n", "\\n").replace("\r", "\\r")
    for raw, escaped in _EXTRA_LINEBREAK_ESCAPES:
        value = value.replace(raw, escaped)
    return value


def _three_tier_pipeline(
    team: str,
    target: str,
    file_types: str,
    advisor_model: str,
    explorer_model: str,
    runner_model: str,
    config: TeamConfig,
) -> str:
    return f"""## Advisor Review Pipeline — {team}
Target: {target} ({file_types})
Models: advisor={advisor_model}, explorers={explorer_model}, coders={runner_model}
Suggested explorers: ~{config.max_explorers} | Suggested coders: ~{config.max_runners} | Min priority: P{config.min_priority}

> **TL;DR** — Spawn the advisor first; it sizes explorer and coder pools from
> its own Glob+Grep pass and authors per-agent prompts. Spawn Haiku explorers
> for the explore wave, then Sonnet coders for fixes. Loop:
> **Explorer discovers → Advisor reasons → Coder fixes.** Runners report to
> team-lead; team-lead relays each report verbatim to the advisor. End by
> shutting down each teammate individually, then `TeamDelete()`.

### Step 1: Reset and create team
TeamDelete()
TeamCreate(name="{team}")

### Step 2: Spawn advisor FIRST (no explorers or coders yet)
Agent(
  name="advisor",
  description="Investigate, rank, and dispatch explorers + coders",
  model="{advisor_model}",
  subagent_type="advisor-executor",
  team_name="{team}",
  prompt=<build_advisor_prompt(config)>
)
→ Advisor does Glob+Grep structural discovery itself, ranks P1–P5,
  decides explorer + coder pool sizes, and produces a dispatch plan.

### Step 3: Spawn explorer pool (Haiku, read-only)
Agent(
  name="explorer-N",
  description="Pool explorer N — read-only file exploration",
  model="{explorer_model}",
  subagent_type="explorer",
  team_name="{team}",
  run_in_background=true,
  prompt=<build_explorer_prompt(config, target_files, guidance)>
)

Use per-explorer prompts from the advisor's dispatch plan. Explorers are
read-only (Read, Glob, Grep). They send `Exploration_Report` blocks to
team-lead; team-lead relays each to the advisor verbatim.

### Step 4: Spawn coder pool (Sonnet, fix implementation)
Agent(
  name="runner-N",
  description="Pool coder N — fix implementation",
  model="{runner_model}",
  subagent_type="code-review",
  team_name="{team}",
  run_in_background=true,
  prompt=<verbatim text from Opus's per-coder prompt block, or build_coder_prompt>
)

Coders receive fix assignments with embedded exploration context via
`build_fix_assignment_message(exploration_context=...)`. ``build_coder_prompt``
/ ``build_runner_pool_prompt`` (alias) are fallbacks for spawning without an
advisor-authored prompt.

### Step 5: Run the explore → reason → fix loop
Explorers discover → Advisor synthesizes Exploration_Reports → Advisor
dispatches fixes with exploration context → Coders implement. Team-lead
relays every report verbatim. The advisor verifies each output as it lands.

### Step 6: Final report
Advisor's final message to team-lead is a structured summary:
- Top-N actions (highest impact first)
- Findings list with status (CONFIRMED / REJECTED / FIXED)
- Test results (if a fix wave ran)
- Follow-ups

### Step 7: Shutdown + clean up
Shut down each teammate individually (broadcast `"*"` with structured
messages fails), then delete the team:

  SendMessage({{"to": "advisor",    "message": {{"type": "shutdown_request"}}}})
  SendMessage({{"to": "explorer-1", "message": {{"type": "shutdown_request"}}}})
  SendMessage({{"to": "runner-1",   "message": {{"type": "shutdown_request"}}}})
  ...
  TeamDelete()
"""


def _legacy_pipeline(
    team: str,
    target: str,
    file_types: str,
    advisor_model: str,
    runner_model: str,
    config: TeamConfig,
) -> str:
    return f"""## Advisor Review Pipeline — {team}
Target: {target} ({file_types})
Models: advisor={advisor_model}, runners={runner_model}
Suggested runners: ~{config.max_runners} | Min priority: P{config.min_priority}

> **TL;DR** — Legacy two-tier mode (`max_explorers=0`). Spawn the advisor first;
> it sizes the runner pool from its own Glob+Grep pass and authors per-runner
> prompts. Spawn that many runners using Opus's per-runner prompts verbatim.
> Runners report to team-lead; team-lead relays each report verbatim to the
> advisor. End by shutting down each teammate individually, then `TeamDelete()`.

### Step 1: Reset and create team
TeamDelete()
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

Use Opus's per-runner prompts verbatim from its dispatch plan. Runners are
long-lived — reused across assignments for context accumulation.

### Step 4: Run the explore → reason → fix loop
Runners send reports to team-lead; team-lead relays each to the advisor
verbatim the moment it arrives. The advisor verifies each output as it
lands, reasons over aggregated findings, optionally dispatches fix
assignments, then sends the final structured report back to team-lead.

### Step 5: Final report
Advisor's final message to team-lead is a structured summary:
- Top-N actions (highest impact first)
- Findings list with status (CONFIRMED / REJECTED / FIXED)
- Test results (if a fix wave ran)
- Follow-ups

### Step 6: Shutdown + clean up
Shut down each teammate individually, then delete the team:

  SendMessage({{"to": "advisor",  "message": {{"type": "shutdown_request"}}}})
  SendMessage({{"to": "runner-1", "message": {{"type": "shutdown_request"}}}})
  ...
  TeamDelete()
"""


def render_pipeline(config: TeamConfig) -> str:
    """Render the full pipeline as Claude Code tool calls for reference."""
    team = _safe_str(config.team_name)
    target = _safe_str(config.target_dir)
    file_types = _safe_str(config.file_types)
    advisor_model = _safe_str(config.advisor_model)
    runner_model = _safe_str(config.runner_model)
    if config.max_explorers > 0:
        return _three_tier_pipeline(
            team,
            target,
            file_types,
            advisor_model,
            _safe_str(config.explorer_model),
            runner_model,
            config,
        )
    return _legacy_pipeline(team, target, file_types, advisor_model, runner_model, config)
