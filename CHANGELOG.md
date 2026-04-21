# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
