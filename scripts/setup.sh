#!/usr/bin/env bash
# Idempotent dev-environment bootstrap for local work and Cursor Cloud agents.
# Replaces the legacy `uv sync --all-extras` path (Python package removed on main).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PATH="${HOME}/.cargo/bin:${PATH}"

ensure_rustup() {
  if command -v rustup >/dev/null 2>&1; then
    return 0
  fi
  echo "Installing rustup..."
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
  # shellcheck source=/dev/null
  source "${HOME}/.cargo/env"
}

ensure_rustup

# rust-toolchain.toml pins stable; install/refresh so edition-2024 deps resolve.
rustup toolchain install stable
rustup default stable
rustup component add rustfmt clippy --toolchain stable

echo "Rust: $(rustc --version)"
echo "Cargo: $(cargo --version)"

cargo build --locked

echo "Setup complete: advisor binary at target/debug/advisor"
