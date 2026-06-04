#!/usr/bin/env bash
# Cross-language parity check: run the Python reference CLI and the Rust port
# on identical inputs and diff stdout byte-for-byte. Exits non-zero on any
# mismatch. Extend with one block per ported subcommand as the port grows
# (see PORT_NOTES.md "Exact next recommended step").
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

RUST_BIN="${RUST_BIN:-$ROOT/target/release/advisor}"
PY="${PY:-python3 -m advisor}"

if [[ ! -x "$RUST_BIN" ]]; then
  echo "building release binary..." >&2
  cargo build --release >/dev/null
fi

fail=0

check() {
  local name="$1"; shift
  # Remaining args are the CLI args passed identically to both.
  local py_out rs_out
  py_out="$(NO_COLOR=1 $PY "$@" 2>/dev/null || true)"
  rs_out="$(NO_COLOR=1 "$RUST_BIN" "$@" 2>/dev/null || true)"
  if [[ "$py_out" == "$rs_out" ]]; then
    echo "PASS  $name"
  else
    echo "FAIL  $name"
    diff <(printf '%s' "$py_out") <(printf '%s' "$rs_out") | sed 's/^/    /' || true
    fail=1
  fi
}

check "presets"        presets
check "presets --json" presets --json

# ── plan: build a small fixture tree and diff `plan --json` end-to-end ──
plan_check() {
  local name="$1"; shift
  local fix py_out rs_out
  fix="$(mktemp -d)"
  mkdir -p "$fix/src" "$fix/api" "$fix/tests" "$fix/node_modules"
  printf 'def login(password, token):\n    pass\n' > "$fix/src/auth.py"
  printf 'def helper():\n    return 1\n'           > "$fix/src/util.py"
  printf '@route\ndef handler():\n    query(sql)\n' > "$fix/api/routes.py"
  printf "password='hunter2'\n"                     > "$fix/tests/test_auth.py"
  printf 'auth\n'                                   > "$fix/node_modules/skip.py"
  py_out="$(cd "$fix" && NO_COLOR=1 $PY plan . "$@" 2>/dev/null || true)"
  rs_out="$(cd "$fix" && NO_COLOR=1 "$RUST_BIN" plan . "$@" 2>/dev/null || true)"
  if [[ "$py_out" == "$rs_out" ]]; then
    echo "PASS  $name"
  else
    echo "FAIL  $name"
    diff <(printf '%s' "$py_out") <(printf '%s' "$rs_out") | sed 's/^/    /' || true
    fail=1
  fi
  rm -rf "$fix"
}

plan_check "plan --json"                 --json --no-history
plan_check "plan --json --batch-size 2"  --json --no-history --batch-size 2
plan_check "plan --json --min-priority 1" --json --no-history --min-priority 1
plan_check "plan --json --preset python-web" --json --no-history --preset python-web
plan_check "plan --json --estimate"      --json --no-history --estimate
plan_check "plan --json --estimate --batch-size 1" --json --no-history --estimate --batch-size 1

check "plan --dump-pricing-template" plan --dump-pricing-template

# ── baseline: create (file bytes) + diff --json, JSON and markdown inputs ──
baseline_check() {
  local tmp py rs
  tmp="$(mktemp -d)"
  cat > "$tmp/f.json" <<'JSON'
[
  {"file_path":"src/auth.py:42","severity":"high","description":"SQL injection in login","evidence":"concat","fix":"params"},
  {"file_path":"lib/x.py","severity":"LOW","description":"weak md5","rule_id":"advisor/custom/1"}
]
JSON
  printf '[CRITICAL] src/auth.py:9 — hardcoded secret\n' > "$tmp/f.md"
  mkdir -p "$tmp/py" "$tmp/rs"
  $PY baseline create "$tmp/py" --from "$tmp/f.json" --quiet 2>/dev/null
  "$RUST_BIN" baseline create "$tmp/rs" --from "$tmp/f.json" --quiet 2>/dev/null
  if diff -q "$tmp/py/.advisor/baseline.jsonl" "$tmp/rs/.advisor/baseline.jsonl" >/dev/null; then
    echo "PASS  baseline create (file bytes)"
  else
    echo "FAIL  baseline create"; fail=1
  fi
  cat > "$tmp/f2.json" <<'JSON'
[
  {"file_path":"src/auth.py:42","severity":"high","description":"SQL injection in login","evidence":"concat","fix":"params"},
  {"file_path":"brand/new.py:1","severity":"MEDIUM","description":"new issue"}
]
JSON
  py="$($PY baseline diff "$tmp/py" --from "$tmp/f2.json" --json 2>/dev/null || true)"
  rs="$("$RUST_BIN" baseline diff "$tmp/rs" --from "$tmp/f2.json" --json 2>/dev/null || true)"
  if [[ "$py" == "$rs" ]]; then echo "PASS  baseline diff --json"; else echo "FAIL  baseline diff --json"; diff <(printf '%s' "$py") <(printf '%s' "$rs") | sed 's/^/    /'; fail=1; fi
  rm -rf "$tmp"
}
baseline_check

# ── suppressions: list / --json / --expired ──
suppressions_check() {
  local tmp py rs
  tmp="$(mktemp -d)"; mkdir -p "$tmp/.advisor"
  cat > "$tmp/.advisor/suppressions.jsonl" <<'JSONL'
{"__advisor_suppressions__": true, "schema_version": "1.0"}
{"rule_id": "advisor/low/a", "file": "src/auth.py", "reason": "ok"}
{"rule_id": "advisor/high/b", "file_glob": "legacy/**", "reason": "rewrite", "until": "2999-01-01"}
{"rule_id": "advisor/critical/c", "file": "x.py", "reason": "old", "until": "2000-01-01"}
JSONL
  local name args
  for spec in "suppressions --json::--json" "suppressions::" "suppressions --expired --json::--expired --json"; do
    name="${spec%%::*}"; args="${spec##*::}"
    py="$(cd "$tmp" && NO_COLOR=1 $PY suppressions . $args 2>/dev/null || true)"
    rs="$(cd "$tmp" && NO_COLOR=1 "$RUST_BIN" suppressions . $args 2>/dev/null || true)"
    if [[ "$py" == "$rs" ]]; then echo "PASS  $name"; else echo "FAIL  $name"; diff <(printf '%s' "$py") <(printf '%s' "$rs") | sed 's/^/    /'; fail=1; fi
  done
  rm -rf "$tmp"
}
suppressions_check

# ── audit: --json + --format pr-comment + --fail-on exit code ──
audit_check() {
  local tmp; tmp="$(mktemp -d)"; mkdir -p "$tmp/.advisor"
  cat > "$tmp/.advisor/run-r1.json" <<'JSON'
{"run_id":"r1","created_at":"2026-06-04T00:00:00+00:00","target":"/repo","team_name":"review","file_types":"*.py","min_priority":3,"max_runners":5,"advisor_model":"claude-opus-4-7","runner_model":"claude-sonnet-4-6","max_fixes_per_runner":2,"large_file_line_threshold":800,"large_file_max_fixes":3,"test_command":"","context":"","tasks":[{"file_path":"src/auth.py","priority":5,"prompt":"p"}],"batches":[{"batch_id":1,"complexity":"high","top_priority":5,"tasks":[{"file_path":"src/auth.py","priority":5}]}],"schema_version":"1.0"}
JSON
  cat > "$tmp/t.txt" <<'TXT'
SendMessage(to='runner-2', message='d')
## Fix assignment (fix 3 of 2)
runner-2 CONTEXT_PRESSURE
## Handoff from runner-2

### Finding 1
- **File**: `src/auth.py:10`
- **Severity**: HIGH
- **Description**: in-batch issue
- **Evidence**: ev
- **Fix**: fx

### Finding 2
- **File**: `other/drift.py:5`
- **Severity**: LOW
- **Description**: out of batch
- **Evidence**: e2
- **Fix**: f2
TXT
  local py rs name args
  for spec in "audit --json::--json" "audit pr-comment::--format pr-comment"; do
    name="${spec%%::*}"; args="${spec##*::}"
    py="$(NO_COLOR=1 $PY audit r1 "$tmp" --transcript "$tmp/t.txt" $args 2>/dev/null || true)"
    rs="$(NO_COLOR=1 "$RUST_BIN" audit r1 "$tmp" --transcript "$tmp/t.txt" $args 2>/dev/null || true)"
    if [[ "$py" == "$rs" ]]; then echo "PASS  $name"; else echo "FAIL  $name"; diff <(printf '%s' "$py") <(printf '%s' "$rs") | sed 's/^/    /'; fail=1; fi
  done
  # --fail-on exit codes (capture without tripping set -e)
  local pc=0 rc=0
  NO_COLOR=1 $PY audit r1 "$tmp" --transcript "$tmp/t.txt" --json --fail-on high >/dev/null 2>&1 || pc=$?
  NO_COLOR=1 "$RUST_BIN" audit r1 "$tmp" --transcript "$tmp/t.txt" --json --fail-on high >/dev/null 2>&1 || rc=$?
  if [[ "$pc" == "$rc" && "$pc" == 4 ]]; then echo "PASS  audit --fail-on (exit $pc)"; else echo "FAIL  audit --fail-on (py=$pc rs=$rc)"; fail=1; fi
  rm -rf "$tmp"
}
audit_check

# ── git scope: plan --staged (no commit needed) ──
gitscope_check() {
  command -v git >/dev/null 2>&1 || { echo "SKIP  plan --staged (git absent)"; return; }
  local tmp py rs; tmp="$(mktemp -d)"
  ( cd "$tmp" && git init -q && git config user.email t@t && git config user.name t \
    && mkdir src && printf 'def login(password, token): pass\n' > src/auth.py \
    && printf 'def helper(): pass\n' > src/util.py && git add -A ) >/dev/null 2>&1 || {
      echo "SKIP  plan --staged (git init failed)"; rm -rf "$tmp"; return; }
  py="$(cd "$tmp" && NO_COLOR=1 $PY plan . --json --no-history --staged 2>/dev/null || true)"
  rs="$(cd "$tmp" && NO_COLOR=1 "$RUST_BIN" plan . --json --no-history --staged 2>/dev/null || true)"
  if [[ "$py" == "$rs" ]]; then echo "PASS  plan --staged"; else echo "FAIL  plan --staged"; diff <(printf '%s' "$py") <(printf '%s' "$rs") | sed 's/^/    /'; fail=1; fi
  rm -rf "$tmp"
}
gitscope_check

# ── history: --json + --stats --json, and plan with history-boost ──
history_check() {
  local tmp now py rs; tmp="$(mktemp -d)"; mkdir -p "$tmp/.advisor" "$tmp/src"
  printf 'def login(password, token): pass\n' > "$tmp/src/auth.py"
  printf 'def helper(): pass\n' > "$tmp/src/util.py"
  now="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
  cat > "$tmp/.advisor/history.jsonl" <<JSONL
{"timestamp": "$now", "file_path": "src/util.py", "severity": "CRITICAL", "description": "recurring bug", "status": "CONFIRMED", "run_id": "r1", "schema_version": "1.0"}
{"timestamp": "2026-01-01T00:00:00+00:00", "file_path": "old.py", "severity": "HIGH", "description": "old", "status": "FIXED", "run_id": "r0", "schema_version": "1.0"}
JSONL
  local name args
  for spec in "history --json::history . --json" "history --stats --json::history . --stats --json" "plan (history boost)::plan . --json --min-priority 1"; do
    name="${spec%%::*}"; args="${spec##*::}"
    py="$(cd "$tmp" && NO_COLOR=1 $PY $args 2>/dev/null || true)"
    rs="$(cd "$tmp" && NO_COLOR=1 "$RUST_BIN" $args 2>/dev/null || true)"
    if [[ "$py" == "$rs" ]]; then echo "PASS  $name"; else echo "FAIL  $name"; diff <(printf '%s' "$py") <(printf '%s' "$rs") | sed 's/^/    /'; fail=1; fi
  done
  rm -rf "$tmp"
}
history_check

# ── checkpoints: --json list ──
checkpoints_check() {
  local tmp py rs; tmp="$(mktemp -d)"; mkdir -p "$tmp/.advisor"
  cat > "$tmp/.advisor/run-20260604T000000Z-abcd1234.json" <<'JSON'
{"run_id":"20260604T000000Z-abcd1234","created_at":"2026-06-04T00:00:00+00:00","target":"/repo","team_name":"review","file_types":"*.py","min_priority":3,"max_runners":5,"advisor_model":"claude-opus-4-7","runner_model":"claude-sonnet-4-6","max_fixes_per_runner":5,"large_file_line_threshold":800,"large_file_max_fixes":3,"test_command":"","context":"","tasks":[],"batches":[],"schema_version":"1.0"}
JSON
  cat > "$tmp/.advisor/run-20260603T000000Z-eeee2222.json" <<'JSON'
{"run_id":"x","created_at":"y","target":"/r","team_name":"review","file_types":"*.py","min_priority":3,"max_runners":5,"advisor_model":"o","runner_model":"s","tasks":[],"batches":[]}
JSON
  py="$(cd "$tmp" && NO_COLOR=1 $PY checkpoints . --json 2>/dev/null || true)"
  rs="$(cd "$tmp" && NO_COLOR=1 "$RUST_BIN" checkpoints . --json 2>/dev/null || true)"
  if [[ "$py" == "$rs" ]]; then echo "PASS  checkpoints --json"; else echo "FAIL  checkpoints --json"; diff <(printf '%s' "$py") <(printf '%s' "$rs") | sed 's/^/    /'; fail=1; fi
  rm -rf "$tmp"
}
checkpoints_check

if [[ "$fail" -ne 0 ]]; then
  echo "parity check FAILED" >&2
  exit 1
fi
echo "all parity checks passed"
