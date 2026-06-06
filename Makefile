.PHONY: dev test check format lint clean install build cli release release-check

# Development setup (cargo build)
dev:
	cargo build

# Run all tests
test:
	cargo test

# Run type checker (no-op for Rust, kept for backward compatibility/scripts)
typecheck:
	@echo "Type checking is done by the compiler."

# Format code with cargo fmt
format:
	cargo fmt

# Lint code with cargo clippy
lint:
	cargo clippy --all-targets

# Run all checks
check:
	cargo clippy --all-targets && cargo fmt --check && cargo test

# Clean build artifacts
clean:
	cargo clean

# Install locally
install:
	cargo install --path .

# Build release distribution
build: clean
	cargo build --release

# Run advisor CLI
cli:
	cargo run --

# Pre-release checklist: everything that CI will run, plus a build.
# Run this before cutting a tag.
release-check: clean check build
	@echo ""
	@echo "== Release checklist =="
	@echo "  Version in Cargo.toml:       $$(grep -m1 '^version =' Cargo.toml | cut -d'"' -f2)"
	@echo "  CHANGELOG [Unreleased] empty: $$(awk '/## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f && NF' CHANGELOG.md | head -1 | grep -q . && echo NO || echo YES)"
	@echo ""
	@echo "If all looks good:"
	@echo "  1. Move [Unreleased] entries under a new [X.Y.Z] - YYYY-MM-DD header"
	@echo "  2. Commit ('release: X.Y.Z')"
	@echo "  3. Tag: git tag -s vX.Y.Z -m 'vX.Y.Z'"
	@echo "  4. Push: git push && git push --tags"

# Cut a release: run checks then print the exact commands to finish.
release: release-check
	@VER=$$(grep -m1 '^version =' Cargo.toml | cut -d'"' -f2); \
	echo "Ready to tag v$$VER — run the commands printed above."
