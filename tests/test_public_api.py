"""Smoke test — verifies all symbols declared in __all__ are importable."""


def test_public_api_exports():
    from advisor import (
        format_findings_block,
        build_explore_prompt,
        ComponentStatus,
        Status,
        status,
    )
    assert callable(format_findings_block)
    assert callable(build_explore_prompt)
    assert ComponentStatus is not None
    assert Status is not None
    assert callable(status)
