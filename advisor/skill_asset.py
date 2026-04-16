"""Bundled SKILL.md source for the `/advisor` slash command.

`advisor install` writes this to `~/.claude/skills/advisor/SKILL.md` so any
user who runs the installer gets the slash command auto-registered in
Claude Code. Keeping it as a Python string avoids needing package_data
configuration in pyproject.toml.
"""

from __future__ import annotations

SKILL_MD = '''---
name: advisor
description: >-
  Opus-led code review-and-fix pipeline. The advisor (Opus) wakes up first,
  does Glob+Grep structural discovery itself, ranks files P1–P5, decides how
  many Sonnet runners the work warrants, and writes a **unique, tailored
  prompt for every single runner** based on what it just learned about each
  file. Runners are not handed a generic prompt — each gets a domain-specific
  briefing built by the advisor. Opus and runners stay in live two-way
  conversation throughout: runners ask questions and send progress pings,
  Opus answers in real time and verifies each runner's output the moment it
  lands.

  TRIGGER when: the user invokes /advisor, says "run the advisor", or
  "advisor mode", or when Claude judges that a task benefits from strategic
  code review across multiple files (3+ files, security audit, root-cause
  hunt, architectural review, concurrency bugs, or fix-and-review work).

  DO NOT TRIGGER when: the task is a single trivial edit, a typo fix, a one
  file look-up, or the user explicitly wants something else.
origin: custom
---

# Advisor — Opus-led review-and-fix pipeline

**ACTIVATION SEQUENCE — follow in STRICT ORDER, no deviations:**

**Step 1 (text only):** Write `**Advisor mode**`

**Step 2 (BEFORE any Bash):** Call `TeamDelete()` — clears existing team

**Step 3 (BEFORE any Bash):** Call `TeamCreate(name="review")` — creates team

**Step 4 (only AFTER steps 2+3 complete):** Build prompt silently. Prefer the
in-conversation **Python tool** (see the concrete example below). If you
must fall back to Bash, write to a per-invocation tmp file — never a
predictable path like `/tmp/adv.txt` (world-readable on shared hosts, leaks
the `context` string across local accounts):

```bash
ADV_PROMPT=$(mktemp -t advisor-prompt)
ADV_PROMPT="$ADV_PROMPT" python3 -c "import os; from advisor import default_team_config, build_advisor_prompt; open(os.environ['ADV_PROMPT'],'w').write(build_advisor_prompt(default_team_config('TARGET', context='CONTEXT')))" 2>/dev/null
```

**Step 5:** Spawn Agent with the prompt you just built (read from
`$ADV_PROMPT` if you used the Bash fallback, or pass the Python-tool
result directly).

**RULE: NO Bash before TeamDelete and TeamCreate. ZERO exceptions.**
The user must see TeamDelete and TeamCreate FIRST before any Bash runs.
Bash is for prompt-building ONLY and happens AFTER the team exists.

**Keep ALL output minimal:**
- NO file listings, NO ls, NO cat
- NO verbose Python in output
- NO long explanations
- ONLY: TeamDelete → TeamCreate → (quiet bash) → Agent spawns

A one-command entry point for a multi-agent review (and optional fix loop)
driven entirely by Opus with Sonnet runners as its hands. Runs through
Claude Code's native team tools — no external API calls.

Target directory: if the user did not specify one, default to the current
working directory.

**IMPORTANT: Build prompts using the Python tool (NOT Bash python -c).**

**Correct API signature:**
```python
default_team_config(
    target_dir: str,           # required - the directory to review
    team_name: str = "review",
    file_types: str = "*.py",
    max_runners: int = 5,
    min_priority: int = 3,
    context: str = "",          # ← user's request goes HERE (not 'user_request')
    advisor_model: str = "opus",
    runner_model: str = "sonnet",
)
```

**Concrete example (use this pattern verbatim):**
```python
from advisor import default_team_config, build_advisor_prompt
config = default_team_config(
    target_dir="/Users/.../project",
    context="Audit codebase for UI enhancements"  # user's request as context
)
prompt = build_advisor_prompt(config)
# Now pass `prompt` to Agent() — DO NOT print
```

**NEVER do these (they dump verbose text to user output):**
- ❌ `advisor prompt advisor ./src` (CLI dumps prompt text)
- ❌ `Bash(python3 -c "...")` (shows code in output)
- ❌ Use kwarg `user_request` (it doesn't exist — use `context`)

User sees only: `**Advisor mode**` → `[TeamCreate]` → `[Agent...]`

## Architecture

| Role | Model | Job |
|------|-------|-----|
| **Advisor** | Opus | Glob+Grep directly, rank P1–P5, size the runner pool, dispatch explore and fix assignments, watch runners live, verify each output as it lands |
| **Runner pool** | Sonnet × N | Long-lived workers. Read files, find issues, implement fixes. In live two-way conversation with the advisor the whole time. Work ONLY on what the advisor hands them. |

No separate explorer-helper. Opus handles cheap structural discovery itself
because its large context window is the right place to hold the repo map.
Runners are the only Sonnet workers and they do both the reading and the
writing.

## The loop Opus drives

```
   [Opus]                 [Runners]
  Glob + Grep     →  (map, in Opus's context)
  rank + pool size  →  team-lead spawns N runners
  dispatch explore  →  runners read files
       ↑                    ↓
       └── findings ←───────┘    (live Q&A both ways)
  reason + plan     →
  dispatch fixes    →  runners implement   (optional)
       ↑                    ↓
       └──  diffs  ←────────┘
  verify each result → final report to team-lead
```

## Protocol

### 0. Clean slate (ALWAYS do this first)

**Check for existing team and delete if present:**

```
⏺ ListTeamInfo
  ⎿ (check if team "review" exists)

If team exists → TeamDelete() first
If error "Already leading team" → TeamDelete() then retry
```

**Never skip this step** — the error "Already leading team" means you forgot to delete first.

### 1. Create the team (no runners yet)

```
TeamCreate({ team_name: "review", description: "Code review of <target>" })
```

### 2. Spawn the Opus advisor FIRST

```
Agent({
  name: "advisor",
  subagent_type: "deep-reasoning",
  model: "opus",
  team_name: "review",
  prompt: <build_advisor_prompt(config)>
})
```

Build the prompt silently via inline Python (see IMPORTANT section above).
The advisor prompt encodes the full six-step flow:

1. **Glob + Grep directly** — Opus builds the structural map itself.
2. **Rank and size the pool** — Opus scores P1–P5 from the Glob+Grep map
   alone and decides how many runners to spawn. Opening line of its
   report to team-lead is always `## Pool size: N — <rationale>`. **No
   hardcoded defaults** — tiny repos get 1, medium get 2–3, large get 4–5,
   huge can justify more.
3. **Dispatch explore wave** — after team-lead spawns the right-sized pool,
   Opus SendMessages each batch to `runner-N`. Runners read files end-to-
   end and report findings back to Opus (not team-lead — the advisor is
   in the middle).
4. **Reason over findings, build fix plan** — Opus reasons across all
   runner reports and decides what to do.
5. **Dispatch fix wave** (only if user asked for fixes/enhancements).
   Opus SendMessages each fix assignment with `File / Problem / Change /
   Acceptance`. Runners apply edits and submit diffs to Opus for review.
6. **Verify and report** — Opus sends the final structured report to
   team-lead.

### 3. Spawn the runner pool with the advisor's per-runner prompts

Wait for Opus's first SendMessage to team-lead. It will open with
`## Pool size: N — <rationale>` followed by a **Dispatch Plan** that
includes a complete, file-aware prompt for every runner — Opus tailors
each one to the specific files in that runner's batch using context
from its Glob+Grep pass.

**Use Opus's per-runner prompts verbatim.** Do not substitute a generic
`build_runner_pool_prompt(...)` — the whole point of having Opus go
first is that each runner gets a powerful, domain-specific briefing
written by the strategist who just read the structural map. A runner
reviewing auth code gets a different prompt than one reviewing CLI
utilities.

Spawn exactly N runners in a single message so they come up in parallel:

```
Agent({
  name: "runner-1",
  subagent_type: "code-review",
  model: "sonnet",
  team_name: "review",
  run_in_background: true,
  prompt: <verbatim text from Opus's "### runner-1 / #### Prompt" block>
})
Agent({ name: "runner-2", ... })   # only if Opus asked for more than 1
...
```

The generic `build_runner_pool_prompt` in the Python API is a fallback
for cases when you're spawning runners without the advisor — the live
pipeline never uses it.

After spawning the pool, tell Opus they're up:

```
SendMessage({
  to: "advisor",
  message: "Pool of N runners is up and waiting for batch assignments."
})
```

### 4. Watch the live dialogue

Once Opus dispatches explore batches, Opus and runners talk continuously:

- Runners SendMessage Opus with **questions**, **progress pings**, **draft
  findings**, and **final reports**.
- Opus SendMessages runners with **answers**, **CONFIRM / NARROW /
  REDIRECT** replies, **context from other runners**, and **proactive
  redirects** when they drift off-topic.
- Opus **verifies each runner's output the moment it lands** — it does
  NOT wait for a bulk end-of-run verification. Per-runner CONFIRM (for
  explore) or CONFIRM / REVISE (for fix diffs).

As team-lead, your job in this phase is mostly to stay out of the way.
Relay anything the user asks into the team, and relay Opus's final report
back out.

### 5. Fix wave (if applicable)

If the user asked for fixes / enhancements / improvements (not a
read-only review), Opus will follow up the explore wave with a fix wave
in the same runner pool. Runners submit draft diffs to Opus for review
before finalizing. Opus CONFIRMs or sends REVISE.

### 6. Final report + shutdown

When Opus sends its final structured report to team-lead, the work is
done. Shut down individually (broadcast to `"*"` with structured
messages fails):

```
SendMessage({ to: "advisor",  message: { type: "shutdown_request" } })
SendMessage({ to: "runner-1", message: { type: "shutdown_request" } })
...
TeamDelete()
```

## Rules (non-negotiable)

1. **TeamDelete first** — a leader can only manage one team at a time.
2. **Opus goes first** — spawn Opus before any runners. The pool is
   right-sized AFTER Opus's first pass, not before.
3. **No hardcoded pool size** — Opus decides N every time. Never default
   to 5 or any other number.
4. **Runners work ONLY on what Opus hands them** — notice but never
   chase anything outside the current assignment.
5. **Live dialogue, not checkpoints** — runners ask questions freely,
   send progress pings, and expect Opus to answer in real time. Opus
   watches continuously and verifies each output as it lands.
6. **Runner reports go to the advisor, not team-lead** — the advisor
   verifies and relays. Team-lead only sees Opus's final report.
7. **Every prompt ends with SendMessage-back** — agents go idle silently
   without an explicit instruction to message back.
8. **Shutdown individually, not broadcast** — send `shutdown_request` to
   each teammate by name.

## Quick start

- Manual: `/advisor` or `/advisor <path>` — defaults to cwd.
- Auto: when Claude judges a task warrants strategic multi-file review
  or a coordinated fix loop, invoke this skill unprompted.

The local `advisor` binary provides:
- `advisor pipeline <dir>` — pipeline reference as a sanity check
- `advisor prompt advisor <dir>` — the exact Opus advisor prompt
- `advisor plan <dir>` — local batch dispatch plan (no agents spawned)
'''
