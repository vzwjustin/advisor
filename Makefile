.PHONY: test coverage lint format typecheck check clean install dev hooks build cli release release-check

# Default Python interpreter
PYTHON ?= .venv/bin/python

# Development setup (editable install + dev tooling)
dev:
	$(PYTHON) -m pip install -e ".[dev]"

# Install pre-commit hooks
hooks:
	$(PYTHON) -m pre_commit install
	$(PYTHON) -m pre_commit run --all-files

# Run all tests
test:
	$(PYTHON) -m pytest tests/ -v

# Run tests with coverage
coverage:
	$(PYTHON) -m pytest tests/ --cov=advisor --cov-report=term-missing --cov-report=html

# Run type checker
typecheck:
	$(PYTHON) -m mypy advisor/

# Format code with ruff
format:
	$(PYTHON) -m ruff format advisor tests

# Lint code with ruff
lint:
	$(PYTHON) -m ruff check advisor tests

# Fix auto-fixable lint issues
lint-fix:
	$(PYTHON) -m ruff check advisor tests --fix

# Run all checks
check: lint typecheck test

# Clean build artifacts
clean:
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ .mypy_cache/ .ruff_cache/ htmlcov/ .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

# Install locally in editable mode
install:
	$(PYTHON) -m pip install -e .

# Build distribution
build: clean
	$(PYTHON) -m pip install build
	$(PYTHON) -m build

# Run advisor CLI
cli:
	$(PYTHON) -m advisor

# Pre-release checklist: everything that CI will run, plus a build.
# Run this before cutting a tag.
release-check: clean check build
	@echo ""
	@echo "== Release checklist =="
	@echo "  Version in pyproject.toml:   $$(grep -m1 '^version =' pyproject.toml)"
	@echo "  advisor.__version__:         $$($(PYTHON) -c 'import advisor; print(advisor.__version__)')"
	@echo "  CHANGELOG [Unreleased] empty: $$(awk '/## \[Unreleased\]/{f=1;next} /^## \[/{f=0} f && NF' CHANGELOG.md | head -1 | grep -q . && echo NO || echo YES)"
	@echo ""
	@echo "If all looks good:"
	@echo "  1. Move [Unreleased] entries under a new [X.Y.Z] - YYYY-MM-DD header"
	@echo "  2. Commit ('release: X.Y.Z')"
	@echo "  3. Tag: git tag -s vX.Y.Z -m 'vX.Y.Z'"
	@echo "  4. Push: git push && git push --tags"
	@echo "  5. GitHub Actions will publish to PyPI via trusted publishing"

# Cut a release: run checks then print the exact commands to finish.
# No git operations are performed automatically.
release: release-check
	@VER=$$(grep -m1 '^version =' pyproject.toml | cut -d'"' -f2); \
	echo "Ready to tag v$$VER — run the commands printed above."
