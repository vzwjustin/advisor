.PHONY: test lint format check clean install dev

# Default Python interpreter
PYTHON ?= .venv/bin/python

# Development setup
dev:
	pip install -e ".[dev]"

# Run all tests
test:
	$(PYTHON) -m pytest tests/ -v

# Run tests with coverage
coverage:
	$(PYTHON) -m pytest tests/ --cov=advisor --cov-report=term-missing --cov-report=html

# Run type checker (if mypy is installed)
typecheck:
	$(PYTHON) -m mypy advisor/

# Format code with ruff (if installed)
format:
	$(PYTHON) -m ruff format advisor tests

# Lint code with ruff (if installed)
lint:
	$(PYTHON) -m ruff check advisor tests

# Fix auto-fixable lint issues
lint-fix:
	$(PYTHON) -m ruff check advisor tests --fix

# Run all checks
check: lint typecheck test

# Clean build artifacts
clean:
	rm -rf build/ dist/ *.egg-info/ .pytest_cache/ .mypy_cache/ htmlcov/
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete

# Install locally in editable mode
install:
	pip install -e .

# Build distribution
build: clean
	pip install build
	python -m build

# Run advisor CLI
cli:
	$(PYTHON) -m advisor
