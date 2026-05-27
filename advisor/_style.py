"""Tiny terminal-styling helpers — ANSI on by default.

Colors are emitted by default so subprocess contexts (Claude Code's
Bash tool, IDEs, captured output) get the same styled view as a direct
terminal session.

Four env vars control color, in this precedence (highest wins):

* ``CLICOLOR_FORCE=1`` — force color on, even under ``NO_COLOR`` /
  ``TERM=dumb`` (https://bixense.com/clicolors).
* ``NO_COLOR`` (any non-empty value, https://no-color.org) or
  ``TERM=dumb`` — opt out.
* ``CLICOLOR=0`` — opt out (only honored when ``CLICOLOR_FORCE`` is unset).
* (default) — color on.

Under any opt-out path, every helper returns the input string unchanged
for byte-identical pipe output. The ``--no-color`` CLI flag also unsets
``CLICOLOR_FORCE`` for the process so it wins over a force override.
"""

from __future__ import annotations

import os
import re
import sys
import unicodedata
from typing import IO

_RESET = "\033[0m"
_CODES = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "dim": "\033[2m",
    "bold": "\033[1m",
}


def _disp_width(s: str) -> int:
    """Approximate terminal display width of ``s``.

    Counts East Asian Wide/Fullwidth characters as 2 columns and everything
    else as 1. Used by box helpers so CJK/wide glyphs don't desync the
    border from the centered text. Not a full ``wcwidth`` — emoji whose
    East-Asian-Width category is ``N`` (many BMP-supplementary emoji) still
    count as 1, which is a known limitation.
    """
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _compute_supports_color() -> bool:
    # Precedence: CLICOLOR_FORCE=1 wins over everything (per
    # https://bixense.com/clicolors — explicit user-override of NO_COLOR
    # for CI-with-colors workflows). Then NO_COLOR / TERM=dumb / CLICOLOR=0
    # all independently disable.
    if os.environ.get("CLICOLOR_FORCE", "") == "1":
        return True
    if os.environ.get("NO_COLOR", "") != "":
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    if os.environ.get("CLICOLOR", "") == "0":
        return False
    return True


def _stream_supports_unicode(stream: IO[str] | None) -> bool:
    """True when ``stream`` can encode the glyphs used by styled helpers.

    Optimistic-by-design: when we can't introspect the stream's
    encoding (``stream is None`` or ``stream.encoding`` is None — the
    latter happens with ``io.StringIO`` and certain captured streams),
    we return True. The helpers in this module RETURN STRINGS; they
    don't write. If a downstream caller writes the returned string
    into a stream that can't encode Unicode, the UnicodeEncodeError is
    raised at the caller's ``print()`` site, not here — caller's
    responsibility to either pass an introspectable encoding-aware
    stream or wrap the write in their own encoding handling.

    Returning False here would break the documented contract that
    ``foo(text, stream=X) == foo(text, stream=None)`` for default-
    color streams (used by several existing tests), since callers
    legitimately pass a no-encoding stream just to disable color via
    ``CLICOLOR=0`` without changing helper output.
    """
    if stream is None:
        return True
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return True
    try:
        "✓✗⚠ℹ💡→━┏┓┗┛┃↻·".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


# Cached env snapshot used by ``supports_color``. Each styled span on a
# rendered pipeline can call this dozens of times; re-reading the env
# vars in tight loops is wasteful. The cache is invalidated either
# explicitly via :func:`reset_color_cache` (used by the autouse pytest
# fixture so ``monkeypatch.setenv`` is observed) or implicitly by detecting
# the env snapshot changed. Keep the snapshot tuple in sync with every
# var consulted by ``_compute_supports_color`` — a var that's read but
# not snapshotted will go stale silently.
_CACHED_SUPPORT: bool | None = None
_CACHED_ENV_SNAPSHOT: tuple[str | None, str | None, str | None, str | None] | None = None


def _env_snapshot() -> tuple[str | None, str | None, str | None, str | None]:
    return (
        os.environ.get("NO_COLOR"),
        os.environ.get("TERM"),
        os.environ.get("CLICOLOR_FORCE"),
        os.environ.get("CLICOLOR"),
    )


def reset_color_cache() -> None:
    """Invalidate the cached ``supports_color`` result.

    Public hook for test fixtures — ``monkeypatch.setenv`` doesn't flush
    our cache on its own, so tests that flip ``NO_COLOR``/``TERM`` must
    call this (the autouse fixture in ``tests/conftest.py`` handles it).
    """
    global _CACHED_SUPPORT, _CACHED_ENV_SNAPSHOT
    _CACHED_SUPPORT = None
    _CACHED_ENV_SNAPSHOT = None


def supports_color(stream: IO[str] | None = None) -> bool:
    """Return True when ANSI styling should be emitted.

    The ``stream`` argument is reserved — it's threaded through by every
    helper so a future per-stream policy (e.g. respect a NO_COLOR env-var
    set on a captured `io.StringIO`) can be added without touching call
    sites. Today only process-wide env vars are consulted.

    Result is cached across calls; the cache auto-invalidates if the
    relevant env vars change (covers normal process mutation without
    requiring callers to remember :func:`reset_color_cache`).
    """
    if not _stream_supports_unicode(stream):
        return False
    global _CACHED_SUPPORT, _CACHED_ENV_SNAPSHOT
    snap = _env_snapshot()
    if _CACHED_SUPPORT is None or snap != _CACHED_ENV_SNAPSHOT:
        _CACHED_SUPPORT = _compute_supports_color()
        _CACHED_ENV_SNAPSHOT = snap
    return _CACHED_SUPPORT


def paint(text: str, *styles: str, stream: IO[str] | None = None) -> str:
    if not styles or not supports_color(stream):
        return text
    # Defensive: callers reading STATE_GLYPHS / ACTION_GLYPHS pass a
    # ``str | None`` color through positionally. The signature says ``str``,
    # but a ``None`` slipping past mypy (e.g. via a tuple unpack on a
    # broader-typed dict) would raise ``KeyError`` on ``_CODES[None]``.
    # Filter ``None`` and non-str entries here so the call is total.
    prefix = "".join(_CODES[s] for s in styles if isinstance(s, str) and s in _CODES)
    return f"{prefix}{text}{_RESET}" if prefix else text


def glyph(fancy: str, plain: str, stream: IO[str] | None = None) -> str:
    """Use a Unicode glyph on color-capable TTYs, ASCII fallback elsewhere."""
    return fancy if supports_color(stream) else plain


def err(text: str, stream: IO[str] | None = None) -> str:
    return paint(text, "red", "bold", stream=stream if stream is not None else sys.stderr)


def ok(text: str, stream: IO[str] | None = None) -> str:
    return paint(text, "green", stream=stream if stream is not None else sys.stdout)


def dim(text: str, stream: IO[str] | None = None) -> str:
    return paint(text, "dim", stream=stream)


def banner(text: str, width: int = 50, stream: IO[str] | None = None) -> str:
    """Create a visual banner with box-drawing characters.

    Helps with visual scanning and adds clear hierarchy to CLI output.
    ``width`` auto-expands when ``text`` is longer than ``width - 4`` so the
    border always encloses the text — otherwise a 60-char title inside a
    width=50 box would overflow the border, producing a broken banner.
    """
    stream = stream if stream is not None else sys.stdout
    text = strip_ansi(text)
    if not supports_color(stream):
        return f"== {text} =="
    # +4 accounts for the two-space pad on each side of the centered text.
    # Use display width (CJK / wide chars count as 2) so the border encloses
    # the text correctly regardless of len() vs visible-column mismatch.
    text_w = _disp_width(text)
    effective_width = max(width, text_w + 4)
    line = "━" * effective_width
    inner = effective_width - 4
    left = (inner - text_w) // 2
    right = inner - text_w - left
    centered = " " * left + text + " " * right
    return (
        f"{paint('┏', 'cyan')}{paint(line, 'cyan')}{paint('┓', 'cyan')}\n"
        f"{paint('┃', 'cyan')}  {paint(centered, 'bold')}  {paint('┃', 'cyan')}\n"
        f"{paint('┗', 'cyan')}{paint(line, 'cyan')}{paint('┛', 'cyan')}"
    )


def success_box(text: str, stream: IO[str] | None = None) -> str:
    """Draw a green success box with checkmark."""
    stream = stream if stream is not None else sys.stdout
    text = strip_ansi(text)
    mark = glyph("✓", "[OK]", stream=stream)
    if not supports_color(stream):
        return f"{mark} {text}"
    return f"{paint(mark, 'green', 'bold')} {paint(text, 'green')}"


def info_box(text: str, stream: IO[str] | None = None) -> str:
    """Draw a blue info box with info symbol."""
    stream = stream if stream is not None else sys.stdout
    text = strip_ansi(text)
    mark = glyph("ℹ", "[i]", stream=stream)
    if not supports_color(stream):
        return f"{mark} {text}"
    return f"{paint(mark, 'blue', 'bold')} {paint(text, 'blue')}"


def warning_box(text: str, stream: IO[str] | None = None) -> str:
    """Draw a yellow warning box with warning symbol."""
    stream = stream if stream is not None else sys.stdout
    text = strip_ansi(text)
    mark = glyph("⚠", "[!]", stream=stream)
    if not supports_color(stream):
        return f"{mark} {text}"
    return f"{paint(mark, 'yellow', 'bold')} {paint(text, 'yellow')}"


def error_box(text: str, stream: IO[str] | None = None) -> str:
    """Draw a red error line with an error glyph — symmetric with success/info/warning_box.

    Stream default is ``sys.stderr`` (not stdout like the other box
    helpers) because errors should not contaminate piped stdout output
    or be swallowed when a caller does ``advisor ... > out.txt``.
    Almost all internal call sites already pass ``stream=sys.stderr``
    explicitly — the corrected default catches the few that didn't AND
    matches what external library consumers would intuitively expect.
    """
    stream = stream if stream is not None else sys.stderr
    text = strip_ansi(text)
    mark = glyph("✗", "[x]", stream=stream)
    if not supports_color(stream):
        return f"{mark} {text}"
    return f"{paint(mark, 'red', 'bold')} {paint(text, 'red')}"


def tip(text: str, stream: IO[str] | None = None) -> str:
    """Inline hint line — dim body with a bold cyan lightbulb lead."""
    stream = stream if stream is not None else sys.stdout
    text = strip_ansi(text)
    mark = glyph("💡", "tip:", stream=stream)
    if not supports_color(stream):
        return f"  {mark} {text}"
    return f"  {paint(mark, 'cyan', 'bold', stream=stream)} {dim(text, stream=stream)}"


def cta(action: str, description: str = "", stream: IO[str] | None = None) -> str:
    """Call-to-action row — bold primary `action` plus optional dim description."""
    stream = stream if stream is not None else sys.stdout
    action, description = strip_ansi(action), strip_ansi(description)
    bullet = glyph("→", ">", stream=stream)
    if not supports_color(stream):
        sep = "  " if description else ""
        return f"  {bullet} {action}{sep}{description}"
    lead = paint(bullet, "cyan", "bold", stream=stream)
    act = paint(action, "bold", stream=stream)
    if not description:
        return f"  {lead} {act}"
    return f"  {lead} {act}  {dim(description, stream=stream)}"


def header_block(
    title: str,
    rows: list[tuple[str, str]],
    *,
    width: int = 52,
    stream: IO[str] | None = None,
) -> str:
    """Banner + dim-label / value rows for multi-row command summaries."""
    lines = [banner(title, width=width, stream=stream), ""]
    for label, value in rows:
        dim_label = dim(f"{label:<10}", stream=stream)
        lines.append(f"  {dim_label} {value}")
    return "\n".join(lines)


# (label, fancy_glyph, ascii_glyph, color) — canonical glyph tables shared
# across CLI surfaces. "dim" is a valid color key handled by paint().
ACTION_GLYPHS: dict[str, tuple[str, str, str, str | None]] = {
    "installed": ("installed", "✓", "+", "green"),
    "updated": ("updated", "↻", "~", "cyan"),
    "unchanged": ("unchanged", "·", "-", "dim"),
    "removed": ("removed", "✗", "x", "yellow"),
    "absent": ("not found", "·", "-", "dim"),
    "skipped": ("skipped", "·", "-", "dim"),
}

STATE_GLYPHS: dict[str, tuple[str, str, str, str | None]] = {
    "ok": ("ok", "✓", "+", "green"),
    "outdated": ("outdated", "↻", "~", "yellow"),
    "warn": ("warn", "⚠", "!", "yellow"),
    "fail": ("fail", "✗", "x", "red"),
    "missing": ("missing", "✗", "x", "red"),
}


# Priority badge colors — high-contrast on both light and dark terminals.
_PRIORITY_STYLES: dict[str, tuple[str, ...]] = {
    "5": ("red", "bold"),
    "4": ("magenta", "bold"),
    "3": ("yellow", "bold"),
    "2": ("cyan", "bold"),
    "1": ("dim",),
}

# Combined priority regex — bold (**P3**) OR bare (P3) in one pass.
# Group 1 = bold digit; Group 2 = bare digit. Exactly one of the two is
# populated per match. Collapsing the two patterns into one alternation
# eliminates a full-text scan without changing semantics.
_PRIORITY_RE = re.compile(r"\*\*P([1-5])\*\*" r"|(?<![A-Za-z0-9*\x1b])P([1-5])(?![A-Za-z0-9*])")
# Matches an SGR escape sequence ending just before the match position,
# used to detect (and skip) priority matches that sit INSIDE already-
# colorized text (e.g. a colored header body). Re-painting them would
# emit an inner ``\x1b[0m`` that prematurely closes the outer style.
_ANSI_SGR_RE = re.compile(r"\x1b\[[\d;]*m")
# Broader than ``_ANSI_SGR_RE``: matches any CSI (``ESC [ ... <letter>``)
# plus OSC (``ESC ] ... BEL`` or ``ESC ] ... ESC \``). Used by
# ``strip_ansi`` so untrusted finding text can't smuggle clipboard-write
# OSC sequences or cursor-control CSIs through the renderer. Must NOT be
# used for ``_inside_ansi_span`` — that one specifically needs to count
# SGR opens/closes that this module emits via ``paint()``.
_ANSI_STRIP_RE = re.compile(r"\x1b\[[\d;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# Matches any SGR whose first parameter is `0` — both the canonical
# ``\x1b[0m`` reset and combined sequences like ``\x1b[0;1m`` (reset
# then bold). Used by `_inside_ansi_span` to count resets correctly.
_ANSI_RESET_RE = re.compile(r"\x1b\[0[;m]")
# Matches a combined reset-then-set SGR like ``\x1b[0;36m`` — leading
# ``0`` followed by at least one more parameter. These sequences both
# CLOSE the outer span and OPEN a new one in a single escape, so they
# must contribute one to each counter in `_inside_ansi_span`.
_ANSI_COMBINED_RESET_SET_RE = re.compile(r"\x1b\[0;[\d;]+m")
# Combined header regex (H2/H3/H4 in a single pass). Depth is inferred
# from the captured ``#`` run length; each depth gets a different style.
# H1 is intentionally excluded — Markdown docs almost never emit ``# `` at
# the start of a line outside titles, and we don't want to color those.
_HEADER_RE = re.compile(r"^(#{2,4})(\s+)(.+)$", re.MULTILINE)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")
_BLOCKQUOTE_RE = re.compile(r"^(>\s+)(.+)$", re.MULTILINE)

_HEADER_STYLES: dict[int, tuple[str, ...]] = {
    2: ("cyan", "bold"),
    3: ("blue", "bold"),
    4: ("bold",),
}


def strip_ansi(text: str) -> str:
    """Remove ANSI CSI + OSC escape sequences from ``text``.

    Broader than just SGR colors — also drops cursor-control CSIs
    (``\\x1b[2J``, ``\\x1b[6n``) and OSC sequences (``\\x1b]0;...\\x07``,
    ``\\x1b]52;c;<base64>\\x07``). Use for any input that may originate
    from untrusted source (target-repo file content, third-party output)
    before printing or re-styling.
    """
    return _ANSI_STRIP_RE.sub("", text)


def colorize_markdown(text: str, stream: IO[str] | None = None) -> str:
    """Colorize markdown headers, priority badges, and `paths`.

    Returns the input unchanged when ``NO_COLOR`` is set or ``TERM=dumb``.
    Markdown markers (``**``, ``##``, backticks) are preserved so the output
    still works as a paste-into-Claude artifact even with colors on.

    Strips any pre-existing ANSI CSI/OSC escape sequences from the input
    before colorizing. A finding description sourced from a target-repo
    file (e.g. a literal ``\\x1b[`` or ``\\x1b]52;c;…\\x07`` in source)
    would otherwise pass through unchanged and inject terminal escape
    sequences. The local colorizer adds its own span markers afterward,
    so dropping inbound escapes is safe.
    """
    if not supports_color(stream):
        # Even on no-color terminals, strip inbound ANSI so a finding
        # description doesn't smuggle escapes into a "plain" output.
        return strip_ansi(text)
    text = strip_ansi(text)

    def _inside_ansi_span(full: str, pos: int) -> bool:
        """True when ``pos`` falls inside an unclosed SGR span.

        Counts SGR escapes preceding ``pos`` and compares openers vs
        resets. Any SGR whose first parameter is ``0`` is treated as a
        reset — that includes the canonical ``\\x1b[0m`` AND combined
        sequences like ``\\x1b[0;1m`` (reset then bold). A combined
        reset-then-set sequence (``\\x1b[0;36m``) ALSO opens a new span
        in the same escape, so it counts as both one close and one open.
        Without that, ``\\x1b[32mgreen\\x1b[0;36mP3\\x1b[0m`` would zero
        the counter at the priority position even though the terminal
        is still in a colored state.
        """
        prefix = full[:pos]
        escapes = _ANSI_SGR_RE.findall(prefix)
        close_count = sum(1 for e in escapes if _ANSI_RESET_RE.match(e))
        # Combined reset-then-set sequences contribute an additional
        # open beyond the bare-escape count (which already gives them
        # one open via ``len(escapes) - close_count`` is wrong here —
        # they were counted as a close, not an open). Add one open per
        # combined occurrence to restore the balance.
        combined_count = sum(1 for e in escapes if _ANSI_COMBINED_RESET_SET_RE.match(e))
        open_count = len(escapes) - close_count + combined_count
        return open_count > close_count

    def _color_priority(m: re.Match[str]) -> str:
        if _inside_ansi_span(m.string, m.start()):
            return m.group(0)
        # Exactly one of group(1) (bold) / group(2) (bare) is non-None.
        digit = m.group(1) or m.group(2)
        assert digit is not None  # regex guarantees one branch matched
        styles = _PRIORITY_STYLES[digit]
        token = f"**P{digit}**" if m.group(1) is not None else f"P{digit}"
        return paint(token, *styles, stream=stream)

    def _color_header(m: re.Match[str]) -> str:
        hashes, ws, body = m.group(1), m.group(2), m.group(3)
        depth = len(hashes)
        styles = _HEADER_STYLES.get(depth, ("bold",))
        return hashes + ws + paint(body, *styles, stream=stream)

    def _color_path(m: re.Match[str]) -> str:
        # Same guard as ``_color_priority``: a backtick path inside an
        # already-painted header body would inject ``\x1b[32m...\x1b[0m``
        # whose inner reset prematurely closes the outer header span.
        # Leave the backtick as-is when the match sits inside an open SGR.
        if _inside_ansi_span(m.string, m.start()):
            return m.group(0)
        return "`" + paint(m.group(1), "green", stream=stream) + "`"

    def _color_blockquote(m: re.Match[str]) -> str:
        # Dim only the ``> `` marker. Body may already contain ANSI spans
        # from earlier substitutions (backticks, priorities) — wrapping it
        # in dim would inject a reset mid-span and break existing styling.
        return paint(m.group(1), "dim", stream=stream) + m.group(2)

    # Order matters. Headers must be colorized BEFORE priorities: if a
    # header line contains a bare `P3`, coloring the priority first inserts
    # an ANSI reset inside the header's body. The subsequent header regex
    # then re-wraps the line, nesting escapes — and the inner reset closes
    # the outer header style early, leaving the text after the priority
    # unstyled. Running headers first ensures priority ANSI sits cleanly
    # inside already-painted header text (which the priority lookbehind
    # then skips).
    text = _HEADER_RE.sub(_color_header, text)
    text = _PRIORITY_RE.sub(_color_priority, text)
    text = _BACKTICK_RE.sub(_color_path, text)
    text = _BLOCKQUOTE_RE.sub(_color_blockquote, text)
    return text
