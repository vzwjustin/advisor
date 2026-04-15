# Advisor — Glasswing Agent Team (Claude Code Native)

Three-model team using Claude Code's TeamCreate/Agent/SendMessage. No external API calls.

## Team Roles

| Role | Model | Agent Type | Job |
|------|-------|------------|-----|
| **Explorer** | Sonnet | `Explore` | Fast file discovery — inventory only, no analysis |
| **Advisor** | Opus | `deep-reasoning` | Ranks files, plans dispatch, verifies findings |
| **Runner** | Sonnet | `code-review` | Focused single-file analysis — one per file, parallel |

> Haiku was dropped after empirical testing: Claude Code's built-in `Explore`
> subagent never honored the `model="haiku"` override in practice. The flow is
> otherwise unchanged from the original three-model design.

## Pipeline

### Step 1: Create Team + Explore

```
TeamCreate(name="glasswing")

Agent(
  name="explorer",
  subagent_type="Explore",
  model="sonnet",
  team_name="glasswing",
  prompt="Explore <target_dir>. Glob for *.py (skip __pycache__, .venv, .git).
         Read first 50 lines per file. Return one line per file:
         `<path>` — <summary>. Inventory only, no opinions.
         When done, send your complete output to the team lead via SendMessage(to='team-lead')."
)
```

### Step 2: Rank + Plan (Opus)

Feed explorer output to the advisor. Advisor ranks and produces a dispatch plan.

```
Agent(
  name="advisor",
  subagent_type="deep-reasoning",
  model="opus",
  team_name="glasswing",
  prompt="Rank these files P5 (auth/secrets) to P1 (utils/tests).
         <explorer output>
         Output: P<n> `path` — reason.
         Then ## Dispatch Plan: top 5 files at P3+, with one-line guidance each.
         When done, send your complete output to the team lead via SendMessage(to='team-lead')."
)
```

### Step 3: Analyze (Sonnet — parallel)

Dispatch all runners in a **single message** so they run in parallel.
Each runner gets one file + the advisor's guidance for that file.

```
Agent(
  name="runner-1", subagent_type="code-review", model="sonnet",
  team_name="glasswing", run_in_background=true,
  prompt="Review ONLY `<file>` (P<n>). <advisor guidance>.
         Report: File:line, Severity, Description, Evidence, Fix.
         If clean, say so. Do NOT review other files.
         When done, send your complete output to the team lead via SendMessage(to='team-lead')."
)
Agent(
  name="runner-2", ...same pattern...
)
# ...one per file from dispatch plan
```

### Step 4: Verify (Opus — reuse advisor)

Collect all runner outputs, send back to advisor for final review.

```
SendMessage(
  to="advisor",
  message="Verify findings from N runners across M files:
          <all runner outputs>
          Each finding: CONFIRMED or REJECTED + reason.
          ## Summary: X confirmed, Y rejected.
          ## Top 3 Actions: most critical fixes.
          When done, send your complete output to the team lead via SendMessage(to='team-lead')."
)
```

## Rules

1. **Teams mandatory** — `TeamCreate` before any agent spawn.
2. **Model discipline** — Opus decides (rank + verify), Sonnet explores and executes. No crossover.
3. **Parallel runners** — All Sonnet agents dispatched in one message with `run_in_background=true`.
4. **Advisor reuse** — Step 4 uses `SendMessage(to="advisor")` to resume the Opus agent, not a new spawn.
5. **Verification is non-optional** — Every pipeline ends with Opus confirming/rejecting findings.
6. **No external API calls** — Everything runs through Claude Code's native Agent/SendMessage tools.
7. **Every prompt must end with SendMessage-back** — Agents (especially Opus) go idle silently without this. Always append: `"When done, send your complete output to the team lead via SendMessage(to='team-lead')."` Learned from live testing where Opus advisor completed work but never reported back.
8. **Shutdown individually, not broadcast** — `SendMessage(to="*")` with structured messages (like `shutdown_request`) fails. Send shutdown to each teammate by name in separate calls.
9. **TeamDelete before TeamCreate** — A leader can only manage one team at a time. Always `TeamDelete` the old team before creating a new one. Make this the first step of every pipeline run.

## Python API

```python
from advisor import (
    default_team_config,    # Create TeamConfig
    build_explore_agent,    # Step 1: Haiku agent spec
    build_rank_agent,       # Step 2: Opus agent spec
    build_runner_agents,    # Step 3: Sonnet agent specs (parallel)
    build_verify_message,   # Step 4: SendMessage to advisor
    rank_files,             # Rank files by keyword signals
    create_focus_tasks,     # Generate one task per file
    parse_findings_from_text, # Parse runner output into Findings
    render_pipeline,        # Print full pipeline reference
)
```

## General Rules

- **rtk prefix** — All Bash commands: `rtk git status`, `rtk ls`, etc.
- **Immutability** — Never mutate. Return new objects.
- **TDD** — Write tests first. 80%+ coverage.
- **Code review** — Use `code-review` agent after any edits.
