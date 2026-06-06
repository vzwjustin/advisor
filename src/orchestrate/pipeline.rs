//! Port of `advisor/orchestrate/pipeline.py` — human-readable pipeline reference.

use crate::config::TeamConfig;

const EXTRA_LINEBREAK_ESCAPES: &[(&str, &str)] = &[
    ("\x0b", "\\x0b"),
    ("\x0c", "\\x0c"),
    ("\u{0085}", "\\x85"),
    ("\u{2028}", "\\u2028"),
    ("\u{2029}", "\\u2029"),
];

fn safe_str(value: &str) -> String {
    let mut out = value.replace('\\', "\\\\").replace('"', "\\\"");
    out = out.replace('\n', "\\n").replace('\r', "\\r");
    for (raw, escaped) in EXTRA_LINEBREAK_ESCAPES {
        out = out.replace(raw, escaped);
    }
    out
}

fn three_tier_pipeline(
    team: &str,
    target: &str,
    file_types: &str,
    advisor_model: &str,
    explorer_model: &str,
    runner_model: &str,
    config: &TeamConfig,
) -> String {
    format!(
        r#"## Advisor Review Pipeline — {team}
Target: {target} ({file_types})
Models: advisor={advisor_model}, explorers={explorer_model}, coders={runner_model}
Suggested explorers: ~{max_explorers} | Suggested coders: ~{max_runners} | Min priority: P{min_priority}

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
"#,
        max_explorers = config.max_explorers,
        max_runners = config.max_runners,
        min_priority = config.min_priority,
    )
}

fn legacy_pipeline(
    team: &str,
    target: &str,
    file_types: &str,
    advisor_model: &str,
    runner_model: &str,
    config: &TeamConfig,
) -> String {
    format!(
        r#"## Advisor Review Pipeline — {team}
Target: {target} ({file_types})
Models: advisor={advisor_model}, runners={runner_model}
Suggested runners: ~{max_runners} | Min priority: P{min_priority}

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
  prompt=<verbatim text from Opus dispatch plan per runner>
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
"#,
        max_runners = config.max_runners,
        min_priority = config.min_priority,
    )
}

/// Render the full pipeline as Claude Code tool calls for reference.
pub fn render_pipeline(config: &TeamConfig) -> String {
    let team = safe_str(&config.team_name);
    let target = safe_str(&config.target_dir);
    let file_types = safe_str(&config.file_types);
    let advisor_model = safe_str(&config.advisor_model);
    let runner_model = safe_str(&config.runner_model);
    if config.max_explorers > 0 {
        three_tier_pipeline(
            &team,
            &target,
            &file_types,
            &advisor_model,
            &safe_str(&config.explorer_model),
            &runner_model,
            config,
        )
    } else {
        legacy_pipeline(
            &team,
            &target,
            &file_types,
            &advisor_model,
            &runner_model,
            config,
        )
    }
}
