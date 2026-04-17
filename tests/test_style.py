"""Tests for advisor._style module — colors-on-by-default contract."""

import io

import pytest

from advisor import _style


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Strip color-related env vars so each test starts from a clean slate."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("TERM", raising=False)


def test_supports_color_default_is_true(monkeypatch):
    assert _style.supports_color() is True


def test_supports_color_default_true_for_non_tty_stream(monkeypatch):
    """Always-on contract: a captured (non-tty) stream still gets colors."""
    buf = io.StringIO()
    assert _style.supports_color(buf) is True


def test_supports_color_no_color_env_disables(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert _style.supports_color() is False


def test_supports_color_no_color_empty_string_still_disables(monkeypatch):
    """NO_COLOR spec: presence is what counts, not the value."""
    monkeypatch.setenv("NO_COLOR", "")
    assert _style.supports_color() is False


def test_supports_color_term_dumb_disables(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    assert _style.supports_color() is False


def test_supports_color_term_xterm_enables(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    assert _style.supports_color() is True


def test_paint_emits_ansi_by_default():
    out = _style.paint("hello", "red", "bold")
    assert out.startswith("\033[")
    assert out.endswith("\033[0m")
    assert "hello" in out


def test_paint_no_op_under_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert _style.paint("hello", "red", "bold") == "hello"


def test_paint_no_op_when_no_styles():
    assert _style.paint("hello") == "hello"


def test_glyph_returns_fancy_by_default():
    assert _style.glyph("✓", "+") == "✓"


def test_glyph_returns_plain_under_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert _style.glyph("✓", "+") == "+"


def test_dim_emits_dim_ansi_by_default():
    assert "\033[2m" in _style.dim("hint")


def test_err_emits_red_bold_ansi_by_default():
    out = _style.err("error:")
    assert "\033[31m" in out
    assert "\033[1m" in out


def test_err_stream_param_does_not_alter_output():
    """stream param on err() must not change the returned string."""
    assert _style.err("x", stream=io.StringIO()) == _style.err("x")


def test_ok_stream_param_does_not_alter_output():
    """stream param on ok() must not change the returned string."""
    assert _style.ok("x", stream=io.StringIO()) == _style.ok("x")


def test_colorize_markdown_styles_priority_badges():
    out = _style.colorize_markdown("**P3** something")
    assert "\033[" in out
    assert "P3" in out
    assert "**" in out  # markers preserved for paste-ability


def test_colorize_markdown_styles_headers():
    out = _style.colorize_markdown("## Section")
    assert "\033[" in out
    assert "## " in out
    assert "Section" in out


def test_colorize_markdown_no_op_under_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    src = "## Header\n**P3** `path/to/file.py`"
    assert _style.colorize_markdown(src) == src


def test_colorize_markdown_no_op_under_term_dumb(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    src = "## Header"
    assert _style.colorize_markdown(src) == src


def test_tip_emits_lightbulb_and_dim_body_by_default():
    out = _style.tip("use --foo")
    assert "💡" in out
    assert "use --foo" in out
    assert "\033[2m" in out  # body is dim


def test_tip_ascii_fallback_under_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = _style.tip("use --foo")
    assert "💡" not in out
    assert "tip:" in out
    assert "use --foo" in out
    assert "\033[" not in out  # no ANSI


def test_cta_emits_bold_action_and_dim_description_by_default():
    out = _style.cta("/advisor <dir>", "Start a code review")
    assert "/advisor <dir>" in out
    assert "Start a code review" in out
    assert "→" in out
    assert "\033[1m" in out  # action is bold
    assert "\033[2m" in out  # description is dim


def test_cta_without_description_renders_action_only():
    out = _style.cta("/advisor <dir>")
    assert "/advisor <dir>" in out
    assert "→" in out


def test_cta_ascii_fallback_under_no_color(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    out = _style.cta("/advisor <dir>", "Start a code review")
    assert "→" not in out
    assert ">" in out
    assert "/advisor <dir>" in out
    assert "Start a code review" in out
    assert "\033[" not in out


def test_codes_covers_every_color_used_in_package():
    """Regression guard: every color literal passed to paint() in the package
    must exist in _CODES, or paint() silently no-ops and the UI loses color.

    This catches the class of silent bug where a helper asks for `"blue"` or
    any other name that never made it into the palette dict.
    """
    import re
    from pathlib import Path

    pkg_root = Path(_style.__file__).parent
    # Colors passed as the 2nd+ positional arg to paint(...) — e.g.
    # `paint(x, "cyan", "bold")`. Restrict the first arg to characters that
    # cannot contain a nested call, so `paint(glyph("x", "y"), "cyan")`
    # doesn't misattribute "y" as a color.
    pat = re.compile(r"""paint\(\s*[^(),]+?,\s*['"]([a-z]+)['"]""")
    used: set[str] = set()
    for py in pkg_root.glob("*.py"):
        for color in pat.findall(py.read_text(encoding="utf-8")):
            used.add(color)

    # These are the non-color style modifiers, also valid in _CODES.
    modifiers = {"bold", "dim"}
    colors_actually_used = used - modifiers

    missing = colors_actually_used - set(_style._CODES)
    assert not missing, f"colors used by callers but missing from _CODES: {missing}"


def test_codes_contains_every_color_modifier_referenced_by_style_helpers():
    """Regression guard: the named helpers err/ok/dim/box family all resolve
    to keys that exist in _CODES. A missing key causes silent no-op styling.
    """
    required = {"red", "green", "yellow", "blue", "cyan", "magenta", "bold", "dim"}
    assert required <= set(_style._CODES), (
        f"_CODES missing required keys: {required - set(_style._CODES)}"
    )


def test_priority_styles_all_bold_for_contrast_ladder():
    """Regression guard: P2–P5 must all be bold for legibility on light terms."""
    for level in ("2", "3", "4", "5"):
        styles = _style._PRIORITY_STYLES[level]
        assert "bold" in styles, f"P{level} must be bold for contrast ladder"


def test_colorize_markdown_header_with_priority_no_inner_reset():
    """Header containing a bare priority must render with a single clean wrap.

    Regression: previously the priority colorizer ran first, then the header
    regex wrapped the entire line, nesting ANSI escapes. The inner `\\x1b[0m`
    from the priority would close the outer header style early, leaving text
    after the priority badge unstyled.
    """
    out = _style.colorize_markdown("## Ranking P3 files")
    # No premature reset followed by unstyled body text.
    assert "\x1b[0m files" not in out
    # Single outer wrap: exactly one top-level `\x1b[0m` closes the header.
    assert out.count("\x1b[0m") == 1


def test_banner_auto_expands_for_overlong_text():
    """Regression: text longer than ``width - 4`` previously overflowed the
    fixed-width border. The banner now auto-widens so the box always encloses
    the text cleanly."""
    out = _style.banner("x" * 60, width=50)
    lines = out.split("\n")
    assert len(lines) == 3
    assert "x" * 60 in lines[1]
    # Borders (strip ANSI) must be wide enough to fit text + 4 padding.
    top_plain = _style._ANSI_SGR_RE.sub("", lines[0])
    bot_plain = _style._ANSI_SGR_RE.sub("", lines[2])
    assert top_plain.count("━") >= 60
    assert bot_plain.count("━") >= 60
