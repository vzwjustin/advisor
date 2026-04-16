# Advisor — Glasswing Agent Team (Claude Code Native)

Two-model team using Claude Code's TeamCreate/Agent/SendMessage. No external API calls.
Models configurable via `TeamConfig(advisor_model=, runner_model=)` — defaults: opus/sonnet.

## Team Roles

| Role | Default Model | Agent Type | Job |
|------|---------------|------------|-----|
| **Advisor** | Opus | `deep-reasoning` | Glob+Grep discovery, ranks P1–P5, sizes runner pool, dispatches explore + fix waves, live dialogue with runners, verifies each output as it lands |
| **Runner** | Sonnet | `code-review` | Reads files, finds issues, implements fixes. Works ONLY on what the advisor hands it. In constant two-way conversation with the advisor. |

## Pipeline

### Step 1: Create Team

```
TeamDelete()  # clean slate
TeamCreate(name="glasswing")
```

### Step 2: Spawn Opus advisor FIRST (no runners yet)

Opus does Glob+Grep structural discovery itself — cheap for its large
context window and the map is its to keep. It ranks files P1–P5 and
decides how many runners to spawn based on the codebase size (no
hardcoded default). It opens its report with `## Pool size: N — <rationale>`.

```
Agent(
  name="advisor",
  subagent_type="deep-reasoning",
  model="opus",
  team_name="glasswing",
  prompt=<build_advisor_prompt(config)>
)
```

The advisor prompt encodes interleaved thinking (reason between every
tool call, contemplate before committing, pivot when evidence reframes
the problem) and the full review-and-fix loop.

### Step 3: Spawn runner pool (when advisor reports pool size)

Wait for Opus's `## Pool size: N`. Spawn exactly N runners. Runners
are long-lived — reused across assignments for context accumulation.

```
Agent(
  name="runner-1",
  subagent_type="code-review",
  model="sonnet",
  team_name="glasswing",
  run_in_background=true,
  prompt=<build_runner_pool_prompt(1, config)>
)
```

After spawning, tell Opus: `"Pool of N runners is up."`

### Step 4: Live dialogue — explore wave

Opus dispatches explore assignments to runners via `SendMessage(to='runner-N')`.
Runners read files end-to-end and report findings back to the advisor
(not team-lead). Throughout:

- Runners ask questions when stuck, send progress pings
- Opus answers in real time, shares context between runners
- Opus verifies each runner's output the moment it lands (CONFIRM / NARROW / REDIRECT)
- Opus proactively redirects runners that drift off-scope

### Step 5: Fix wave (if user asked for enhancements/fixes)

Opus reasons over all findings, builds a fix plan, then dispatches fix
assignments to the same runner pool. Runners implement changes, submit
diffs to Opus for review before finalizing.

### Step 6: Final report + shutdown

Opus sends the final structured report to team-lead. Shut down individually:

```
SendMessage({ to: "advisor",  message: { type: "shutdown_request" } })
SendMessage({ to: "runner-1", message: { type: "shutdown_request" } })
TeamDelete()
```

## Rules

1. **TeamDelete before TeamCreate** — one team at a time.
2. **Opus goes first** — no runners before Opus's first pass.
3. **No hardcoded pool size** — Opus decides every time.
4. **Runners work ONLY on what Opus hands them.**
5. **Live dialogue, not checkpoints** — runners talk to Opus constantly.
6. **Runner reports go to the advisor** — Opus verifies and relays.
7. **Every prompt ends with SendMessage-back.**
8. **Shutdown individually, not broadcast.**
9. **Fence untrusted data** in Opus prompts (code blocks).

## Python API

```python
from advisor import (
    default_team_config,       # TeamConfig with advisor_model/runner_model
    build_advisor_agent,       # Opus agent spec
    build_advisor_prompt,      # Opus prompt (interleaved thinking + full loop)
    build_runner_pool_agents,  # Sonnet pool agent specs
    build_runner_pool_prompt,  # Runner prompt (live dialogue + explore/fix)
    build_runner_dispatch_messages,  # SendMessage specs per batch
    build_verify_message,      # SendMessage to resume advisor for verification
    render_pipeline,           # Print pipeline reference
)
```

## Activation

Invoke via `/advisor` slash command. Full protocol details in
`~/.claude/skills/advisor/PROTOCOL.md`.

## General Rules

- **rtk prefix** — All Bash commands: `rtk git status`, `rtk ls`, etc.
- **Immutability** — Never mutate. Return new objects.
- **TDD** — Write tests first. 80%+ coverage.
- **Code review** — Use `code-review` agent after any edits.
