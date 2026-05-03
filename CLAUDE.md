# Advisor — Opus-led Agent Team (Claude Code Native)

Two-model team using Claude Code's TeamCreate/Agent/SendMessage. No external API calls.
Models configurable via `TeamConfig(advisor_model=, runner_model=)` — defaults: `claude-opus-4-7` / `claude-sonnet-4-6`. Claude Code's Agent() tool accepts bare aliases (`opus`, `sonnet`, `haiku`) for "always-latest" or full `claude-<family>-<version>` IDs to pin a specific version; mid-form strings like `opus-4-5` are not accepted.

## Team Roles

| Role | Default Model | Agent Type | Job |
|------|---------------|------------|-----|
| **Advisor** | Opus 4.7 (`claude-opus-4-7`) | `advisor-executor` | Glob+Grep discovery, ranks P1–P5, sizes runner pool, **writes a unique, file-aware prompt for every runner**, dispatches explore + fix waves, live dialogue with runners, verifies each output as it lands |
| **Runner** | Sonnet 4.6 (`claude-sonnet-4-6`) | `code-review` | Reads files, finds issues, implements fixes. Each runner gets a domain-specific prompt from the advisor — not a generic template. Works ONLY on what the advisor hands it. In constant two-way conversation with the advisor (via team-lead relay). |

## Pipeline

### Step 1: Create Team

```
TeamDelete()  # clean slate
TeamCreate(name="review")
```

### Step 2: Spawn Opus advisor FIRST (no runners yet)

Opus does Glob+Grep structural discovery itself — cheap for its large
context window and the map is its to keep. It ranks files P1–P5 and
decides how many runners to spawn based on the codebase size (no
hardcoded default). It opens its report with `## Pool size: N — <rationale>`,
followed by a **Dispatch Plan** that includes a complete, custom prompt
for every runner — tailored to the files in that runner's batch using
context from its discovery pass.

```
Agent(
  name="advisor",
  subagent_type="advisor-executor",
  model="claude-opus-4-7",
  team_name="review",
  prompt=<build_advisor_prompt(config)>
)
```

The advisor prompt encodes interleaved thinking (reason between every
tool call, contemplate before committing, pivot when evidence reframes
the problem) and the full review-and-fix loop.

### Step 3: Spawn runners with the advisor's per-runner prompts

Wait for Opus's `## Pool size: N` and the **Dispatch Plan**. Spawn
exactly N runners using **Opus's verbatim per-runner prompts** — do
NOT substitute `build_runner_pool_prompt(...)`. The whole point of
having Opus go first is that each runner gets a powerful, domain-
specific briefing written by the strategist who just read the
structural map.

Runners are long-lived — reused across assignments for context accumulation.

```
Agent(
  name="runner-1",
  subagent_type="code-review",
  model="claude-sonnet-4-6",
  team_name="review",
  run_in_background=true,
  prompt=<verbatim text from Opus's "### runner-1 / #### Prompt" block>
)
```

After spawning, tell Opus: `"Pool of N runners is up."`

### Step 4: Live dialogue — explore wave

Opus dispatches explore assignments to runners via `SendMessage(to='runner-N')`.
Runners send all replies — questions, progress pings, draft findings,
final reports — to **team-lead**, who relays each one verbatim to the
advisor. The advisor responds directly to the runner. Throughout:

- Runners ask questions when stuck, send progress pings (to team-lead → advisor)
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
4. **Runner prompts come from Opus, not a template** — use the per-runner prompts in Opus's dispatch plan verbatim.
5. **Runners work ONLY on what Opus hands them.**
6. **Live dialogue, not checkpoints** — runners talk to Opus constantly.
7. **Runner reports go to team-lead; team-lead relays to the advisor** — verbatim, the moment they arrive. Do not batch or summarize.
8. **Every prompt ends with SendMessage-back.**
9. **Shutdown individually, not broadcast.**
10. **Fence untrusted data** in Opus prompts (code blocks).

## Python API

```python
from advisor import (
    default_team_config,       # TeamConfig with advisor_model/runner_model
    build_advisor_agent,       # Opus agent spec
    build_advisor_prompt,      # Opus prompt (interleaved thinking + full loop)
    build_runner_pool_agents,  # Sonnet pool agent specs (fallback path)
    build_runner_pool_prompt,  # generic runner prompt (fallback only — live pipeline uses Opus's per-runner prompts)
    build_runner_dispatch_messages,  # SendMessage specs per batch
    build_verify_message,      # SendMessage to resume advisor for verification
    render_pipeline,           # Print pipeline reference
)
```

## Activation

Invoke via `/advisor` slash command. Full protocol details in
`~/.claude/skills/advisor/SKILL.md`.

## Setup

```bash
uvx --from advisor-agent advisor install  # install from PyPI
uv sync                   # sync dependencies (dev)
uv run pytest             # run tests
python -m advisor         # run locally
```

## Behavioral Guidelines

Reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

Tradeoff: These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding
Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First
Minimum code that solves the problem. Nothing speculative.
- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes
Touch only what you must. Clean up only your own mess.

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution
Define success criteria. Loop until verified.

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

These guidelines are working if: fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
