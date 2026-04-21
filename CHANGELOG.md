# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- `InstallAction` is now exported from `advisor` top-level (was used internally
  and documented in docs/CHANGELOG but not actually importable — fixed)
- `make release-check` — full pre-release gate (clean + lint + mypy + tests +
  wheel build + version/changelog sanity print)
- `make release` — prints the exact tag-and-push sequence after a clean gate
- `test_all_symbols_in_all_resolve` — meta-test that every name in
  `advisor.__all__` actually resolves, preventing future public-API drift

### Added (from previous Unreleased batch)
- `advisor.__version__` attribute (populated from installed package metadata)
- `mypy` now runs as part of `pre-commit` (previously CI-only)
- `.github/dependabot.yml` for weekly dev-dep updates (pip + GitHub Actions)
- Tag-triggered release workflow (`.github/workflows/release.yml`): pushing
  `v*` tags publishes to PyPI via trusted publishing
- `SECURITY.md` — threat model, reporting guidance, scope
- `CONTRIBUTING.md` — developer workflow, style, release process
- GitHub issue templates (bug / feature) and PR template
- Hypothesis `@settings(deadline=1000)` on fuzz tests to prevent Windows CI flakes

### Coverage
- Test count: 258 → **292** (+34)
- Overall coverage: 84% → **91%** (+7pt)
- `__main__.py` coverage: 68% → **85%** (+17pt)
- `rank.py` coverage: 80% → **93%** (+13pt)
- `install.py` coverage: 87% → **89%** (+2pt)

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
