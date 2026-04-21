"""Shared test fixtures for advisor tests.

The ``_reset_color_cache_each_test`` fixture is autouse so every test
starts with a freshly invalidated :func:`advisor._style.supports_color`
cache. This keeps ``monkeypatch.setenv('NO_COLOR', ...)`` and
``monkeypatch.setenv('TERM', 'dumb')`` semantics working — the cache
auto-detects env changes, but flipping *before* the first call in a
test would otherwise depend on whether an earlier test warmed the cache.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from advisor import _style


@pytest.fixture(autouse=True)
def _reset_color_cache_each_test() -> Iterator[None]:
    _style.reset_color_cache()
    yield
    _style.reset_color_cache()
