# Contributing to advisor

Thanks for your interest in improving advisor! This is a small, focused
project; most contributions land in under a week. Here's how to get going.

## Prerequisites

- Python 3.10+
- `pip` or `uv`
- `git`

## First-time setup

```bash
git clone https://github.com/vzwjustin/advisor
cd advisor
pip install -e ".[dev,completion]"
make hooks    # installs pre-commit + runs it once
```

`make hooks` installs the ruff/ruff-format/mypy pre-commit hooks and the
standard whitespace/YAML/TOML sanity checks. Everything is reproducible —
the hook revisions are pinned in `.pre-commit-config.yaml`.

## The dev loop

```bash
make check           # ruff + mypy + pytest — must pass before PR
make test            # pytest only
make fmt             # apply ruff format
pytest --cov=advisor --cov-report=term-missing   # local coverage
```

CI runs the same `make check` on Python 3.10–3.13 across Linux, macOS, and
Windows (`.github/workflows/ci.yml`).

## Project layout

See [`docs/architecture.md`](docs/architecture.md) for the module
dependency graph and runtime flow.

- `advisor/` — source
  - `__main__.py` — CLI entry point
  - `rank.py` / `focus.py` / `verify.py` — core logic (pure functions)
  - `install.py` — `~/.claude/CLAUDE.md` + `~/.claude/skills/advisor/SKILL.md` IO
  - `orchestrate/` — prompt builders + dispatch helpers
    - `_prompts/advisor.txt` — the Opus advisor prompt body (edit this as
      prose, NOT as a Python string)
  - `_style.py` — ANSI color helpers (opt out via `NO_COLOR=1`)
- `tests/` — `pytest` suite, one `test_<module>.py` per source module
- `docs/` — `architecture.md`, `prompts.md` (contributor reference only)

## Writing code

### Style
- All source must pass `ruff check` and `ruff format --check`.
- All source must pass `mypy --strict` (see `pyproject.toml`). New
  modules should have full type annotations; no bare `Any`.
- Dataclasses are **frozen** with `slots=True` unless there's a specific
  reason otherwise.
- Prompt builders are pure strings-in / strings-out — no I/O.

### Commits
- Small, logically coherent commits. Squash WIP noise before pushing.
- Conventional-commit subjects are welcome but not required.

### Tests
- Every new feature needs a test. Every bug fix needs a regression test.
- `pytest.raises(...)` over `try/except/assert False`.
- Hypothesis fuzz tests for anything parsing free-form text (see
  `tests/test_verify.py` for the pattern).

## Modifying prompts

The Opus advisor's system prompt lives in
`advisor/orchestrate/_prompts/advisor.txt`. **Before editing**:

1. Read [`docs/prompts.md`](docs/prompts.md) — it explains *why* the
   prompt looks the way it does (fenced user goal, pre-finding grep
   requirement, live-dialogue framing, etc.).
2. Run the full test suite after any change — many tests assert specific
   substrings in the rendered prompt. If your change invalidates one of
   those assertions, update the test *and* note the behavioral change in
   the PR description.
3. Test with BOTH an empty `context` and a populated `context` (the fenced
   goal block is conditional).

## Reporting bugs & filing feature requests

Use the GitHub issue templates under `.github/ISSUE_TEMPLATE/`.

For **suspected security issues**, see [`SECURITY.md`](SECURITY.md) — do
not file a public issue.

## Releasing (maintainers)

1. Update `CHANGELOG.md` — move entries under `## [Unreleased]` to a new
   `## [X.Y.Z] - YYYY-MM-DD` section.
2. Bump `version` in `pyproject.toml`.
3. Commit, tag, push:
   ```bash
   git commit -am "Release X.Y.Z"
   git tag vX.Y.Z
   git push origin main --tags
   ```
4. The `release.yml` workflow verifies tag-vs-pyproject agreement, runs
   tests, builds sdist + wheel, and publishes to PyPI via Trusted
   Publishing (no secrets). Watch the run under the Actions tab.
