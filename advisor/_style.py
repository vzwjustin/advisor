"""Tiny terminal-styling helpers — ANSI on by default.

Colors are emitted by default so subprocess contexts (Claude Code's
Bash tool, IDEs, captured output) get the same styled view as a direct
terminal session. Opt out with ``NO_COLOR=1`` (https://no-color.org) or
``TERM=dumb``; under either, every helper returns the input string
unchanged for byte-identical pipe output.
"""

from __future__ import annotations

import os
import re
import sys
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


def _compute_supports_color() -> bool:
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


def _stream_supports_unicode(stream: IO[str] | None) -> bool:
    """True when ``stream`` can encode the glyphs used by styled helpers."""
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
# rendered pipeline can call this dozens of times; re-reading the two
# env vars in tight loops is wasteful. The cache is invalidated either
# explicitly via :func:`reset_color_cache` (used by the autouse pytest
# fixture so ``monkeypatch.setenv`` is observed) or implicitly by detecting
# the env snapshot changed.
_CACHED_SUPPORT: bool | None = None
_CACHED_ENV_SNAPSHOT: tuple[str | None, str | None] | None = None


def _env_snapshot() -> tuple[str | None, str | None]:
    return (os.environ.get("NO_COLOR"), os.environ.get("TERM"))


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
    if not supports_color(stream):
        return f"== {text} =="
    # +4 accounts for the two-space pad on each side of the centered text.
    effective_width = max(width, len(text) + 4)
    line = "━" * effective_width
    centered = text.center(effective_width - 4)
    return (
        f"{paint('┏', 'cyan')}{paint(line, 'cyan')}{paint('┓', 'cyan')}\n"
        f"{paint('┃', 'cyan')}  {paint(centered, 'bold')}  {paint('┃', 'cyan')}\n"
        f"{paint('┗', 'cyan')}{paint(line, 'cyan')}{paint('┛', 'cyan')}"
    )


def success_box(text: str, stream: IO[str] | None = None) -> str:
    """Draw a green success box with checkmark."""
    stream = stream if stream is not None else sys.stdout
    mark = glyph("✓", "[OK]", stream=stream)
    if not supports_color(stream):
        return f"{mark} {text}"
    return f"{paint(mark, 'green', 'bold')} {paint(text, 'green')}"


def info_box(text: str, stream: IO[str] | None = None) -> str:
    """Draw a blue info box with info symbol."""
    stream = stream if stream is not None else sys.stdout
    mark = glyph("ℹ", "[i]", stream=stream)
    if not supports_color(stream):
        return f"{mark} {text}"
    return f"{paint(mark, 'blue', 'bold')} {paint(text, 'blue')}"


def warning_box(text: str, stream: IO[str] | None = None) -> str:
    """Draw a yellow warning box with warning symbol."""
    stream = stream if stream is not None else sys.stdout
    mark = glyph("⚠", "[!]", stream=stream)
    if not supports_color(stream):
        return f"{mark} {text}"
    return f"{paint(mark, 'yellow', 'bold')} {paint(text, 'yellow')}"


def error_box(text: str, stream: IO[str] | None = None) -> str:
    """Draw a red error line with an error glyph — symmetric with success/info/warning_box."""
    stream = stream if stream is not None else sys.stdout
    mark = glyph("✗", "[x]", stream=stream)
    if not supports_color(stream):
        return f"{mark} {text}"
    return f"{paint(mark, 'red', 'bold')} {paint(text, 'red')}"


def tip(text: str, stream: IO[str] | None = None) -> str:
    """Inline hint line — dim body with a bold cyan lightbulb lead."""
    stream = stream if stream is not None else sys.stdout
    mark = glyph("💡", "tip:", stream=stream)
    if not supports_color(stream):
        return f"  {mark} {text}"
    return f"  {paint(mark, 'cyan', 'bold', stream=stream)} {dim(text, stream=stream)}"


def cta(action: str, description: str = "", stream: IO[str] | None = None) -> str:
    """Call-to-action row — bold primary `action` plus optional dim description."""
    stream = stream if stream is not None else sys.stdout
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
# Matches any SGR whose first parameter is `0` — both the canonical
# ``\x1b[0m`` reset and combined sequences like ``\x1b[0;1m`` (reset
# then bold). Used by `_inside_ansi_span` to count resets correctly.
_ANSI_RESET_RE = re.compile(r"\x1b\[0[;m]")
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


def colorize_markdown(text: str, stream: IO[str] | None = None) -> str:
    """Colorize markdown headers, priority badges, and `paths`.

    Returns the input unchanged when ``NO_COLOR`` is set or ``TERM=dumb``.
    Markdown markers (``**``, ``##``, backticks) are preserved so the output
    still works as a paste-into-Claude artifact even with colors on.
    """
    if not supports_color(stream):
        return text

    def _inside_ansi_span(full: str, pos: int) -> bool:
        """True when ``pos`` falls inside an unclosed SGR span.

        Counts SGR escapes preceding ``pos`` and compares openers vs
        resets. Any SGR whose first parameter is ``0`` is treated as a
        reset — that includes the canonical ``\\x1b[0m`` AND combined
        sequences like ``\\x1b[0;1m`` (reset then bold) common in copied
        terminal output. An odd opener count without a matching reset
        means the match is inside a painted region.
        """
        prefix = full[:pos]
        escapes = _ANSI_SGR_RE.findall(prefix)
        close_count = sum(1 for e in escapes if _ANSI_RESET_RE.match(e))
        open_count = len(escapes) - close_count
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
