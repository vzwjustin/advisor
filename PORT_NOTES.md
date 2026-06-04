# PORT_NOTES.md — advisor Python → Rust migration status

Companion to `RUST_PORT_PLAN.md` (the plan). This file tracks **what is
actually ported and validated** so far, the Python↔Rust parity matrix, and the
exact next step. The Python implementation is unchanged and still ships as the
reference; the Rust binary is additive until parity is proven (per the plan's
§10 cutover gate).

## Build & test commands

```bash
# Rust (from repo root)
cargo build            # debug build
cargo build --release  # optimized; binary at target/release/advisor
cargo test             # 26 unit + golden-parity tests
cargo clippy --all-targets -- -D warnings
cargo fmt --check

# Python (unchanged reference)
pip install -e ".[dev]"
pytest -q

# Cross-language parity (presets, byte-exact)
./scripts/parity_check.sh
```

## What was ported (real, tested, parity-verified)

Each Rust module below mirrors its Python counterpart one-to-one. Every ported
function carries unit tests whose expected values were **captured from the
running Python implementation** (not hand-derived), so the tests are genuine
parity assertions.

| Rust module | Python source | Ported surface | Validation |
|-------------|---------------|----------------|------------|
| `src/fence.rs` | `orchestrate/_fence.py` | `sanitize_inline`, `fence` (+ linebreak/invisible strip) | Unit tests vs Python outputs |
| `src/style.rs` | `_style.py` | `strip_ansi` (CSI+OSC regex) | Unit tests |
| `src/fs.rs` | `_fs.py` | `normalize_path`, `validate_file_types`, `CONTENT_SCAN_LIMIT`, POSIX `normpath` | Reference table vs Python (10 cases) |
| `src/checkpoint.rs` | `checkpoint.py` | `Checkpoint`, `checkpoint_path`, run-id validation, `load_checkpoint`, `list_checkpoints` | unit (load round-trip, run-id validation) + backs `advisor audit` |
| `src/audit.rs` | `audit.py` | transcript analysis: fix-assignment counting + runner attribution (SendMessage-envelope/proximity), cap overruns, CONTEXT_PRESSURE attribution, PROTOCOL_VIOLATION dedup/cap, fenced-block stripping, handoff/rotation count, scope-drift classification; `AuditReport`, `audit_to_dict`, `format_audit_report` | **Golden JSON** vs Python (full `audit_to_dict` + `format` on a crafted transcript) |
| `src/suppressions.rs` | `suppressions.py` | `Suppression` + `matches`, `severity_from_rule_id`, `**`-aware glob match (shared with rank) + fnmatch fallback, `until` date validation/expiry (UTC), `load_suppressions` (structural validation → errors), `apply_suppressions` | **Golden JSON** vs Python (severity ranks, matches, apply, load valid + 4 error shapes) |
| `src/baseline.rs` | `baseline.py` | `BaselineEntry`/`BaselineDiff`, `findings_to_entries`, `description_hash`, `normalize_identity_path`, `write_baseline`/`read_baseline` (JSONL), `filter_against_baseline`, `diff_against_baseline` (incl. abs/rel suffix aliasing) | **Golden JSON** vs Python (entries, written bytes, hashes, normalize, filter, diff) |
| `src/fs.rs` (+helpers) | `_fs.py` | `normalize_path`, `validate_file_types`, **`read_text_capped`**, **`atomic_write_text`**, `posix_normpath` | reference table + used by baseline round-trip |
| `src/config.rs` | `orchestrate/config.py` | `is_known_model` + model regex/constants, **`TeamConfig`**, **`default_team_config`** (env fallbacks, range clamping + stderr warnings, preset merge) | Matrix + **golden JSON** (7 scenarios: minimal, presets, clamps, explicit) |
| `src/sarif.rs` | `sarif.py` | **full `findings_to_sarif`** document builder (rule dedup/ordering, `path:line:col:col` parsing, region clamps, control stripping, `%SRCROOT%` URIs, percent-encoding, partial fingerprints), `synthesize_rule_id`, `level_for`, `short_text`, `strip_controls` | **Golden JSON** vs Python (full SARIF doc byte-for-byte + parse/short/strip cases) |
| `src/cost.rs` | `cost.py` | pricing table, token-overhead constants, `family_of`, `PRICING_AS_OF` | Unit tests |
| `src/models.rs` | `verify.py`, `rank.py` | `Finding`, `RankedFile`, `Severity` (+ canonicalization), serde | Serde field-completeness test |
| `src/presets.rs` | `presets.py` + CLI handler | `RulePack`, `list_presets`, `get_preset`, `presets_json`, `presets_pretty` | **Byte-exact golden** vs Python `presets [--json]` |
| `src/verify.rs` | `verify.py` | `Finding` format/parse state machine: `format_findings_block`, `build_verify_prompt`, `parse_findings_from_text`/`_with_drift`, `safe_inline`, severity canonicalization, fenced-block auto-recovery, scope filtering | **Golden JSON** vs Python (round-trip, plain/list, ASCII arrow, fenced, invented severity, header-less, scope, safe_inline) |
| `src/focus.rs` | `focus.py` | `FocusTask`/`FocusBatch`, `create_focus_tasks`, `create_focus_batches`, `format_dispatch_plan`, `format_batch_plan`, prompt templating | **Golden JSON** vs Python (tasks, batches, forced/auto complexity, plans, grammar) |
| `src/rank.rs` | `rank.py` | `language_for_path`, shebang detection, keyword scoring (`finditer` simulation), test-path cap, history boost, `rank_files`, `rank_to_prompt`, `.advisorignore` glob engine, `load_advisorignore` | **Golden JSON** vs Python (language/shebang/score/test/rank/history/ignore — 60+ cases) |
| `src/pr_comment.rs` | `pr_comment.py` | `format_pr_comment` (collapsible `<details>` blocks, severity table, HTML escaping `quote=True`, summary/inline-code escaping, evidence byte-cap + fence/`<details>` neutralization, severity sort, unknown→LOW clamp, body-byte truncation) | **Golden JSON** vs Python (empty, basic, unknown severity, hostile HTML) |
| `src/jsonutil.rs` | (CPython `json.dumps`) | `ensure_ascii` escaping (incl. surrogate pairs) | Unit tests vs CPython |
| `src/version.rs` | `_version.py` | `resolve_version` (crate version) | Unit test |
| `src/main.rs` | `__main__.py` (subset) | `advisor presets [--json]`, `advisor plan [...]`, `advisor baseline create/diff [--from/--output/--baseline/--json]` (incl. JSON+markdown findings input loader), `advisor --version` | Binary diff vs Python (byte-identical, incl. end-to-end `plan` + `baseline` on fixtures) |

### Parity matrix (verified surfaces)

| Surface | Python | Rust | Status | Notes |
|---------|--------|------|--------|-------|
| `advisor presets --json` | ✓ | ✓ | **IDENTICAL** (byte-for-byte, incl. `—` escaping & key order) | `scripts/parity_check.sh` |
| `advisor presets` (NO_COLOR) | ✓ | ✓ | **IDENTICAL** (markdown body + CTA) | golden `tests/parity/presets_plain.txt` |
| `advisor plan --json` (flat/batch/min-priority/preset) | ✓ | ✓ | **IDENTICAL** end-to-end (discovery→rank→focus→JSON) on a fixture tree | `scripts/parity_check.sh` (4 variants) |
| `advisor baseline create` (file bytes) | ✓ | ✓ | **IDENTICAL** written `baseline.jsonl` (JSON findings input) | `scripts/parity_check.sh` |
| `advisor baseline diff --json` | ✓ | ✓ | **IDENTICAL** (new/persisting_count/fixed) | `scripts/parity_check.sh` |
| `advisor suppressions [--json/--expired]` | ✓ | ✓ | **IDENTICAL** (list/json/expired) | `scripts/parity_check.sh` |
| `advisor audit --json` / `--format pr-comment` / `--fail-on` | ✓ | ✓ | **IDENTICAL** end-to-end (checkpoint+transcript) incl. exit-4 gate | `scripts/parity_check.sh` |
| `advisor --version` | n/a (`advisor version`) | ✓ | Intentional difference — Rust uses clap `--version`; full `version` subcommand pending | classified *intentional* |
| `sanitize_inline` / `fence` | ✓ | ✓ | IDENTICAL on tested inputs | |
| `normalize_path` | ✓ | ✓ | IDENTICAL on 10-case reference table | |
| `is_known_model` | ✓ | ✓ | IDENTICAL on 6-case matrix | |
| `synthesize_rule_id` | ✓ | ✓ | IDENTICAL (SHA-1 slug) | |
| `rank_files` / `_score_file` | ✓ | ✓ | IDENTICAL incl. position-ordered reasons, test cap, history boost | golden `tests/parity/rank.json` |
| `.advisorignore` glob match | ✓ | ✓ | IDENTICAL on `*`/`**`/slash/dir/bare/char-class cases | golden |
| `language_for_path` / shebang | ✓ | ✓ | IDENTICAL | golden |

No accidental mismatches found in the ported surface. The single intentional
difference (`--version` vs the `version` subcommand) is recorded above and will
resolve when the `version` subcommand is ported (it emits Python-runtime
fields — `python_version`, `install_path` — that need a defined Rust analog).

## What still remains in Python (not yet ported)

Everything else. The Python package is fully intact and authoritative. Largest
remaining work, roughly in dependency order (see `RUST_PORT_PLAN.md` §6):

- **Core logic**: `runner_budget.py`, `git_scope.py` (subprocess),
  `history.py`, full `cost.py` estimator, remaining `_fs.py` nuances,
  `checkpoint.py` save path (`plan --checkpoint`).
- **Orchestration prompts**: `advisor_prompt.py`, `runner_prompts.py`,
  `verify_dispatch.py`, `pipeline.py`, `_schema.py`, the embedded
  `_prompts/advisor.txt`, and the 20+ golden snapshot fixtures (parity linchpin).
- **CLI**: the remaining ~20 subcommands and all flags/exit-codes in
  `__main__.py`.
- **install/doctor/update**: `install.py` (nudge + skill files, PyPI check),
  `doctor.py`, `skill_asset.py`, `codex_skill.py`.
- **Web dashboard** (`ui` feature): `web/server.py`, `web/assets.py`, `live.py`.

## Known risks / watch-items (carried from the plan)

- **Snapshot parity** is the hardest gate: the orchestration prompt builders
  must reproduce `tests/snapshots/*.txt` byte-for-byte. Plan: reuse those files
  directly as Rust test fixtures.
- **`ensure_ascii` divergence**: serde emits raw UTF-8; CPython escapes. Handled
  by `jsonutil::ensure_ascii`, which every JSON-emitting CLI path must route
  through (already used by `presets_json`).
- **Bespoke glob engine** (`rank`/`suppressions`): must port the custom
  `**`→regex translator + quantifier ReDoS guard, *not* swap in a glob crate.
- **Datetime formatting** variants (history seconds / live milliseconds+`Z` /
  run-id / date-only `until`) must match exactly.
- **Subprocess group-kill + caps** in `git_scope`, and the daemon-thread
  network deadline in `install`, are platform-sensitive — port carefully.
- **stderr warning strings** are sometimes asserted by tests; keep byte-exact.

## Files changed in this slice

- Added: `RUST_PORT_PLAN.md`, `PORT_NOTES.md`, `Cargo.toml`, `src/*.rs`
  (`lib.rs`, `main.rs`, `fence.rs`, `style.rs`, `fs.rs`, `config.rs`,
  `sarif.rs`, `cost.rs`, `models.rs`, `presets.rs`, `jsonutil.rs`, `version.rs`),
  `tests/parity/presets_json.txt`, `tests/parity/presets_plain.txt`,
  `scripts/parity_check.sh`.
- Modified: `.gitignore` (ignore `/target/`).
- **No Python implementation files were modified or deleted.**

## Exact next recommended step

`rank.py`, `focus.py`, `config.py`, `verify.py`, `sarif.py`, and the
**`advisor plan` CLI** are now ported and parity-verified. Next:
1. Port **`cost.py`** estimator + wire `plan --estimate`/`--dump-pricing-template`,
   and **`git_scope.py`** + wire `plan --since/--staged/--branch`.
2. Port **`history.py`** to close the `plan` history-ranking gap, then
   **`checkpoint.py` save** for `plan --checkpoint`/`--resume` and the
   `advisor checkpoints` list command (loader already ported).
3. Remaining `plan` flags not yet wired (documented gaps): `--since/--staged/--branch`
   (needs `git_scope.py`), `--estimate`/`--pricing` (needs full `cost.py`),
   `--checkpoint`/`--resume` (needs `checkpoint.py`), `--sarif`, `--exclude`,
   `--output`, and **history-informed ranking** (needs `history.py` — the Rust
   `plan` currently behaves as `--no-history`). Pretty (non-JSON) `plan` output
   prints the core dispatch/batch plan but not the full colorized framing/tips.
