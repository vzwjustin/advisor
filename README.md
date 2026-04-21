# advisor

A one-command, Opus-led code review-and-fix pipeline for Claude Code. The
advisor (Opus) goes first, does its own Glob+Grep discovery, ranks files
P1–P5, decides how many Sonnet runners to spawn, and **writes a unique,
file-aware prompt for every runner** based on what it just learned.
Runners and the advisor stay in live two-way conversation throughout —
runners ask questions, the advisor answers and verifies each output as
it lands. Optional fix wave applies edits the same way.

No external API calls. Runs entirely through Claude Code's native
`TeamCreate` / `Agent` / `SendMessage` tools.

## Team

| Role | Model | Agent type | Job |
|------|-------|------------|-----|
| Advisor | Opus | `deep-reasoning` | Glob+Grep discovery, P1–P5 ranking, sizes the pool, writes per-runner prompts, dispatches explore + fix waves, verifies each output as it lands |
| Runner pool | Sonnet × N | `code-review` | Long-lived workers; each gets a custom prompt from the advisor; reports findings + diffs back to the advisor in live dialogue |

Priority scale: **P5** auth/secrets · **P4** user input/parsing · **P3** handlers/DB/exec · **P2** config/crypto/logging · **P1** utils/tests.

## Install (30 seconds)

```bash
pipx install git+https://github.com/vzwjustin/advisor
advisor status
```

That's it. The first run wires up `~/.claude/CLAUDE.md` and the `/advisor`
slash command automatically. `advisor status` confirms what landed where.

<details>
<summary>Other install methods</summary>

```bash
# Zero-install one-shot (uv)
uvx --from git+https://github.com/vzwjustin/advisor advisor pipeline src/

# Plain pip
pip install git+https://github.com/vzwjustin/advisor

# Local dev
git clone https://github.com/vzwjustin/advisor && cd advisor && pip install -e .

# Local dev with uv tool (reinstall after edits)
uv tool install --reinstall .
```

Requires Python ≥ 3.10. Package name: `advisor-agent`. Import: `advisor`. CLI: `advisor`.

</details>

<details>
<summary>Manage the nudge / skill manually</summary>

```bash
advisor status               # health check (alias: advisor doctor)
advisor install              # append / update the nudge + skill (idempotent)
advisor install --check      # dry-run: print status, exit 3 if anything missing
advisor uninstall            # cleanly remove the nudge + skill
advisor install --path /x    # target a different CLAUDE.md
```

Opt out of auto-install with `ADVISOR_NO_NUDGE=1`. The CLAUDE.md block is
wrapped in `<!-- advisor:nudge:start -->` / `<!-- advisor:nudge:end -->`
markers so reinstalls update in place.

</details>

## Usage

Invoke from inside Claude Code:

```
/advisor                    # review the cwd
/advisor src/               # review a specific dir
/advisor review the auth flow      # add scope context
```

Or use the standalone CLI to inspect the prompts and plans:

```bash
advisor pipeline src/                  # full pipeline reference
advisor plan src/                      # rank local files, print dispatch plan
advisor plan src/ --json               # same, machine-readable for `jq` etc.
advisor prompt advisor src/            # the advisor's prompt body
advisor prompt runner src/ --runner-id 1   # a runner's bootstrap prompt
advisor prompt verify src/ < findings  # verify-pass prompt
advisor status                         # health check (alias: doctor)
advisor status --json                  # JSON-formatted health for scripting
advisor install                        # install nudge + /advisor skill
advisor uninstall                      # remove nudge + /advisor skill
```

Every subcommand's `target` defaults to `.` (current directory). Piping a
long scope description is supported via `--context -` (reads stdin).

Flags: `--team`, `--file-types`, `--max-runners` (advisory — Opus may
exceed for large repos), `--min-priority`, `--context`, `--advisor-model`,
`--runner-model`. Default models: `opus` / `sonnet`.

Automation flags: `--json` on `status`/`plan`/`install --check`,
`--quiet` on `install`/`uninstall`, `--strict` on `status`/`install`/`uninstall`
(exit `3` when nothing changed or the install is unhealthy).

Colors are on by default. Opt out with `NO_COLOR=1` or `TERM=dumb`.

## Excluding files (`.advisorignore`)

Drop an `.advisorignore` file into your project root to skip paths during
`advisor plan` and the live pipeline:

```gitignore
# comments begin with #
tests/            # skip directories (trailing slash)
*.md              # skip by filename glob
vendor/
generated/**/*.py # ** recursive globs are supported
```

Patterns follow ``fnmatch`` semantics for filename matches, and use
``PurePath.match`` when ``**`` is present. Bare words match any path
component (``docs`` matches both ``docs/`` and ``foo/docs/bar.py``).

## Python API

```python
from advisor import (
    default_team_config,
    build_advisor_agent,
    build_advisor_prompt,
    build_runner_pool_agents,
    build_runner_pool_prompt,
    build_runner_dispatch_messages,
    build_runner_handoff_message,
    build_verify_dispatch_prompt,
    build_verify_message,
    rank_files,
    load_advisorignore,
    create_focus_tasks,
    create_focus_batches,
    parse_findings_from_text,
    format_findings_block,
    render_pipeline,
)

config = default_team_config(
    target_dir="src/",
    team_name="review",
    file_types="*.py",
    max_runners=5,
    min_priority=3,
)

print(render_pipeline(config))
advisor_spec = build_advisor_agent(config)
# After Opus produces its dispatch plan, spawn each runner with the
# verbatim per-runner prompt from that plan — not a generic builder.
```

The builder functions return plain dicts — drop each one into a Claude
Code `Agent(...)` or `SendMessage(...)` call.

## Modules

- `advisor/orchestrate/` — `TeamConfig`, advisor + runner prompt builders,
  dispatch helpers, pipeline renderer (package: `config`, `advisor_prompt`,
  `runner_prompts`, `verify_dispatch`, `pipeline`)
- `advisor/rank.py` — `rank_files`, `RankedFile` (keyword-signal priority ranking)
- `advisor/focus.py` — `create_focus_tasks` / `create_focus_batches`, plan formatters
- `advisor/verify.py` — `Finding`, `parse_findings_from_text`, verify-pass builders
- `advisor/install.py` — idempotent CLAUDE.md nudge + `/advisor` skill install/uninstall
- `advisor/_style.py` — zero-dep ANSI styling (colors on by default)

## Orchestration rules

- `TeamCreate` before any agent spawn; `TeamDelete` before creating a new team.
- Opus goes first — no runners until Opus's first pass produces a pool size.
- Each runner is spawned with the **verbatim per-runner prompt** from
  Opus's dispatch plan. Don't substitute a generic template.
- Dispatch runners in a **single message** with `run_in_background=true`
  so they come up in parallel.
- Every agent prompt must end with a `SendMessage(...)` — agents go idle
  silently otherwise.
- Shut down teammates individually by name; broadcast shutdown does not work.

See `CLAUDE.md` for the full protocol.

## Tests

```bash
pip install -e ".[dev]"
make check        # ruff + mypy + pytest
pytest --cov=advisor --cov-report=term-missing
```

## Further reading

- [`docs/architecture.md`](docs/architecture.md) — module dependency graph,
  runtime flow, data contract, design invariants
- [`docs/prompts.md`](docs/prompts.md) — prompt engineering notes for
  contributors modifying prompt templates
