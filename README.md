# advisor

Native Claude Code implementation of the Glasswing three-model analysis team. Coordinates Haiku, Opus, and Sonnet agents through Claude Code's `TeamCreate` / `Agent` / `SendMessage` tools — no external API calls.

## Team

| Role     | Model  | Agent type       | Job |
|----------|--------|------------------|-----|
| Explorer | Haiku  | `Explore`        | Fast file inventory (no analysis) |
| Advisor  | Opus   | `deep-reasoning` | Rank files, plan dispatch, verify findings |
| Runner   | Sonnet | `code-review`    | Focused single-file analysis, one agent per file, in parallel |

## Pipeline

1. **Explore** — Haiku globs the target dir and produces a `path — summary` inventory.
2. **Rank** — Opus scores each file P1–P5 and emits a dispatch plan with per-file guidance.
3. **Analyze** — Sonnet runners dispatch in parallel (`run_in_background=true`), one file each.
4. **Verify** — Opus is resumed via `SendMessage` and confirms/rejects each finding, then returns the top actions.

Priority scale: **P5** auth/secrets · **P4** user input/parsing · **P3** handlers/DB/exec · **P2** config/crypto/logging · **P1** utils/tests.

See `CLAUDE.md` for the full protocol and orchestration rules.

## Install

**One-liner (recommended):**

```bash
uvx --from git+https://github.com/vzwjustin/advisor advisor pipeline src/
```

No clone, no venv, no editable install. [`uv`](https://docs.astral.sh/uv/) fetches the package, runs the `advisor` CLI, and cleans up. Install uv once with `brew install uv` or `curl -LsSf https://astral.sh/uv/install.sh | sh`.

**pip / pipx:**

```bash
pipx install git+https://github.com/vzwjustin/advisor
# or
pip install git+https://github.com/vzwjustin/advisor
```

**Local dev:**

```bash
git clone https://github.com/vzwjustin/advisor && cd advisor
pip install -e .
```

Requires Python ≥ 3.10. Package name: `advisor-agent`. Import: `advisor`. CLI: `advisor`.

### Nudge Claude Code to use advisor (optional)

Append a sentinel-wrapped block to `~/.claude/CLAUDE.md` so Claude Code reaches for the advisor tool on complex tasks:

```bash
uvx --from git+https://github.com/vzwjustin/advisor advisor install
# or after pipx install:
advisor install      # idempotent — safe to run repeatedly
advisor uninstall    # cleanly removes the block
```

Override the target file with `--path /some/other/CLAUDE.md`. The block is wrapped in `<!-- advisor:nudge:start -->` / `<!-- advisor:nudge:end -->` markers, so reinstalling updates in place instead of duplicating.

## CLI

Three subcommands — all print to stdout so you can paste into Claude Code:

```bash
advisor pipeline src/                  # full 4-step pipeline reference
advisor plan src/                      # rank local files, print dispatch plan
advisor prompt explore src/            # Step 1 prompt (Haiku explorer)
advisor prompt rank src/  < inventory  # Step 2 prompt (Opus ranker)
advisor prompt verify src/ < findings  # Step 4 prompt (Opus verifier)
advisor install                        # append nudge to ~/.claude/CLAUDE.md
advisor uninstall                      # remove the nudge block
```

Flags: `--team`, `--file-types`, `--max-runners`, `--min-priority`, `--context`.

Typical vibe-coder flow: run `advisor pipeline <dir>`, paste it into Claude Code, let it drive the `TeamCreate` / `Agent` / `SendMessage` calls.

## Python API

```python
from advisor import (
    default_team_config,
    build_explore_agent,
    build_rank_agent,
    build_runner_agents,
    build_verify_message,
    rank_files,
    create_focus_tasks,
    parse_findings_from_text,
    render_pipeline,
)

config = default_team_config(
    target_dir="src/",
    team_name="glasswing",
    file_types="*.py",
    max_runners=5,
    min_priority=3,
)

print(render_pipeline(config))
explore_spec = build_explore_agent(config)          # Step 1
rank_spec    = build_rank_agent(inventory, config)  # Step 2
runner_specs = build_runner_agents(tasks, config)   # Step 3 (parallel)
verify_msg   = build_verify_message(findings, file_count=5, runner_count=5)  # Step 4
```

The builder functions return plain dicts — drop each one into a Claude Code `Agent(...)` or `SendMessage(...)` call.

## Modules

- `advisor/rank.py` — `rank_files`, `RankedFile`, keyword-signal priority ranking
- `advisor/focus.py` — `create_focus_tasks`, `FocusTask`, dispatch plan formatting
- `advisor/verify.py` — `Finding`, `VerifiedResult`, `parse_findings_from_text`
- `advisor/orchestrate.py` — team config and prompt/agent-spec builders for all four steps
- `advisor/install.py` — idempotent CLAUDE.md nudge install/uninstall

## Orchestration rules

- `TeamCreate` before any agent spawn; `TeamDelete` before creating a new team.
- Model discipline: Haiku explores, Opus decides, Sonnet executes — no crossover.
- Dispatch runners in a **single message** with `run_in_background=true` so they run in parallel.
- Step 4 **reuses** the advisor via `SendMessage(to="advisor")` — do not spawn a second Opus agent.
- Every agent prompt must end with `SendMessage(to='team-lead')`, otherwise agents go idle silently.
- Shut down teammates individually by name; broadcast shutdown does not work.

## Tests

```bash
pytest
pytest --cov=advisor --cov-report=term-missing
```
