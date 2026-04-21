"""Smoke test — verifies all symbols declared in __all__ are importable."""

import advisor


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
