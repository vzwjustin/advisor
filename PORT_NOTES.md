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
| `src/config.rs` | `orchestrate/config.py` | `is_known_model`, model regex, `POOL_SIZE_CEILING`, default model ids, `KNOWN_MODEL_SHORTCUTS` | Matrix vs Python |
| `src/sarif.rs` | `sarif.py` | `synthesize_rule_id`, `level_for`, schema/version constants | SHA-1 rule-id vs Python |
| `src/cost.rs` | `cost.py` | pricing table, token-overhead constants, `family_of`, `PRICING_AS_OF` | Unit tests |
| `src/models.rs` | `verify.py`, `rank.py` | `Finding`, `RankedFile`, `Severity` (+ canonicalization), serde | Serde field-completeness test |
| `src/presets.rs` | `presets.py` + CLI handler | `RulePack`, `list_presets`, `get_preset`, `presets_json`, `presets_pretty` | **Byte-exact golden** vs Python `presets [--json]` |
| `src/jsonutil.rs` | (CPython `json.dumps`) | `ensure_ascii` escaping (incl. surrogate pairs) | Unit tests vs CPython |
| `src/version.rs` | `_version.py` | `resolve_version` (crate version) | Unit test |
| `src/main.rs` | `__main__.py` (subset) | `advisor presets [--json]`, `advisor --version` | Binary diff vs Python (byte-identical) |

### Parity matrix (verified surfaces)

| Surface | Python | Rust | Status | Notes |
|---------|--------|------|--------|-------|
| `advisor presets --json` | ✓ | ✓ | **IDENTICAL** (byte-for-byte, incl. `—` escaping & key order) | `scripts/parity_check.sh` |
| `advisor presets` (NO_COLOR) | ✓ | ✓ | **IDENTICAL** (markdown body + CTA) | golden `tests/parity/presets_plain.txt` |
| `advisor --version` | n/a (`advisor version`) | ✓ | Intentional difference — Rust uses clap `--version`; full `version` subcommand pending | classified *intentional* |
| `sanitize_inline` / `fence` | ✓ | ✓ | IDENTICAL on tested inputs | |
| `normalize_path` | ✓ | ✓ | IDENTICAL on 10-case reference table | |
| `is_known_model` | ✓ | ✓ | IDENTICAL on 6-case matrix | |
| `synthesize_rule_id` | ✓ | ✓ | IDENTICAL (SHA-1 slug) | |

No accidental mismatches found in the ported surface. The single intentional
difference (`--version` vs the `version` subcommand) is recorded above and will
resolve when the `version` subcommand is ported (it emits Python-runtime
fields — `python_version`, `install_path` — that need a defined Rust analog).

## What still remains in Python (not yet ported)

Everything else. The Python package is fully intact and authoritative. Largest
remaining work, roughly in dependency order (see `RUST_PORT_PLAN.md` §6):

- **Core logic**: `rank.py` (ranking + bespoke `.advisorignore`/glob engine),
  `verify.py` parse/format state machine, `focus.py`, `runner_budget.py`,
  `git_scope.py` (subprocess), `history.py`, `baseline.py`, `checkpoint.py`,
  `suppressions.py`, `pr_comment.py`, `audit.py`, full `cost.py` estimator,
  full `sarif.py` document builder, full `_fs.py` atomic write/locking.
- **Config**: `TeamConfig` struct + `default_team_config` env/clamp assembler.
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

Port **`rank.py`** next — it is the most-depended-on core module (drives
`plan`, `focus`, `cost`, the web `/api/plan`) and is pure/deterministic, so it
is highly parity-verifiable. Concretely:
1. Port `PRIORITY_KEYWORDS`, `LANGUAGE_EXTRA_KEYWORDS`, `EXTENSION_LANGUAGE`,
   `SKIP_DIRS`, `SKIP_EXTENSIONS`, and the constants in Appendix A of the plan.
2. Port `language_for_path`, the keyword-scoring `_score_file`, `rank_files`,
   `rank_to_prompt`, and the `.advisorignore` glob engine
   (`_double_star_to_regex` + quantifier guard).
3. Capture a Python golden for `advisor plan <fixture> --json --no-history` and
   add it to `scripts/parity_check.sh` as the second cross-language check.
4. Then wire `advisor plan` into the Rust CLI behind the verified ranker.
