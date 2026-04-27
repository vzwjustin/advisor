"""Smoke test — verifies all symbols declared in __all__ are importable."""

from pathlib import Path

import advisor


def _pyproject_version() -> str:
    text = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
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
                return raw_value.split("#", 1)[0].strip().strip("\"'")
    raise AssertionError("pyproject.toml has no [project].version")


def test_public_api_exports():
    from advisor import (
        ComponentStatus,
        InstallAction,
        Status,
        build_advisor_prompt,
        format_findings_block,
        status,
    )

    assert callable(format_findings_block)
    assert callable(build_advisor_prompt)
    assert ComponentStatus is not None
    assert Status is not None
    assert callable(status)
    assert InstallAction.INSTALLED.value == "installed"


def test_all_symbols_in_all_resolve():
    """Every name declared in ``__all__`` must actually be importable
    as an attribute of the ``advisor`` package.

    Catches CHANGELOG / docs promises that don't exist at runtime —
    like the pre-0.4.1 miss where ``InstallAction`` was documented in
    CHANGELOG but not exported.
    """
    missing: list[str] = []
    for name in advisor.__all__:
        if not hasattr(advisor, name):
            missing.append(name)
    assert not missing, f"declared in __all__ but not importable: {missing}"


def test_version_is_nonempty_string():
    assert isinstance(advisor.__version__, str)
    assert advisor.__version__  # not empty


def test_source_checkout_version_wins_over_stale_distribution(monkeypatch):
    """Running from a checkout must not report an older installed wheel version."""
    import advisor._version as version_mod

    monkeypatch.setattr(version_mod, "pkg_version", lambda _name: "0.0.0")
    assert version_mod.resolve_version() == _pyproject_version()
    assert advisor.__version__ == _pyproject_version()
