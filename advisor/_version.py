"""Version resolution helpers."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any


def _read_local_pyproject_version() -> str | None:
    """Return this checkout's project version when running from source."""
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.is_file():
        return None
    try:
        text = pyproject.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        tomllib: Any = import_module("tomllib")
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
        tomllib = None
    if tomllib is not None:
        try:
            data = tomllib.loads(text)
        except (TypeError, ValueError):
            pass
        else:
            project = data.get("project")
            if isinstance(project, dict):
                version = project.get("version")
                if isinstance(version, str) and version:
                    return version
    in_project = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line == "[project]":
            in_project = True
            continue
        if in_project and line.startswith("["):
            break
        if in_project and line.startswith("version"):
            key, sep, raw_value = line.partition("=")
            if sep and key.strip() == "version":
                value = raw_value.split("#", 1)[0].strip().strip("\"'")
                if value:
                    return value
    return None


def resolve_version() -> str:
    # Prefer the adjacent pyproject when this package is imported from a
    # checkout. Otherwise an older globally installed wheel can mask the
    # freshly fetched source version during local development.
    local_version = _read_local_pyproject_version()
    if local_version is not None:
        return local_version
    try:
        return pkg_version("advisor-agent")
    except PackageNotFoundError:  # pragma: no cover - not-installed fallback
        return "0+unknown"
