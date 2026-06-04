# RUST_PORT_PLAN.md — Porting `advisor` (Python → Rust)

> Status: **Phase 1 audit complete.** This document is the authoritative
> migration plan. It is written before any implementation source change so
> that the port preserves behavior, CLI/API compatibility, file formats,
> configuration, logging, error semantics, and runtime behavior.

`advisor` (PyPI: `advisor-agent`, import: `advisor`, CLI: `advisor`) is an
Opus-led code-review-and-fix pipeline for Claude Code. It is **pure-stdlib
Python** (no runtime dependencies), ~18k production lines across ~35 modules,
with a 4,590-line argparse CLI, a stdlib `http.server` dashboard, and 1,086
tests including 20+ byte-exact golden snapshot files.

Crucially, advisor makes **no external API calls** for its core job: it is a
*prompt/plan generator and findings-lifecycle manager*. It ranks files, builds
prompts, parses findings, emits SARIF/PR comments, persists history, and wires
up Claude Code's `~/.claude` skill files. The only network I/O is an optional
PyPI update check and changelog fetch (`advisor update`). This makes it a
strong Rust port candidate: most logic is pure, deterministic string/data
transformation.

---

## 1. Python Architecture Summary

### Entry points
- `pyproject.toml` → `[project.scripts] advisor = "advisor.__main__:main"`.
- `python -m advisor` → `advisor/__main__.py:main(argv)`.
- Library import surface: `advisor/__init__.py` re-exports a large, curated
  `__all__` (the public API; pinned by `tests/test_public_api.py`).

### Module map (production)
| Module | Lines | Responsibility |
|--------|-------|----------------|
| `__main__.py` | 4590 | argparse CLI: ~21 subcommands, dispatch, stdin, JSON modes, exit codes |
| `rank.py` | 1290 | File priority ranking (P1–P5), language detection, `.advisorignore` glob engine |
| `install.py` | 1266 | `~/.claude/CLAUDE.md` nudge + skill install, PyPI update check, changelog parsing |
| `web/assets.py` | 1572 | Embedded HTML/CSS/JS dashboard assets (3 string constants) |
| `web/server.py` | 684 | stdlib `ThreadingHTTPServer` dashboard, JSON API, DNS-rebind defense |
| `orchestrate/runner_prompts.py` | 856 | Runner prompt + dispatch/fix/handoff message builders |
| `audit.py` | 751 | Post-hoc transcript analyser (regex), `AuditReport` |
| `history.py` | 628 | `.advisor/history.jsonl` persistence, repeat scoring (decay), stats |
| `verify.py` | 624 | `Finding` model, findings markdown parse/format, verify prompt |
| `sarif.py` | 543 | SARIF 2.1.0 emitter, path containment, control-char stripping |
| `_style.py` | 472 | ANSI color/glyph/box helpers, `strip_ansi`, markdown colorizer |
| `skill_asset.py` | 471 | Embedded `SKILL.md` / `SKILL.md` update markdown |
| `cost.py` | 451 | Token/USD estimator, pricing model + override loader |
| `doctor.py` | 409 | Setup diagnostics (`DoctorReport`) |
| `suppressions.py` | 398 | `.advisor/suppressions.jsonl` rule gate w/ expiry |
| `live.py` | 384 | `.advisor/live/events.jsonl` event stream (append/tail) |
| `baseline.py` | 375 | Baseline snapshot/diff (`.advisor/baseline.jsonl`) |
| `runner_budget.py` | 366 | Per-runner char/read/fix budget + scope-drift detection |
| `_fs.py` | 350 | Atomic writes, capped reads, path normalization, file-type validation |
| `orchestrate/config.py` | 344 | `TeamConfig`, env fallbacks, model validation, clamping |
| `sarif.py` (script `scripts/sarif_gate.py`) | 143 | CI fail-on gate over a SARIF file |
| `focus.py` | ~250 | `FocusTask`/`FocusBatch` batching from ranked files |
| `checkpoint.py` | ~300 | `.advisor/run-<id>.json` save/resume |
| `presets.py` | ~200 | 7 curated rule-pack presets (pure data) |
| `git_scope.py` | ~250 | `--since/--staged/--branch` via `git` subprocess |
| `pr_comment.py` | ~200 | GitHub PR body markdown (HTML-escaped) |
| `orchestrate/advisor_prompt.py` | ~150 | Advisor prompt template fill (`_prompts/advisor.txt`) |
| `orchestrate/verify_dispatch.py` | ~80 | Verify dispatch prompt/message |
| `orchestrate/_fence.py` | ~120 | Inline sanitization + adaptive code fencing |
| `orchestrate/_schema.py` | ~40 | `FINDING_SCHEMA` markdown constant |
| `orchestrate/pipeline.py` | ~120 | `render_pipeline` human reference |
| `codex_skill.py` | ~200 | Codex variant skill + CSV dispatch prompt |
| `_version.py` | ~60 | Version resolution (local pyproject → installed metadata → `0+unknown`) |

### Runtime flow (CLI)
`main(argv)` → `build_parser()` (argparse with subparsers) → parse →
`--no-color` env handling → optional `--print-completion` → first-run
`ensure_nudge()` (skipped for some subcommands) → `args.func(args)` →
per-subcommand handler returns exit code → `KeyboardInterrupt`→130,
`BrokenPipeError`→0.

### Config loading
`orchestrate/config.py:default_team_config(...)` is the single config
assembler. Precedence: explicit CLI arg → env var (only when the arg is left
at its documented default sentinel) → hardcoded default. Validation clamps
values and warns to **stderr** (never blocks). Optional `preset` merges
preset fields only where the caller left defaults.

### Logging
No `logging` module use in the hot path. Diagnostics are written to **stderr**
via `warnings.warn(...)` (parse tolerance, clamping, staleness) and direct
`print(..., file=sys.stderr)`. User output is stdout. Color via `_style`.

### Networking
Only in `install.py`: `fetch_pypi_latest_version` (GET
`https://pypi.org/pypi/advisor-agent/json`, 1 MiB cap, daemon-thread deadline)
and `fetch_remote_changelog` (GET GitHub raw `CHANGELOG.md`, 512 KiB cap).
Both are best-effort, cached at `~/.claude/.advisor/update-check.json` (24h
TTL), and degrade silently offline. The `web/server.py` dashboard binds
loopback only.

### Data models (dataclasses, all `frozen=True, slots=True`)
`RankedFile`, `FocusTask`, `FocusBatch`, `Finding`, `HistoryEntry`,
`BaselineEntry`, `BaselineDiff`, `Checkpoint`, `Suppression`, `CostEstimate`,
`RulePack`, `RunnerBudget`, `ScopeAnchor`, `TeamConfig`, `AuditReport`,
`DoctorReport`/`Check`, `InstallResult`/`ComponentStatus`/`Status`.

### Background tasks / daemon
No daemon. `web/server.py` runs a synchronous `ThreadingHTTPServer` until
Ctrl-C. `live.py` is an append-only event log polled by the dashboard.

### File/database usage
No database. All persistence is files under the target's `.advisor/`:
- `history.jsonl` (JSONL), `baseline.jsonl` (JSONL w/ header), 
  `suppressions.jsonl` (JSONL w/ header), `run-<id>.json` (JSON),
  `live/events.jsonl` (JSONL). Plus `~/.claude/...` install artifacts and
  `~/.claude/.advisor/update-check.json` cache.

### Tests
1,086 functions across 34 files; 20+ golden snapshot files in
`tests/snapshots/`; hypothesis property tests. See §8.

### Packaging/deployment
hatchling wheel; force-includes `orchestrate/_prompts/advisor.txt`,
`py.typed`, and `CHANGELOG.md`→`advisor/_changelog.md`. Published to PyPI via
tag-triggered `release.yml`; `autotag.yml` tags on `pyproject.toml` version
bump.

---

## 2. Public Compatibility Map (MUST be preserved)

### CLI subcommands (21)
`pipeline`, `plan`, `codex-plan-csv`, `prompt`, `status`, `install`,
`uninstall`, `protocol`, `version`, `doctor`, `changelog`, `update`, `ui`,
`history`, `history-append`, `live` (record/tail/clear), `checkpoints`,
`baseline` (create/diff), `suppressions`, `presets`, `audit`.

Common flags on planning subcommands: `--team` (default `review`),
`--file-types` (`*.py`), `--max-runners` (5, env `ADVISOR_MAX_RUNNERS`,
ceiling `POOL_SIZE_CEILING=20`), `--min-priority` (3, choices 1–5),
`--context`/`--context -`, `--advisor-model` (`claude-opus-4-7`),
`--runner-model` (`claude-sonnet-4-6`), `--max-fixes-per-runner`,
`--large-file-line-threshold` (800), `--large-file-max-fixes` (3),
`--runner-output-char-ceiling` (80000), `--runner-file-read-ceiling` (20).

`plan`-specific: `--batch-size`, `--format {pretty,json}`, `--json`,
`--output`, `--estimate`, `--test-cmd`, `--since`, `--staged`, `--branch`,
`--checkpoint`, `--resume`, `--exclude` (repeatable), `--pricing`,
`--dump-pricing-template`, `--sarif`, `--no-history`, `--preset`.

`audit`-specific: `--transcript` (default stdin), `--sarif`,
`--fail-on {never,low,medium,high,critical}`,
`--format {pretty,json,pr-comment}`, `--baseline`.

Global: `--version`, `--no-color`, `--print-completion {bash,zsh,tcsh}`.

(See the audit transcript / §3 mapping; the full per-subcommand flag list is
the source of truth and is pinned by `tests/test_main.py`, 133 functions.)

### Exit codes
| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Generic error (IO, not found, install/update failure, changelog missing) |
| 2 | User input error (bad path/glob/flag, stdin cap exceeded) |
| 3 | Strict no-op (`--strict`/`--check`: nothing changed / unhealthy) |
| 4 | `--fail-on` threshold met |
| 130 | KeyboardInterrupt |
| (BrokenPipe → 0) | |

`scripts/sarif_gate.py`: 0 (pass), 2 (unreadable SARIF), 4 (gate tripped).

### Environment variables
`ADVISOR_MODEL`, `ADVISOR_RUNNER_MODEL`, `ADVISOR_MAX_RUNNERS`,
`ADVISOR_FILE_TYPES`, `ADVISOR_MIN_PRIORITY`, `ADVISOR_TEST_COMMAND`,
`ADVISOR_RUNNER_OUTPUT_CHAR_CEILING`, `ADVISOR_RUNNER_FILE_READ_CEILING`,
`ADVISOR_QUIET`, `ADVISOR_NO_NUDGE` (opt-out), `ADVISOR_FAIL_ON` (gate script),
`ADVISOR_UPDATE_SNAPSHOTS` (test only). Color: `NO_COLOR`, `CLICOLOR`,
`CLICOLOR_FORCE`, `TERM`.

### Config files & on-disk formats (byte-compatible)
- `.advisorignore` — gitignore-like globs (`#` comments, `**`, trailing `/`
  dirs); 1 MiB cap; negation (`!`) and root-anchor warned + dropped.
- `.advisor/history.jsonl` — `{timestamp, file_path, severity, description,
  status, run_id, schema_version}`; `HISTORY_SCHEMA_VERSION="1.0"`.
- `.advisor/baseline.jsonl` — header line `{"__advisor_baseline__":true,
  "schema_version":"1.0","count":N}` + entries `{file_path, rule_id,
  description_hash, severity, description}`, sorted by identity key.
- `.advisor/suppressions.jsonl` — header `{"__advisor_suppressions__":true,
  "schema_version":"1.0"}` + entries `{rule_id, reason, file?|file_glob?,
  until?}`.
- `.advisor/run-<id>.json` — full `Checkpoint` JSON (`indent=2`);
  `CHECKPOINT_SCHEMA_VERSION="1.0"`; run_id regex
  `^[A-Za-z0-9][A-Za-z0-9_.-]*$`, ≤128 chars.
- `.advisor/live/events.jsonl` — `{schema_version, ts, seq, kind, data}`;
  `LIVE_SCHEMA_VERSION="1.0"`.
- Install: `~/.claude/CLAUDE.md` (nudge between
  `<!-- advisor:nudge:start -->`/`<!-- advisor:nudge:end -->`),
  `~/.claude/skills/advisor/SKILL.md`,
  `~/.claude/skills/advisor-update/SKILL.md`,
  `~/.agents/skills/advisor/SKILL.md` (Codex),
  `~/.claude/.advisor/update-check.json`.

### API endpoints (`advisor ui`, loopback `127.0.0.1:8765`)
`GET /`, `/index.html`, `/static/app.css`, `/static/app.js`,
`/api/target`, `/api/status`, `/api/history?limit=`, `/api/plan?file_types=&min_priority=`,
`/api/cost?...`, `/api/events?since=&limit=`. Host-header allowlist
(`127.0.0.1`, `localhost`, `::1`); CSP + `nosniff`; 400/403/404/500. JSON
shapes documented in §2 of the audit (pinned by `tests/test_web.py`).

### stdout/stderr behavior
User output → stdout. Warnings/diagnostics → stderr. JSON payloads carry
`schema_version` (top-level `JSON_SCHEMA_VERSION="1.0"`; modules add their own).

### Output stability
Snapshot tests pin exact prompt/message text byte-for-byte. SARIF results are
sorted; baseline written sorted by identity for byte-identical diffs.

---

## 3. Python → Rust Design

### Crate structure
A **single Cargo package at the repo root** (`advisor-rs`) with `src/lib.rs`
(all core logic, mirrors `advisor/` modules) and `src/main.rs` (the `advisor`
binary). Rationale: one binary + one library; a workspace adds ceremony with
no payoff today. The optional web dashboard is gated behind a `ui` cargo
feature so the base binary stays dependency-light, mirroring Python's `[ui]`
extra. If the dashboard later needs a heavy async stack, promote to a
workspace member then.

```
Cargo.toml                 # package "advisor-rs", bin name "advisor"
src/
  lib.rs                   # pub mod re-exports (mirrors advisor/__init__.py __all__)
  fs.rs                    # _fs.py: atomic_write, read_capped, normalize_path, validate_file_types
  style.rs                 # _style.py: strip_ansi, paint, glyph, boxes, colorize_markdown
  version.rs               # _version.py
  fence.rs                 # orchestrate/_fence.py: sanitize_inline, fence
  rank.rs                  # rank.py: ranking + .advisorignore glob engine
  focus.rs                 # focus.py
  verify.rs                # verify.py: Finding + parse/format
  presets.rs               # presets.py (pure data)
  cost.rs                  # cost.py
  runner_budget.rs         # runner_budget.py
  sarif.rs                 # sarif.py
  git_scope.rs             # git_scope.py (std::process::Command)
  history.rs               # history.py
  baseline.rs              # baseline.py
  checkpoint.rs            # checkpoint.py
  suppressions.rs          # suppressions.py
  pr_comment.rs            # pr_comment.py
  audit.rs                 # audit.py
  doctor.rs                # doctor.py
  install.rs               # install.py (+ skill_asset, codex_skill content)
  live.rs                  # live.py
  orchestrate/
    mod.rs                 # config + prompt builders + pipeline + verify_dispatch + schema
    config.rs
    advisor_prompt.rs
    runner_prompts.rs
    verify_dispatch.rs
    pipeline.rs
  cli/                     # __main__.py
    mod.rs                 # clap command tree + dispatch
    <subcommand handlers>
  web/                     # behind feature "ui"
    mod.rs
    server.rs
    assets.rs              # include_str!-ed assets
prompts/advisor.txt        # moved/copied; embedded via include_str!
tests/                     # Rust integration tests (assert_cmd) + parity goldens
```

Python files stay in place and untouched during the port (no deletions until
parity is proven — §10).

### Binary names
`advisor` (unchanged). `python -m advisor` has no Rust analog; documented.

### Data model mapping
Each `@dataclass(frozen, slots)` → a Rust `struct` with `#[derive(Debug,
Clone, PartialEq, Serialize, Deserialize)]`. Field names preserved exactly;
`serde` field renames only where the JSON key differs from an idiomatic Rust
name (e.g. `schema_version` stays snake_case — already matches). Enums with
fixed string sets (`InstallAction`, severity, status, scope stage) → Rust
`enum` with `#[serde(rename_all=...)]` or explicit `rename` to preserve the
exact JSON strings (`CONFIRMED`, `error`, etc.). Optional fields → `Option<T>`
with `#[serde(default, skip_serializing_if = ...)]` chosen to match Python's
emitted JSON (Python emits all dataclass fields via `asdict`, so **do not
skip** — emit every field, including defaults, to keep byte parity).

### Error strategy
- Library: one error enum **per module** with `thiserror`, preserving the
  Python exception taxonomy where public behavior depends on it
  (`GitScopeError`, `ValueError`-equivalents for checkpoint/cost/suppressions,
  `FileNotFoundError`-equivalent). A crate-level `AdvisorError` aggregates via
  `#[from]`.
- Binary: `anyhow` only at the `cli` boundary, mapped to the exact **exit
  codes** (§2) — never let a panic escape. A small `ExitCode`-returning
  dispatcher converts errors to (code, stderr message).
- Warnings: Python's `warnings.warn`/stderr prints → a tiny `warn!`-style
  helper writing to stderr (not `tracing` for these user-facing diagnostics,
  to keep exact text). `tracing` is reserved for the optional `--verbose`
  dashboard request log.

### Async/runtime strategy
The core is **fully synchronous** — no async needed. Network (update check)
uses a blocking HTTP client with a hard deadline thread, mirroring Python.
The dashboard is a synchronous threaded server (`std::net::TcpListener` +
a small thread pool, or `tiny_http`), matching Python's `ThreadingHTTPServer`;
**no tokio**. (Re-evaluate only if the dashboard grows.)

### Serialization strategy
`serde` + `serde_json`. JSONL = line-by-line `serde_json::to_string` /
`from_str`. Preserve `ensure_ascii=False` semantics → serde already emits
UTF-8 (no `\uXXXX` escaping by default), matching Python's non-ASCII output.
Checkpoint uses `serde_json::to_string_pretty` (2-space indent) to match
`json.dumps(indent=2)`. **Key ordering**: Python `asdict` preserves field
declaration order; serde structs serialize in declaration order — keep struct
field order identical to the dataclass to match byte output.

### Config strategy
Port `default_team_config` literally: a builder that reads `Option<T>` args,
falls back to env, then defaults; clamps with stderr warnings using the exact
warning strings. No `config`/`figment` crate — the precedence logic is bespoke
and must match exactly.

### Logging/tracing strategy
User diagnostics → stderr with byte-exact strings (no log framework).
`tracing`/`tracing-subscriber` only behind the `ui --verbose` path.

### Testing strategy
Port unit tests per module (Rust `#[cfg(test)]`). Reuse the **existing golden
snapshot files** verbatim as parity fixtures (the Rust prompt builders must
reproduce them byte-for-byte). CLI integration via `assert_cmd` + `predicates`
+ `tempfile`. Property tests via `proptest` (mirror the 7 hypothesis
invariants: PR-comment HTML safety, findings round-trip, glob never-panics).
A **cross-checker**: run Python and Rust on identical inputs and diff stdout/
exit code (script in `scripts/parity_check.sh`).

---

## 4. Dependency Mapping

| Python (stdlib) usage | Rust equivalent | Direct / custom |
|-----------------------|-----------------|-----------------|
| `argparse` | `clap` (derive) | Direct (subcommand tree, exact flags/defaults) |
| `json` | `serde` + `serde_json` | Direct |
| `re` | `regex` | Direct; pre-compile with `once_cell::Lazy` |
| `fnmatch` / glob translation | `regex` (port the `**`→regex translation) + custom | Custom (must match advisor's bespoke translator, not the `glob` crate) |
| `dataclasses` | `struct` + `#[derive(Serialize,Deserialize)]` | Direct |
| `pathlib.Path` | `std::path::{Path,PathBuf}` | Direct |
| `posixpath.normpath` | custom lexical normalizer | Custom (POSIX semantics, platform-independent — must match exactly) |
| `subprocess` (git) | `std::process::Command` | Direct (+ timeout via wait-thread / `wait-timeout` crate) |
| `urllib.request` (PyPI/changelog) | `ureq` (blocking, tiny) | Direct (size cap + deadline) |
| `http.server` (dashboard) | `tiny_http` (feature `ui`) | Direct (sync threaded) |
| `hashlib.sha1` | `sha1` crate | Direct (rule-id + description hash) |
| `datetime`/`timezone.utc` | `chrono` (UTC) or `time` | Direct — **must match ISO-8601 formats** incl. `timespec=seconds`/`milliseconds` and `Z` normalization |
| `secrets`/random run-id | `rand` (8 hex) | Direct |
| `fcntl`/`msvcrt` file locks | `fs2` crate (`FileExt::lock_exclusive`) | Direct (best-effort, tolerate `ENOLCK`/`ENOSYS`) |
| `tempfile.mkstemp` + `os.replace` + `fsync` | `tempfile` crate + `fs::rename` + `File::sync_all` | Direct (atomic write) |
| `importlib.resources` (advisor.txt) | `include_str!` | Direct (compile-time embed) |
| `importlib.metadata.version` | `env!("CARGO_PKG_VERSION")` + pyproject read | Custom (preserve `_version.py` precedence) |
| `html.escape` | `v_htmlescape` or hand-rolled | Custom (match `quote=True` exactly) |
| `unicodedata` East-Asian width (`_style`) | `unicode-width` crate | Direct |
| `shtab` completions | `clap_complete` | Direct (bash/zsh; tcsh: document gap) |
| `warnings`/`fcntl` errno checks | `std::io::Error` kind matching | Direct |

New runtime deps (all justified): `clap`, `serde`, `serde_json`, `regex`,
`once_cell`, `chrono`, `sha1`, `rand`, `unicode-width`, `tempfile`, `fs2`,
`thiserror`, `anyhow`. Network: `ureq` (only for `update`). UI feature:
`tiny_http`, `clap_complete`. Dev: `assert_cmd`, `predicates`, `proptest`.

---

## 5. Risk Register

| Risk | Detail | Mitigation |
|------|--------|------------|
| **Byte-exact prompt parity** | 20+ golden snapshots pin orchestration text; any whitespace/placeholder drift breaks consumers | Embed `advisor.txt` verbatim; port substitution as single-pass; assert against the *existing* snapshot files in Rust tests |
| **Bespoke glob engine** | `rank`/`suppressions` use a custom `**`→regex translator + `fnmatch` fallback + ReDoS quantifier guard (`_MAX_GLOB_QUANTIFIERS=8`, 4 on <3.12) | Port translator literally; **do not** swap in the `glob` crate; replicate quantifier cap + inert `$.^` fallback (proptest: never panics) |
| **Path normalization** | `_fs.normalize_path` strips BOM/backticks/`\`→`/`, collapses `..`/`.`, strips trailing `:line[:col]` (≤2 iters), keeps drive letters; `baseline._normalize_identity_path` differs (no `:line` strip) | Two distinct functions; port with exact example table from audit as tests |
| **Datetime formatting** | ISO-8601 variants: history `isoformat(timespec="seconds")` w/ `+00:00`; live `milliseconds` normalized to `Z`; run-id `YYYYMMDDTHHMMSSZ-XXXXXXXX`; suppression `until` is date-only `YYYY-MM-DD` and rejects datetime-shaped strings | Use `chrono` with explicit format strings; unit-test each format; reject datetime-shaped `until` |
| **Decay / scoring math** | `file_repeat_scores` exponential decay (half-life 30d, cap 10.0), severity weights `{CRIT 4.0, HIGH 2.5, MED 1.5, LOW 1.0}`; history boost threshold 1.5 → +1 tier | Port f64 math; tolerance-free integer tier results; test boundary at threshold |
| **Subprocess timeout/kill** | `git_scope` uses `start_new_session=True`, SIGKILL process group on 30s timeout, 50 MiB stdout / 1 MiB stderr caps; ref allowlist regex; rejects `-`/`..` | Spawn in new process group (`process_group(0)` on unix), kill group; cap reads; port `_REF_ALLOWED` regex exactly |
| **Network update check** | daemon-thread total deadline (not just socket timeout), 1 MiB cap, PEP 440 charset validation, stale-cache fallback, non-finite timestamp guard | `ureq` with `.timeout()` + a wrapping deadline thread; replicate cache schema + guards |
| **File locking portability** | advisory `flock`/`msvcrt`; tolerates `ENOLCK/ENOSYS/EOPNOTSUPP/ENOTSUP` (NFS), one-shot warning | `fs2` best-effort; match the "continue unlocked + warn once" behavior |
| **Unicode safety invariants** | fence/sanitize drop 18 zero-width/bidi code points + map 8 linebreaks to space; SARIF/PR-comment strip C0 + bidi; proptest-backed | Port the exact code-point lists; reuse hypothesis invariants as proptest |
| **HTML escaping parity** | `pr_comment` uses `html.escape(quote=True)` + pipe escaping + backtick→quote; GitHub 60 KB body cap, 500 B evidence cap | Match `html.escape` mapping (`& < > " '`) exactly; byte-count truncation in UTF-8 |
| **Concurrency in dashboard** | `ThreadingHTTPServer`, exclusive lock on append path only, `_response_committed` double-send guard | Sync threaded server; same lock scope; document any deviation |
| **stderr warning text** | clamping/staleness/parse warnings are user-visible and some tests assert on them | Keep warning strings byte-identical; centralize in a `warn()` helper |
| **`asdict` field ordering & completeness** | Python emits every field in declaration order | Match struct field order; do not `skip_serializing` defaults |
| **Performance paths** | `rank` reads first `CONTENT_SCAN_LIMIT=1024` bytes/file, thread-pooled; large repos | Rust `rayon` optional; default bounded threads; stream reads (don't slurp) |
| **`tcsh` completion** | `shtab` supports tcsh; `clap_complete` does not | Document gap; still support bash/zsh; keep flag, error clearly on tcsh |

---

## 6. Migration Milestones

1. **Smallest build** — `cargo build` green: `advisor --version`,
   `advisor --help` skeleton (clap tree present, handlers may return
   "not yet ported" with a distinct sentinel — but **only** in the binary
   shell, never in library logic; remove as each is ported).
2. **Smallest run** — `advisor presets`, `advisor version`,
   `advisor protocol` produce real, parity-checked output.
3. **First real test** — port `fence`, `_style.strip_ansi`, `presets`,
   `_fs.normalize_path` + unit tests passing; assert against Python output.
4. **Data models + config** — `Finding`, `RankedFile`, `FocusTask/Batch`,
   `TeamConfig` + `default_team_config` env/clamp logic, with serde round-trip
   and the audit's example tables as tests.
5. **Core logic** — `rank` (+ glob engine), `verify` parse/format,
   `cost`, `runner_budget`, `sarif`, `git_scope`, `history`, `baseline`,
   `checkpoint`, `suppressions`, `pr_comment`, `audit`. Each lands with tests
   and a parity check vs Python.
6. **Orchestration prompts** — port builders; **assert against the existing
   golden snapshot files** (byte-exact). This is the parity linchpin.
7. **CLI** — clap tree matching every subcommand/flag/default/exit-code;
   `assert_cmd` integration tests; `scripts/parity_check.sh` diffs Python vs
   Rust across a fixture matrix.
8. **install/doctor/update/web** — filesystem + network + dashboard, behind
   features where appropriate.
9. **Parity audit** — populate `PORT_NOTES.md` parity matrix; classify and
   fix every mismatch.
10. **Cutover** — update README/CI/packaging; remove Python only after Rust
    parity is proven and the team agrees. Until then, both ship.

### Parity test strategy
- Reuse `tests/snapshots/*.txt` directly in Rust tests.
- `scripts/parity_check.sh`: for each subcommand+fixture, run
  `python -m advisor ...` and `./target/release/advisor ...`, diff stdout and
  compare exit codes; fail on any difference not in an allowlist documented in
  `PORT_NOTES.md`.
- JSON outputs compared structurally (parse both) **and** byte-wise where
  Python output stability is contractually pinned.

### Final cutover plan
Ship Rust binary alongside Python; gate removal of `advisor/*.py` on: (a) all
ported subcommands parity-green, (b) snapshot tests green, (c) CI matrix green
on Rust, (d) maintainer sign-off. Keep `RUST_PORT_PLAN.md` + `PORT_NOTES.md`
as the migration record.

---

## Appendix A — Exact constants to preserve (selected)

- `POOL_SIZE_CEILING = 20`; `DEFAULT_ADVISOR_MODEL = "claude-opus-4-7"`;
  `DEFAULT_RUNNER_MODEL = "claude-sonnet-4-6"`;
  `KNOWN_MODEL_SHORTCUTS = {opus, sonnet, haiku}`;
  model regex `^(?:Codex|claude)-(opus|sonnet|haiku)-\d+(?:[.-]\d+){0,3}(?:-\d{8})?$`.
- `CONTENT_SCAN_LIMIT = 1024`; `.advisorignore` cap `1048576`;
  glob quantifier cap 8 (4 on <3.12); history boost threshold `1.5`.
- Severity weights `{CRITICAL 4.0, HIGH 2.5, MEDIUM 1.5, LOW 1.0}`; file score
  cap `10.0`; half-life `30d`; window `90d`.
- SARIF: `SARIF_VERSION="2.1.0"`,
  `SARIF_SCHEMA_URI="https://json.schemastore.org/sarif-2.1.0.json"`,
  level map `{CRITICAL/HIGH→error, MEDIUM→warning, LOW→note}`, default
  `warning`, int32 max `2147483647`.
- Cost: `PRICING_AS_OF=2026-05-22`, cents/Mtok `opus (1500,7500)`,
  `sonnet (300,1500)`, `haiku (25,125)`; `ADVISOR_SYSTEM_TOKENS=4500`,
  `RUNNER_SYSTEM_TOKENS=2000`, `PER_MESSAGE_OVERHEAD_TOKENS=300`,
  `CHARS_PER_TOKEN=3.5`; stale after 180d.
- Budget: `DEFAULT_CHAR_CEILING=80000`, `DEFAULT_FILE_READ_CEILING=20`,
  `SOFT_WARN_FRACTION=0.60`, `ROTATE_FRACTION=0.80`,
  `SCOPE_STAGES=(reading,hypothesizing,confirming,fixing,done)`.
- Schema versions all `"1.0"`; top-level `JSON_SCHEMA_VERSION="1.0"`.
- Install markers `<!-- advisor:nudge:start -->` / `...:end -->`; badge regex
  `<!--\s*advisor:([^\s>]+)\s*-->`; caps PyPI/ CLAUDE.md 1 MiB, skill 256 KiB,
  changelog 512 KiB; update cache TTL 24h.
- Dashboard `127.0.0.1:8765`; host allowlist `{127.0.0.1, localhost, ::1}`.

These are reproduced in code as named constants with the same identifiers.
