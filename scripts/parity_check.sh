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

if [[ "$fail" -ne 0 ]]; then
  echo "parity check FAILED" >&2
  exit 1
fi
echo "all parity checks passed"
