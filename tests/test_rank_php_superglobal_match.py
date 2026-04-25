"""Regression tests for the PHP superglobal regex anchors in advisor.rank.

The original implementation built keyword regexes as ``\\b{kw}\\b``, which
silently fails for keywords whose first/last character is non-word — e.g.
``$_GET`` (the leading ``$`` is non-word, so ``\\b`` never fires there).
After the fix, keywords with non-word boundaries use lookaround anchors.
"""

from __future__ import annotations

import pytest

from advisor.rank import _score_file, rank_files

PHP_SUPERGLOBALS = ["$_GET", "$_POST", "$_REQUEST", "$_FILES"]


@pytest.mark.parametrize("kw", PHP_SUPERGLOBALS)
def test_php_superglobal_is_detected_in_content(kw: str) -> None:
    """Each PHP superglobal must score the file at P4 (or higher)."""
    content = f"<?php\n$value = {kw}['name'];\n"
    priority, reasons = _score_file("public/index.php", content)
    assert priority >= 4, f"{kw!r} should trigger at least P4, got P{priority} ({reasons})"
    assert any(kw.lower() in r.lower() for r in reasons), f"{kw!r} expected in reasons {reasons!r}"


@pytest.mark.parametrize("kw", PHP_SUPERGLOBALS)
def test_php_superglobal_case_insensitive(kw: str) -> None:
    """Both ``$_GET`` and ``$_get`` should match — the regex is IGNORECASE."""
    content = f"<?php\n$value = {kw.lower()}['name'];\n"
    priority, _ = _score_file("public/index.php", content)
    assert priority >= 4


def test_php_superglobal_does_not_match_substring() -> None:
    """``$_GETLIST`` (made-up identifier) must NOT match ``$_GET``.

    The right-hand lookaround anchor prevents bleeding into wider tokens.
    """
    content = "<?php\n$value = $_GETLIST['name'];\n"
    priority, reasons = _score_file("public/index.php", content)
    # Without other risk signals, this should not promote to P4 just from
    # a partial-token match.
    assert priority < 4 or not any("$_get" in r.lower() for r in reasons)


def test_php_file_with_superglobal_outranks_neutral_php() -> None:
    """A PHP file that uses ``$_GET`` must outrank a neutral PHP file."""
    content_map = {
        "src/login.php": "<?php\n$user = $_GET['user'];\n",
        "src/util.php": "<?php\nfunction add($a, $b) { return $a + $b; }\n",
    }
    ranked = rank_files(list(content_map.keys()), read_fn=lambda p: content_map[p])
    assert ranked[0].path == "src/login.php"
    assert ranked[0].priority > ranked[1].priority
