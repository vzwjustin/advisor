# Advisor — Agent Instructions

## Cursor Cloud specific instructions

### Quick reference

- **Package manager**: `uv` (lockfile: `uv.lock`)
- **Sync deps**: `uv sync --all-extras`
- **Lint**: `uv run ruff check advisor tests`
- **Format**: `uv run ruff format advisor tests`
- **Type check**: `uv run mypy advisor/`
- **Tests**: `uv run pytest tests/ -v`
- **All checks**: `uv run ruff check advisor tests && uv run mypy advisor/ && uv run pytest tests/ -v` (or `make check` with `.venv/bin/python`)
- **Run CLI**: `uv run advisor <subcommand>` (e.g. `version`, `plan`, `doctor`, `status`, `presets`, `ui`)
- **Web dashboard**: `uv run advisor ui --port 8765` (stdlib http.server, no extra deps)

### Gotchas

- Zero runtime dependencies — the package itself has `dependencies = []`. All dev tooling (pytest, ruff, mypy, hypothesis, pre-commit) is in the `[dev]` optional extra. Use `uv sync --all-extras` to get everything.
- The `Makefile` targets assume `.venv/bin/python`; prefer `uv run <tool>` commands directly when working with uv.
- `pyproject.toml` promotes `FutureWarning` to hard errors in pytest (`filterwarnings = ["error::FutureWarning"]`), so hypothesis fuzz tests will fail if any stdlib regex deprecation is triggered.
- mypy is set to `strict = true` with `warn_unused_ignores = true`; tests are excluded via `[[tool.mypy.overrides]]`.
- The `claude` and `codex` CLIs are not needed for development/testing — only for the live pipeline. `advisor doctor` will warn about their absence, but the tool is otherwise fully functional.
