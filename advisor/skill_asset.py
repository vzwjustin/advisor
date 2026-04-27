"""Bundled SKILL.md source for the `/advisor` slash command.

`advisor install` writes this to `~/.claude/skills/advisor/SKILL.md` so any
user who runs the installer gets the slash command auto-registered in
Claude Code. Keeping it as a Python string avoids needing package_data
configuration in pyproject.toml.
"""

from __future__ import annotations

from ._version import resolve_version

#: HTML-comment badge so ``advisor status`` can parse the installed version
#: without hashing the whole file. Must match ``_BADGE_RE`` in ``install.py``.
VERSION_BADGE = f"<!-- advisor:{resolve_version()} -->"


SKILL_MD = """---
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
__VERSION_BADGE__

# Advisor — Opus-led review-and-fix pipeline

**ACTIVATION SEQUENCE — follow in STRICT ORDER, no deviations:**

**Step 1 (text only):** Write `**Advisor mode**`

**Step 2 (BEFORE any Bash):** Call `TeamDelete()` — clears existing team

**Step 3 (BEFORE any Bash):** Call `TeamCreate(name="review")` — creates team

**Step 4 (only AFTER steps 2+3 complete):** Build the prompt via Bash —
write it to a unique temp file (mktemp avoids the `/tmp/advisor_prompt.txt`
symlink-overwrite footgun on multi-user hosts), then Read it back and
pass the content directly as the `prompt` parameter to Agent:

```bash
python3 -c "
import tempfile
from advisor import default_team_config, build_advisor_prompt
config = default_team_config(target_dir='TARGET', context='CONTEXT')
with tempfile.NamedTemporaryFile('w', prefix='advisor_prompt.', suffix='.txt', delete=False) as f:
    f.write(build_advisor_prompt(config))
    print(f.name)
"
```

The Bash command prints the unique tmpfile path. Then `Read("<that path>")`
and use its content as the Agent `prompt` parameter verbatim.

**Step 5:** Spawn Agent with the prompt content from Step 4.

**Step 6 (immediately after Step 5):** Send a begin trigger — agents in
mailbox mode go idle until a message arrives. Without this, the advisor's
first turn will never start:

```
SendMessage({ to: "advisor", message: "Begin." })
```

**Keep ALL output minimal:**
- NO file listings, NO ls, NO cat
- NO long explanations
- ONLY: TeamDelete → TeamCreate → Bash build → Read prompt → Agent spawn → SendMessage begin

A one-command entry point for a multi-agent review (and optional fix loop)
driven entirely by Opus with Sonnet runners as its hands. Runs through
Claude Code's native team tools — no external API calls.

Target directory: if the user did not specify one, default to the current
working directory.

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
```bash
python3 -c "
import tempfile
from advisor import default_team_config, build_advisor_prompt
config = default_team_config(
    target_dir='/Users/.../project',
    context='Audit codebase for UI enhancements'
)
with tempfile.NamedTemporaryFile('w', prefix='advisor_prompt.', suffix='.txt', delete=False) as f:
    f.write(build_advisor_prompt(config))
    print(f.name)
"
```

**NEVER do these:**
- ❌ `advisor prompt advisor ./src` (CLI dumps prompt text)
- ❌ Use kwarg `user_request` (it doesn't exist — use `context`)
- ❌ Pass Python code as the Agent prompt — the prompt must be the built string,
  not the code that builds it

**Stale-mailbox recovery (if you must re-spawn after a bad spawn):**
If you sent a `shutdown_request` to a badly-spawned advisor and need to
re-spawn, **always TeamDelete → TeamCreate first** before re-spawning.
A shutdown_request queued in the mailbox survives the old agent's death
and will be delivered to any new agent with the same name on the same
team — causing it to immediately shut down. TeamDelete clears all mailboxes.

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
  description: "Investigate, rank, and dispatch runners",
  subagent_type: "advisor-executor",
  model: "opus",
  team_name: "review",
  prompt: <build_advisor_prompt(config)>
})
```

**Immediately after spawning, send a begin trigger** — agents in mailbox
mode go idle until a message arrives:

```
SendMessage({ to: "advisor", message: "Begin." })
```

Build the prompt silently via inline Python (see IMPORTANT section above).
The advisor prompt encodes the full six-step flow:

1. **Glob + Grep directly** — Opus builds the structural map itself.
2. **Rank and size the pool** — Opus scores P1–P5 from the Glob+Grep map
   alone and decides how many runners to spawn. Opening line of its
   report to team-lead is always `## Pool size: N — <rationale>`. **No
   hardcoded defaults** — tiny repos get 1, medium get 2–3, large get 4–5,
   huge can justify more.
3. **Watch for runner reports** — runners have their batch assignments in
   their initial prompts and start reading immediately. Team-lead relays
   each runner report to Opus verbatim as it arrives.
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

**CRITICAL: spawn all N runners in ONE single response as parallel Agent tool calls. Never split across turns — runners that come up later miss reports already in flight.**

```
Agent({
  name: "runner-1",
  description: "Pool runner 1 — reads batch from initial prompt",
  subagent_type: "code-review",
  model: "sonnet",
  team_name: "review",
  run_in_background: true,
  prompt: <verbatim text from Opus's "### runner-1 / #### Prompt" block>
})
Agent({ name: "runner-2", description: "Pool runner 2 — reads batch from initial prompt", ... })   # only if Opus asked for more than 1
...
```

The generic `build_runner_pool_prompt` in the Python API is a fallback
for cases when you're spawning runners without the advisor — the live
pipeline never uses it.

After spawning the pool, tell Opus they're up — and that runners already started:

```
SendMessage({
  to: "advisor",
  message: "Pool of N runners is up. Runners have their batch assignments from initial prompts and are reading now — do NOT send separate explore dispatch messages. Watch your inbox for runner reports, which team-lead will relay to you verbatim."
})
```

### 4. Watch the live dialogue — relay runner reports to advisor

Runners send their reports to **team-lead** (not directly to the advisor). When any runner report arrives, relay it verbatim to the advisor immediately — before doing anything else:

```
SendMessage({
  to: "advisor",
  message: "Runner-N report (verbatim):\\n\\n<full message body>"
})
```

Do not summarize, filter, or batch. Relay each report the moment it arrives. This is the fix for mailbox delivery failures — the advisor sees every finding through the relay even when direct delivery drops.

Runners and Opus also talk continuously:

- Runners SendMessage team-lead with **questions**, **progress pings**, **draft findings**, and **final reports** — team-lead relays each to advisor.
- Opus SendMessages runners directly with **answers**, **CONFIRM / NARROW / REDIRECT** replies, and **proactive redirects**.
- Opus **verifies each runner's output the moment it lands** — per-runner CONFIRM (for explore) or CONFIRM / REVISE (for fix diffs).

Beyond relay, your job in this phase is to stay out of the way. Relay anything the user asks into the team, and relay Opus's final report back out.

### 5. Fix wave (if applicable)

If the user asked for fixes / enhancements / improvements (not a
read-only review), Opus will follow up the explore wave with a fix wave
in the same runner pool. Runners submit draft diffs to Opus for review
before finalizing. Opus CONFIRMs or sends REVISE.

### 6. Final report + shutdown

**HOLD until Opus's final report arrives.** Do NOT send `shutdown_request`
to any runner (or the advisor) until the advisor has sent its final
structured report to you. Sending shutdown early creates a dead dispatch —
Opus may still be routing work when you pull the plug. Wait for the
`## Summary` block, then shut down.

When Opus sends its final structured report to team-lead, the work is
done. Shut down individually (broadcast to `"*"` with structured
messages fails):

```
SendMessage({"to": "advisor",  "message": {"type": "shutdown_request"}})
SendMessage({"to": "runner-1", "message": {"type": "shutdown_request"}})
...
TeamDelete()
```

## Rules (non-negotiable)

1. **TeamDelete first** — a leader can only manage one team at a time.
2. **Opus goes first** — spawn Opus before any runners. The pool is
   right-sized AFTER Opus's first pass, not before.
3. **No hardcoded pool size** — Opus decides N every time. Never default
   to 5 or any other number.
4. **All runners spawn in ONE message** — put every Agent call in a single
   response so they come up in parallel. Splitting across turns means later
   runners miss reports already in flight.
5. **Runners work ONLY on what Opus hands them** — notice but never
   chase anything outside the current assignment.
6. **Live dialogue, not checkpoints** — runners ask questions freely,
   send progress pings, and expect Opus to answer in real time. Opus
   watches continuously and verifies each output as it lands.
7. **Runner reports go to team-lead; team-lead relays to advisor** —
   team-lead forwards each report verbatim to the advisor the moment it
   arrives. Do not batch or summarize.
8. **Every prompt ends with SendMessage-back** — agents go idle silently
   without an explicit instruction to message back.
9. **Shutdown individually, not broadcast** — send `shutdown_request` to
   each teammate by name.

## Quick start

- Manual: `/advisor` or `/advisor <path>` — defaults to cwd.
- Auto: when Claude judges a task warrants strategic multi-file review
  or a coordinated fix loop, invoke this skill unprompted.

The local `advisor` binary provides:
- `advisor pipeline <dir>` — pipeline reference as a sanity check
- `advisor prompt advisor <dir>` — the exact Opus advisor prompt
- `advisor plan <dir>` — local batch dispatch plan (no agents spawned)
"""

SKILL_MD = SKILL_MD.replace("__VERSION_BADGE__", VERSION_BADGE)
