# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — runner budget + scope anchors (drift + exhaustion defense)
- **`advisor/runner_budget.py`** — pure `RunnerBudget` dataclass,
  `parse_scope_anchor`, `update_budget`, `budget_status`,
  `stage_regressed`, `out_of_batch`, `format_budget_nudge`. Three
  layered signals: SCOPE anchor line per runner reply, per-runner
  output-byte budget (soft nudge at 60%, auto-rotate at 80%), and
  hard ceilings on bytes/file-reads/fixes as the safety net.
- **`TeamConfig.runner_output_byte_ceiling` / `.runner_file_read_ceiling`**
  — new configurable ceilings (defaults 80 000 bytes / 20 files).
- Runner prompt now requires every reply to open with
  `SCOPE: <file> · <reading|hypothesizing|confirming|fixing|done>`.
  Missing / drifting / regressing anchors are caught by the advisor
  deterministically, well before a finding lands.
- Advisor prompt gains a "Scope anchors and runner output budget"
  clause that mirrors the runner contract and encodes the soft/hard
  rotation protocol (BUDGET SOFT → compact, BUDGET ROTATE → handoff).

## [0.5.0] - 2026-04-22

### Added — SARIF + GitHub Action (v0.5 Phase 1)
- **`advisor/sarif.py`**: pure SARIF 2.1.0 emitter. `findings_to_sarif`
  converts a list of `Finding` objects into a schema-compliant run dict
  ready for GitHub Code Scanning. `synthesize_rule_id(severity,
  description)` produces stable `advisor/<sev>/<hash>` ids so repeated
  findings group under one rule in the UI. Absolute paths outside the
  target tree raise `ValueError` rather than leaking into CI artifacts.
- **`--sarif PATH`** on `advisor plan` and `advisor audit`: writes SARIF
  2.1.0 to PATH via the atomic-write helper. `plan --sarif` emits an
  empty-results document (real findings come from `audit --sarif`).
- **`Finding.rule_id: str | None = None`**: optional stable rule
  identifier. Existing Finding fields are unchanged; parsers tolerate
  presence and absence equally.
- **`.github/workflows/advisor.yml`**: reusable workflow that runs
  `advisor plan`, uploads SARIF to Code Scanning, and optionally posts a
  PR comment. Inputs: `target`, `min-priority`, `since`, `fail-on`,
  `preset`, `post-pr-comment`.
- README gains a **GitHub Action** section with a copy-paste example.

### Added — history-informed ranking (v0.5 Phase 2)
- **`history.load_recent_findings`** + **`history.file_repeat_scores`**:
  pure readers that aggregate `.advisor/history.jsonl` into a per-file
  "repeat offender" score with exponential decay (default half-life 30
  days).
- **`rank_files(history_scores=...)`**: optional per-file bonus bounded
  at **+1 tier** (P3→P4 never P3→P5 from history alone). Files with
  repeated findings float up the plan without drowning fresh risk.
- **`FocusTask.reasons`** surfaces `"repeat offender: N findings in last
  90d"` when history boosted the priority.
- **`advisor plan --no-history`** disables the bonus for deterministic
  CI plans.

### Added — rule-pack presets (v0.5 Phase 3)
- **`advisor/presets.py`** ships six `RulePack`s: `python-web`,
  `python-cli`, `node-api`, `typescript-react`, `go-service`,
  `rust-crate`. Each preset tweaks `file_types`, `min_priority`,
  `test_command`, and layers ecosystem-specific keywords onto the
  language-aware baseline.
- **`--preset NAME`** on `advisor plan`, `pipeline`, `prompt`.
- **`advisor presets` / `--json`** subcommand lists presets.

### Added — findings lifecycle (v0.5 Phase 4)
- **`--fail-on {low,medium,high,critical,never}`** on `plan` and
  `audit`: exits 4 when any finding ≥ threshold. `never` (default)
  preserves back-compat.
- **`advisor baseline create [TARGET]`** and
  **`advisor baseline diff`**: snapshot-and-compare mode for adopting
  advisor on existing codebases. `plan --baseline PATH` suppresses
  matching findings. JSONL, schema-versioned.
- **`advisor/suppressions.py`** + **`.advisor/suppressions.jsonl`**:
  targeted per-rule, per-file false-positive suppressions with expiry.
  Zero-deps JSONL (no YAML dep added — preserves the zero-runtime-deps
  invariant). Expired entries log at WARNING; findings above MEDIUM
  require both a non-empty `reason` and a future `until` date.
- **`advisor plan --format pr-comment`**: emits GitHub-flavored markdown
  summary suitable for a PR body. Safely escapes backticks and pipes.
- GHA workflow gains `post-pr-comment` input to post the summary via
  `actions/github-script`.

### Added — structural drift enforcement
- **`build_fix_assignment_message`** (`advisor.orchestrate`): new helper
  for building fix-assignment SendMessage specs with the runner's
  current fix-count budget stamped into every message header (e.g.
  `## Fix assignment (fix 4 of 5 — send CONTEXT_PRESSURE BEFORE
  accepting the next assignment)`). Raises `ValueError` if
  `fix_number > max_fixes_per_runner` (or `large_file_max_fixes` when
  `is_large_file=True`) — the advisor literally cannot dispatch an
  over-cap fix without the builder failing. Runners see the budget on
  every turn, not just once in their spawn prompt.
- **`check_batch_fix_budget`** (`advisor.orchestrate`): pre-flight
  validator that warns when a dispatch plan has batches whose size could
  over-run per-runner fix caps (including the tighter `large_file_max_fixes`
  cap when file line counts are provided). `advisor plan` now surfaces
  these warnings on stderr during pretty output and in the `budget_warnings`
  key of the JSON payload, so users see structural issues at plan time
  rather than mid-run.
- **PROTOCOL_VIOLATION named-stop clause** in the advisor prompt: before
  constructing any fix-assignment SendMessage, the advisor is told to
  verify three protocol invariants (fix count < cap, file in-batch,
  runner has not already pinged `CONTEXT_PRESSURE` without a rotation).
  If any would be violated, it must output the exact string
  `PROTOCOL_VIOLATION: <reason>` and rotate or re-plan. Named violations
  survive LLM pattern-matching where "don't do X" instructions do not.
- **Scope-drift filter** on `parse_findings_from_text` /
  `parse_findings_with_drift` (`advisor.verify`): accepts an optional
  `batch_files: set[str]` parameter. Findings whose `file_path` is not in
  the batch are dropped with a warning logged to `advisor.verify`. A
  runner assigned to `{auth.py, session.py}` that wanders into
  `crypto.py` cannot land findings against `crypto.py` in the final
  report — structural, not procedural.
- **`advisor audit RUN_ID [TARGET]`**: new post-hoc diagnostic. Loads a
  checkpoint and a transcript (from `--transcript FILE` or stdin) and
  reports fix counts per runner, cap overruns, `CONTEXT_PRESSURE` ping
  attribution + total count, rotation count (handoff messages),
  `PROTOCOL_VIOLATION` strings emitted, and findings on out-of-batch
  files. Supports `--json` for scripting. Turns "I feel like runners
  drifted" into a concrete evidence-backed report.

### Added — context-pressure knobs on the CLI
- `advisor plan --max-fixes-per-runner N` (and `pipeline`, `prompt`, `ui`):
  lower the hard cap on sequential fixes per runner when runners are
  exhausting context mid-fix-wave. Default remains `5`; the knob was
  previously only reachable by constructing a `TeamConfig` in Python.
- `advisor plan --large-file-line-threshold N` / `--large-file-max-fixes M`:
  tune the tighter fix cap that kicks in for batches containing any file
  at/above the threshold. Defaults remain `800` lines / `3` fixes.
- Checkpoints now persist `large_file_line_threshold` and
  `large_file_max_fixes`, so `advisor plan --resume` reproduces the
  capped-run configuration instead of silently using the live defaults.
  Legacy checkpoints (pre-upgrade) load cleanly with the documented
  defaults.

### Changed — runner prompt gives runners sharper self-awareness
- `build_runner_pool_prompt` now tells each runner, explicitly, that it
  has no direct read on its remaining context window (no tool exposes it
  to subagents, and subjective "I feel foggy" signals are unreliable)
  and to track two concrete proxies instead:
  - **Fix-count proxy (primary):** ping `CONTEXT_PRESSURE` after fix
    `(N-1)` of `max_fixes_per_runner` — *before* accepting the next
    assignment — so the advisor has one fix of runway to spawn the
    successor runner and build a handoff brief. Previously runners were
    told to ping "as they approach the cap", which in practice meant at
    the cap, which is too late.
  - **Read-count proxy (secondary):** if the runner has Read more than
    ~15 files in its session (explore + fixes combined), flag at-risk at
    the start of the next assignment. Catches heavy-cross-reference
    sessions that exhaust context before the fix count catches up.
  - Subjective symptoms (slower replies, hazy recall) are demoted to a
    backup-only signal.
- Advisor prompt mirror-updated: the strategist is now told to keep an
  explicit mental ledger of per-runner fix counts and to treat the
  early `CONTEXT_PRESSURE` ping as the normal rotation trigger, not an
  exception.

### Fixed
- `advisor --print-completion` error hint now points at the correct PyPI
  distribution name (`advisor-agent[completion]`) instead of the non-existent
  `advisor[completion]`, so users who copy-paste the suggested install command
  don't hit "No matching distribution found"
- `advisor doctor` now surfaces `ADVISOR_NO_NUDGE` in its `env_overrides`
  report (previously it tracked a phantom `ADVISOR_NO_AUTO_INSTALL` that is
  not read anywhere in the codebase)
- `advisor protocol` reference now matches the current defaults
  (`TeamCreate(name="review")`, `model="opus"` / `model="sonnet"`) instead of
  the stale `advisor-review` / `opus-4` / `sonnet-4` strings that would no
  longer work if pasted verbatim
- Advisor prompt template now threads `TeamConfig.team_name` through to the
  runner-identity briefing line; runners invoked under a custom `--team` no
  longer get told they are on `team review`
- `advisor plan --resume --estimate` now uses the checkpoint's recorded
  models / `test_command` when computing the cost estimate, instead of
  silently falling back to whatever CLI defaults the resuming invocation
  happens to have
- `advisor history --limit` now rejects zero / negative integers via
  argparse (was previously accepted and produced surprising slice results
  because `entries[-(-N):]` is not "last N")
- Dashboard JS no longer sends a dead `target` query param to `/api/plan`
  and `/api/cost` — the server has always ignored it (target is fixed when
  the server starts) and shipping it just hinted at multi-root support
  that does not exist

### Added — E1–E12 enhancement pack
- **E1 — Git-incremental scoping**: `advisor plan --since REF`, `--staged`,
  `--branch BASE` to scope reviews to changed files only. Turns advisor
  into a PR-review tool.
- **E2 — Language-aware priority keywords**: `rank.py` now ships Python,
  JavaScript/TypeScript, Go, and Rust keyword sets. Per-file language is
  auto-detected from extension and contributes to P1–P5 scoring alongside
  the cross-language baseline.
- **E3 — Cost / time estimates**: `advisor plan --estimate` prints
  per-run token and USD estimates based on file size + prompt overhead +
  runner fix count. `CostEstimate` and `estimate_cost()` are public API.
- **E4 — `advisor doctor` diagnostic**: health-checks Python, `git`,
  `claude` CLI, `~/.claude` integrity, install status (with version skew
  detection via the new badge), and active `ADVISOR_*` env overrides.
- **E5 — Env-var defaults**: `ADVISOR_MODEL`, `ADVISOR_RUNNER_MODEL`,
  `ADVISOR_MAX_RUNNERS`, `ADVISOR_FILE_TYPES`, `ADVISOR_MIN_PRIORITY`,
  `ADVISOR_TEST_COMMAND` — set once in shell profile / CI env for
  org-wide defaults.
- **E6 — JSON schema versioning**: `"schema_version": "1.0"` on every
  JSON output (`status`, `plan`, `install --check`, `doctor`, `history`)
  so downstream parsers can switch on it.
- **E7 — `plan --output FILE`**: dump JSON plan to a file for CI
  artifact archiving.
- **E8 — Test orchestration**: `TeamConfig.test_command` (+ CLI
  `--test-cmd "pytest -q"`) threads a test command into the advisor
  prompt so runners can loop on failures.
- **E9 — Findings history**: `.advisor/history.jsonl` is appended after
  each confirmed finding. Advisor prompt auto-injects the last N entries
  so recurring issues surface across runs. `advisor history` prints
  (or JSONs) the log.
- **E10 — Model-name validation**: `TeamConfig` warns once on unknown
  `advisor_model` / `runner_model` names (whitelist: `opus`/`sonnet`/
  `haiku` + long-form `claude-*`). Typos get flagged early.
- **E11 — Per-run checkpoint**: `advisor plan --checkpoint` writes
  `.advisor/run-<ts>.json` containing the rank + dispatch plan +
  rendered advisor prompt. `advisor plan --resume <ts>` reconstructs
  the plan without rescanning. Survives Claude Code session crashes.
- **E12 — CLAUDE.md version badge**: SKILL.md now carries a
  `<!-- advisor:X.Y.Z -->` badge. `status --json` surfaces
  `skill.installed_version`; `doctor` shows version-skew messages like
  "installed: 0.3.0, available: 0.4.0 — run: advisor install".

### Fixes / polish
- `InstallAction` is now exported from `advisor` top-level (was used
  internally and documented but not actually importable — fixed)
- `make release-check` — full pre-release gate (clean + lint + mypy +
  tests + wheel build + version/changelog sanity print)
- `make release` — prints the exact tag-and-push sequence after gate
- `test_all_symbols_in_all_resolve` — meta-test preventing future
  public-API drift
- `advisor.__version__` attribute (populated from installed package
  metadata)
- `mypy` now runs as part of `pre-commit` (previously CI-only)
- `.github/dependabot.yml` for weekly dev-dep updates
- Tag-triggered release workflow (PyPI trusted publishing)
- `SECURITY.md`, `CONTRIBUTING.md`, issue/PR templates
- Hypothesis `@settings(deadline=1000)` on fuzz tests to prevent
  Windows CI flakes

### Coverage
- Test count: 164 → **348** (+112%)
- Overall coverage: unmeasurable → **88%**
- `__main__.py` coverage: 68% → **74%** (new CLI surfaces tested)
- New module coverage: `checkpoint.py` 100%, `cost.py` 98%,
  `history.py` 94%, `doctor.py` 83%, `git_scope.py` 83%

## [0.4.0] - 2026-04-20

### Breaking
- Removed deprecated APIs: `build_explore_prompt`, `build_explore_agent`, `build_rank_agent`
  (use `build_advisor_prompt` + `build_runner_pool_prompt` instead)
- `advisor.orchestrate` is now a package (submodules: `config`, `advisor_prompt`,
  `runner_prompts`, `verify_dispatch`, `pipeline`). All public symbols remain
  importable from `advisor` and `advisor.orchestrate`.

### Added
- `InstallAction` string enum (`INSTALLED`/`UPDATED`/`UNCHANGED`/`REMOVED`/`ABSENT`/`SKIPPED`)
  replaces the bare-string action field on `InstallResult`. String equality still holds.
- `--json` output on `advisor status`, `advisor plan`, and `advisor install --check`
- `--quiet` flag on `install`/`uninstall` for CI use
- `--strict` flag on `advisor status` (exits `3` when anything is missing/outdated)
- `target` positional now defaults to `.` (current directory) for every subcommand
- `--context -` reads stdin so large scope descriptions can be piped in
- `.advisorignore`: `**` recursive globs now work via `PurePath.match`
- `verify.parse_findings_from_text` now only skips `### Finding` section headers
  (previously any line starting with `#` was dropped, mangling markdown bodies)
- CI workflow for Python 3.10–3.13 on Linux and Windows (ruff + mypy + pytest)
- `[project.optional-dependencies]` for `dev` and `test` extras
- Explicit `[tool.ruff]` / `[tool.mypy]` config in `pyproject.toml`
- Bash/zsh completion via `shtab` (`advisor --print-completion bash|zsh`)
- `advisor protocol` subcommand — prints the strict team-lifecycle sequence
  (`TeamCreate` → spawn advisor → runner pool → shutdowns → `TeamDelete`)
  as an ad-hoc reference without the full pipeline body
- Hypothesis fuzz test for `parse_findings_from_text` (100+ adversarial inputs)

### Fixed
- `_read_head` now uses `CONTENT_SCAN_LIMIT` (was reading 2× the scanned budget)
- `_safe_rglob` also catches `OSError` (symlink loops, permission errors)
- `_atomic_write_text` writes with mode `0o644` so editors/tools can read it
  (previously inherited `tempfile.mkstemp`'s `0o600`) and uses a randomized
  tmp name to avoid predictable-suffix TOCTOU on shared hosts
- `build_runner_dispatch_messages` now raises `ValueError` on empty `batch.tasks`
- `~50×` faster keyword scoring via a single combined regex (one `finditer` pass
  replaces ~50 `pattern.search` calls per file)
- Removed internal `rtk` CLI reference that leaked into the shipped CLAUDE.md nudge
- `create_focus_tasks` now substitutes `{file_path}` / `{priority}` / `{reasons}`
  in a **single pass** — a path containing a literal `{reasons}` token is no
  longer rewritten by the later substitution (order-dependence bug in the old
  `.replace()` chain)
- `build_advisor_prompt` body extracted to `advisor/orchestrate/_prompts/advisor.txt`
  so the 220-line strategist prompt is diffable as prose; the Python builder is
  now ~70 lines of placeholder wiring
- `colorize_markdown` — consolidated 3 per-depth header regexes (`_H2`/`_H3`/`_H4`)
  into one `_HEADER_RE` (7 → 5 passes); depth determines style
- `ensure_nudge` write failures now render as a yellow `⚠ warn` line (was
  invisible dim text) AND surface on `InstallResult.error` so programmatic
  consumers can detect partial installs
- `advisor --help` for `--file-types`: clarified that `*.py` already recurses via
  `rglob`; users must NOT pass `**/*.py`
- `uv.lock` regenerated — now reflects full dev dependency graph (was 134 bytes)

### Testing
- Parametrized tests over `PRIORITY_KEYWORDS` to lock in tier assignments
- Direct coverage for `cmd_install`/`cmd_uninstall`, `_safe_rglob`, `_config_from_args`
- Replaced bare `try/except` idioms with `pytest.raises(...)` for clarity
- Round-trip test: `format_findings_block → parse_findings_from_text` is identity
- Regression test for the `{file_path}`/`{reasons}` ordering bug in `create_focus_tasks`
- Header-depth parametrized tests for the consolidated `colorize_markdown` H2/H3/H4 pass
- `ensure_nudge` error-surfacing tests (result.error populated, warning is visible)
- `advisor protocol` subcommand coverage (lifecycle steps printed, no nudge side-effect)

### Hardening (follow-up)
- `_atomic_write_text` now **refuses to write through a symlink target** and
  opens the parent directory with `O_NOFOLLOW | O_DIRECTORY` (where available)
  to defend against swap-dir TOCTOU on shared hosts
- `supports_color()` is now **cached with env auto-invalidation** — every
  styled span previously re-read two env vars; the cache transparently
  notices `NO_COLOR`/`TERM` changes and the autouse `conftest.py` fixture
  invalidates between tests so `monkeypatch.setenv` still works
- `colorize_markdown` priority regex consolidated: `**P3**` (bold) and bare
  `P3` now match in a single alternation pass (5 → 4 regex scans total)
- `mypy strict = true` enabled; all 15 source modules type-check clean
- Prompt-engineering rationale documented in `docs/prompts.md` (why `.txt`
  extraction, why the fenced-goal idiom, why pre-finding verification is
  mandatory, etc.)
- Module-dependency graph and runtime flow documented in `docs/architecture.md`

## [0.3.0] - 2026-04-16

### Changed
- Advisor now uses Opus for direct discovery instead of delegating to Sonnet explorer
- Runners now receive custom prompts written by Opus based on structural discovery
- Added live two-way dialogue between advisor and runners throughout the pipeline

### Added
- `__slots__` on all dataclasses (`RankedFile`, `FocusTask`, `FocusBatch`, `Finding`) for improved memory
- `py.typed` marker file for PEP 561 type hint support
- `.advisorignore` support — drop a file of glob patterns into the project root to exclude paths
  - `load_advisorignore(base_dir)` function
  - `ignore_patterns` parameter on `rank_files()`
- Visual banner headers on `advisor status`
- First-run setup message with success box and quick-start guide
- Helpful tips in `advisor plan` empty-state
- Styling helpers: `banner()`, `success_box()`, `info_box()`, `warning_box()`
- All UI improvements respect `NO_COLOR=1`

## [0.2.0] - 2026-04-15

### Added
- Initial `/advisor` slash command via skill installation
- File priority ranking (P1–P5) based on security-relevant keywords
- Focus batching for parallel runner dispatch
- Verification pass to filter findings
- CLI commands: `pipeline`, `plan`, `prompt`, `install`, `uninstall`, `status`

## [0.1.0] - 2026-04-13

### Added
- Initial release
- Basic advisor/runner pattern implementation
