"""Shared test fixtures for advisor tests.

The ``_reset_color_cache_each_test`` fixture is autouse so every test
starts with a freshly invalidated :func:`advisor._style.supports_color`
cache. This keeps ``monkeypatch.setenv('NO_COLOR', ...)`` and
``monkeypatch.setenv('TERM', 'dumb')`` semantics working — the cache
auto-detects env changes, but flipping *before* the first call in a
test would otherwise depend on whether an earlier test warmed the cache.

``isolate_home`` is a cross-platform helper used by tests that need the
CLI to treat ``tmp_path`` as ``$HOME``. On Windows ``Path.home()``
consults ``USERPROFILE`` (and ``HOMEDRIVE``/``HOMEPATH``) rather than
``HOME``, so setting only ``HOME`` leaks into the real user home and
contaminates later tests (see e.g. the nudge/status suites).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from advisor import _style


@pytest.fixture(autouse=True)
def _reset_color_cache_each_test() -> Iterator[None]:
    _style.reset_color_cache()
    yield
    _style.reset_color_cache()


def isolate_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``Path.home()`` at ``tmp_path`` on every supported OS.

    Sets ``HOME`` (POSIX), ``USERPROFILE`` (Windows primary), and
    ``HOMEDRIVE``/``HOMEPATH`` (Windows fallback). Returns ``tmp_path``
    for fluent use in test bodies.
    """

    home = str(tmp_path)
    monkeypatch.setenv("HOME", home)
    # Windows ``os.path.expanduser('~')`` — consulted by ``Path.home()`` —
    # prefers ``USERPROFILE`` over ``HOME``. Without this, tests writing
    # into ``Path.home() / '.claude'`` on the Windows CI runner install
    # into the real user profile and pollute later tests.
    monkeypatch.setenv("USERPROFILE", home)
    drive, _, tail = home.partition(":")
    if tail:
        monkeypatch.setenv("HOMEDRIVE", f"{drive}:")
        monkeypatch.setenv("HOMEPATH", tail or "\\")
    return tmp_path
