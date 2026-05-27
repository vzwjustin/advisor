# advisor

![advisor demo](assets/demo.png)

A one-command, Opus-led code review-and-fix pipeline for Claude Code. Opus
goes first — does its own Glob+Grep discovery, ranks files P1–P5, and
**writes a unique, file-aware prompt for every runner** based on what it
just learned. Then it stays online and **actively steers the runners
throughout**: redirecting drift, answering questions in real time, verifying
each output the moment it lands, and adjusting the plan when a finding
changes the picture. Opus is the strategist that never goes idle until the
final report ships. Optional fix wave applies edits the same way.

No external API calls. Runs entirely through Claude Code's native
`TeamCreate` / `Agent` / `SendMessage` tools.

## Team

| Role | Model | Agent type | Job |
|------|-------|------------|-----|
| Advisor | Opus 4.7 (`claude-opus-4-7`) | `advisor-executor` | Glob+Grep discovery, P1–P5 ranking, sizes the pool, writes per-runner prompts, dispatches explore + fix waves — then stays live: redirects runner drift, answers questions in real time, verifies each output as it lands, adjusts plan mid-wave |
| Runner pool | Sonnet 4.6 (`claude-sonnet-4-6`) × N | `code-review` | Long-lived workers; each gets a custom prompt from the advisor; reports findings + diffs to team-lead, who relays to the advisor in live dialogue |

Priority scale: **P5** auth/secrets · **P4** user input/parsing · **P3** handlers/DB/exec · **P2** config/crypto/logging · **P1** utils/tests.

## Install (30 seconds)

```bash
pipx install advisor-agent
advisor status
```

That's it. The first run wires up `~/.claude/CLAUDE.md` and the `/advisor`
slash command automatically. The CLAUDE.md block also embeds a 4-rule
**Behavioral Guidelines** section (Think Before · Simplicity First ·
Surgical Changes · Goal-Driven Execution) that nudges Claude away from
the most common LLM coding mistakes. `advisor status` confirms what
landed where, and every upgrade prints a "What's new" digest from
`CHANGELOG.md` so you see what changed without leaving the terminal.

<details>
<summary>Other install methods</summary>

```bash
# Zero-install one-shot (uv)
uvx advisor-agent pipeline src/

# Plain pip
pip install advisor-agent

# From source
git clone https://github.com/vzwjustin/advisor && cd advisor && pip install -e .

# Local dev with uv tool (reinstall after edits)
uv tool install --reinstall .
```

Requires Python ≥ 3.10. Package name: `advisor-agent`. Import: `advisor`. CLI: `advisor`.

</details>

<details>
<summary>Manage the nudge / skill manually</summary>

```bash
advisor status               # install health check
advisor install              # append / update the nudge + skill (idempotent)
advisor install --check      # dry-run: print status, exit 3 if anything missing
advisor uninstall            # cleanly remove the nudge + skill
advisor install --path /x    # target a different CLAUDE.md
```

Opt out of auto-install with `ADVISOR_NO_NUDGE=1`. Suppress the diagnostic TTY spinner with `ADVISOR_QUIET=1`. The CLAUDE.md block is
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
advisor protocol                       # print the strict team-lifecycle protocol
advisor plan src/                      # rank local files, print dispatch plan
advisor plan src/ --json               # same, machine-readable for `jq` etc.
advisor plan src/ --format json        # explicit selector (alias of --json; pretty overrides)
advisor plan src/ --sarif out.sarif    # SARIF 2.1.0 output for Code Scanning
advisor audit RUN_ID [TARGET]          # post-hoc diagnostic for a completed run
advisor prompt advisor src/            # the advisor's prompt body
advisor prompt runner src/ --runner-id 1   # a runner's bootstrap prompt
advisor prompt verify src/ < findings  # verify-pass prompt
advisor status                         # install health check
advisor status --json                  # JSON-formatted health for scripting
advisor doctor                         # extended diagnostic: git/claude/python/env checks
advisor install                        # install nudge + /advisor skill (prints What's new on upgrade)
advisor update                         # self-upgrade via uv tool / pipx, then re-runs install
advisor changelog [VERSION]            # print bundled CHANGELOG section(s); --since X.Y.Z for a digest
advisor uninstall                      # remove nudge + /advisor skill
advisor ui                             # launch local web dashboard on 127.0.0.1:8765 (Findings · Live · Plan · Run config · Cost)
advisor live tail                      # tail the live event stream (the Live tab subscribes to this)
advisor history                        # recent findings from .advisor/history.jsonl
advisor history --stats                # aggregate: confirm rate, breakdowns, top files
advisor baseline create                # snapshot current findings as baseline
advisor baseline diff                  # compare current run vs. baseline
advisor checkpoints                    # list saved plan checkpoints
advisor checkpoints --rm RUN_ID        # delete a single checkpoint
advisor checkpoints --clear            # delete all checkpoints
advisor presets                        # list available rule-pack presets
advisor suppressions --list            # list active false-positive suppressions
advisor version                        # print version + environment info
```

Every subcommand's `target` defaults to `.` (current directory). Piping a
long scope description is supported via `--context -` (reads stdin).

Flags: `--team`, `--file-types`, `--max-runners` (advisory — Opus may
exceed for large repos), `--min-priority`, `--context`, `--advisor-model`,
`--runner-model`. Default models: `claude-opus-4-7` / `claude-sonnet-4-6` (full IDs pin the version; bare aliases `opus`/`sonnet`/`haiku` resolve to the latest at spawn time).

Context-pressure knobs (reduce runner context exhaustion):
`--max-fixes-per-runner N` · `--large-file-line-threshold N` · `--large-file-max-fixes M` · `--runner-output-char-ceiling K` · `--runner-file-read-ceiling L`.

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
    build_fix_assignment_message,   # stamped fix-count header per assignment
    check_batch_fix_budget,         # pre-flight cap validator
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
- `advisor/runner_budget.py` — `RunnerBudget`, scope-anchor parsing, per-runner output-char budget and rotation logic
- `advisor/install.py` — idempotent CLAUDE.md nudge + `/advisor` skill install/uninstall
- `advisor/doctor.py` — `DoctorReport`, extended git/claude/python/env diagnostics
- `advisor/audit.py` — `audit_transcript`, `format_audit_report`, post-hoc run diagnostics
- `advisor/baseline.py` — `read_baseline`, `write_baseline`, `diff_against_baseline`
- `advisor/checkpoint.py` — `Checkpoint`, save/load/list plan checkpoints for `--resume`
- `advisor/cost.py` — `CostEstimate`, rough token and cost range estimator
- `advisor/git_scope.py` — `resolve_git_scope`, git-incremental scoping (`--since`/`--staged`/`--branch`)
- `advisor/history.py` — `HistoryEntry`, confirmed findings log at `.advisor/history.jsonl`
- `advisor/pr_comment.py` — `format_pr_comment`, PR-body markdown formatter
- `advisor/presets.py` — `PRESETS`, `RulePack`, curated rule-pack bundles
- `advisor/sarif.py` — `findings_to_sarif`, SARIF 2.1.0 serializer
- `advisor/suppressions.py` — `Suppression`, per-rule false-positive suppressions
- `advisor/skill_asset.py` — `SKILL_MD`, bundled `/advisor` skill content
- `advisor/web/` — local web dashboard served by `advisor ui` (Findings · Live · Plan · Run config · Cost)
- `advisor/live.py` — ephemeral event stream (`<target>/.advisor/live/events.jsonl`) the dashboard's **Live** tab subscribes to
- `advisor/_style.py` — zero-dep ANSI styling (colors on by default)

## Live dashboard (new in 0.8.0)

Run `advisor ui` and open http://127.0.0.1:8765 to watch a `/advisor`
run in real time without keeping Claude Code in the foreground. The
**Live** tab polls `/api/events` every 2s and renders the team-lead's
event stream as a feed: each runner spawn, every report relay, every
fix dispatch, and the final run summary. Newly-arrived rows briefly
flash; FIFO-trimmed at 500 rows; respects `prefers-reduced-motion`.

The team-lead emits events via `advisor live record` at three
checkpoints (`run_start`, every `report_relay`, `run_end`), instructed
by the bundled `/advisor` skill body. Events are best-effort: a failed
write never halts the pipeline. Users who never start `advisor ui` see
no behavior change — the events file just accumulates harmlessly in
`<target>/.advisor/live/events.jsonl`.

The event store is deliberately separate from `history.jsonl`:
- `history.jsonl` — authoritative CONFIRMED findings; drives ranker
  boost, SARIF emission, repeat-offender analytics.
- `live/events.jsonl` — ephemeral event feed; opaque to the
  orchestrator, advisory to the dashboard, free-form payload.

For ad-hoc inspection from the terminal: `advisor live tail --limit 50`
(`--json` for scripting). `advisor live clear` removes the file; the
cursor preserves cleanly so the next run resumes the stream.

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

## GitHub Action

Reusable workflow that runs `advisor plan`, uploads SARIF 2.1.0 output to
GitHub Code Scanning, and (optionally) posts a PR comment. Paste this into
`.github/workflows/advisor.yml` in your repo:

```yaml
name: Advisor

on:
  pull_request:
  push:
    branches: [main]

jobs:
  advisor:
    uses: vzwjustin/advisor/.github/workflows/advisor.yml@v0.8.0
    with:
      target: "."
      min-priority: 3
      preset: "python-web"   # optional rule-pack tuning
      post-pr-comment: false
```

> [!NOTE]
> The `fail-on` parameter is enforced by a SARIF-parsing step that runs after `actions/upload-sarif`. Because `advisor plan --sarif` emits an empty-results document by design, the gate is a no-op unless a downstream step replaces `advisor.sarif` with real findings (e.g. SARIF captured from a live `/advisor` run). Threshold semantics match `advisor audit --fail-on`: the gate reads each result's `properties.severity` (which advisor's SARIF writer emits) so `critical` and `high` are correctly distinguished. For third-party SARIF that lacks `properties.severity`, the gate falls back to the SARIF level field (CRITICAL/HIGH → `error`, MEDIUM → `warning`, LOW → `note`) which cannot distinguish CRITICAL from HIGH.

Or roll your own: any CI system can run `advisor plan --sarif advisor.sarif`
and upload the file to whatever scanner you use.

## Presets

Curated rule-pack bundles tune file-type defaults and priority keywords for
common stacks:

| Preset             | Stack                        | Defaults                           |
|--------------------|------------------------------|------------------------------------|
| `general-python`   | Generic Python codebase      | `*.py`, no stack-specific boosting |
| `python-web`       | Flask / Django / FastAPI     | `*.py`, P5 auth keywords           |
| `python-cli`       | argparse / click CLIs        | `*.py`, P3 subprocess keywords     |
| `node-api`         | Express / Fastify / Koa      | `*.js,*.ts`, P5 JWT/session        |
| `typescript-react` | React + TS                   | `*.ts,*.tsx`, P4 DOM sinks         |
| `go-service`       | net/http services            | `*.go`, P3 net/http/sql            |
| `rust-crate`       | library / crate              | `*.rs`, P3 unsafe/transmute        |

```bash
advisor plan src/ --preset python-web
advisor presets            # list presets
advisor presets --json     # machine-readable
```

## Automation flags

Findings come from `advisor audit` (the verify pass) — `advisor plan`
prints the dispatch ranking. The gating + emit flags that depend on
findings (`--fail-on`, `--format pr-comment`, `--baseline`) only exist
on `audit`. `--sarif` exists on both — `plan` writes an empty-results
document (no findings yet), `audit` writes the real one.

| Flag                       | Applies to      | Effect                                    |
|----------------------------|-----------------|-------------------------------------------|
| `--sarif PATH`             | `plan`, `audit` | Write SARIF 2.1.0 for Code Scanning       |
| `--fail-on LEVEL`          | `audit`         | Exit 4 if any finding ≥ LEVEL             |
| `--format pr-comment`      | `audit`         | Emit a PR-body-ready markdown summary     |
| `--baseline PATH`          | `audit`         | Suppress findings matching a baseline     |
| `--no-history`             | `plan`          | Ignore history for deterministic CI plans |
| `--json` / `--output FILE` | `plan`, `audit` | Machine-readable output                   |

Exit codes: `0` clean · `4` `--fail-on` threshold tripped · `3` `--strict`
no-op or unhealthy install · `2` argparse / user error · `1` unexpected.

## Findings lifecycle

- **`advisor history`** — recent confirmed findings from `.advisor/history.jsonl` (`--stats` for an aggregate view)
- **`advisor baseline create`** — snapshot current findings as an accepted baseline
- **`advisor baseline diff`** — compare current run vs. baseline
- **`.advisor/suppressions.jsonl`** — per-rule, per-file suppressions with
  expiry dates (run `advisor suppressions` to list, add `--expired` to filter)

## Further reading

- [`docs/architecture.md`](docs/architecture.md) — module dependency graph,
  runtime flow, data contract, design invariants
- [`docs/prompts.md`](docs/prompts.md) — prompt engineering notes for
  contributors modifying prompt templates
