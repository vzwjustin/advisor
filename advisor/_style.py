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


def supports_color(stream: IO[str] | None = None) -> bool:
    """Return True when ANSI styling should be emitted.

    The ``stream`` argument is reserved — it's threaded through by every
    helper so a future per-stream policy (e.g. respect a NO_COLOR env-var
    set on a captured `io.StringIO`) can be added without touching call
    sites. Today only process-wide env vars are consulted.
    """
    del stream  # reserved for per-stream policy
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


def paint(text: str, *styles: str, stream: IO[str] | None = None) -> str:
    if not styles or not supports_color(stream):
        return text
    prefix = "".join(_CODES[s] for s in styles if s in _CODES)
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


def spinner_frame(i: int) -> str:
    """Return a spinner character for the given frame index.

    ADHD-friendly progress indicator. Use in a loop to show activity.
    Example: for i in range(100): print(f"\\r{spinner_frame(i)} Working...", end="")
    """
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    return paint(frames[i % len(frames)], "cyan", "bold")


def banner(text: str, width: int = 50, stream: IO[str] | None = None) -> str:
    """Create a visual banner with box-drawing characters.

    Helps with visual scanning and adds clear hierarchy to CLI output.
    """
    if not supports_color(stream):
        return f"== {text} =="
    line = "━" * width
    centered = text.center(width - 4)
    return f"{paint('┏', 'cyan')}{paint(line, 'cyan')}{paint('┓', 'cyan')}\n" \
           f"{paint('┃', 'cyan')}  {paint(centered, 'bold')}  {paint('┃', 'cyan')}\n" \
           f"{paint('┗', 'cyan')}{paint(line, 'cyan')}{paint('┛', 'cyan')}"


def success_box(text: str, stream: IO[str] | None = None) -> str:
    """Draw a green success box with checkmark."""
    mark = glyph("✓", "[OK]")
    if not supports_color(stream):
        return f"{mark} {text}"
    return f"{paint(mark, 'green', 'bold')} {paint(text, 'green')}"


def info_box(text: str, stream: IO[str] | None = None) -> str:
    """Draw a blue info box with info symbol."""
    mark = glyph("ℹ", "[i]")
    if not supports_color(stream):
        return f"{mark} {text}"
    return f"{paint(mark, 'blue', 'bold')} {paint(text, 'blue')}"


def warning_box(text: str, stream: IO[str] | None = None) -> str:
    """Draw a yellow warning box with warning symbol."""
    mark = glyph("⚠", "[!]")
    if not supports_color(stream):
        return f"{mark} {text}"
    return f"{paint(mark, 'yellow', 'bold')} {paint(text, 'yellow')}"


def tip(text: str, stream: IO[str] | None = None) -> str:
    """Inline hint line — dim body with a bold cyan lightbulb lead."""
    mark = glyph("💡", "tip:")
    if not supports_color(stream):
        return f"  {mark} {text}"
    return f"  {paint(mark, 'cyan', 'bold', stream=stream)} {dim(text, stream=stream)}"


def cta(action: str, description: str = "", stream: IO[str] | None = None) -> str:
    """Call-to-action row — bold primary `action` plus optional dim description."""
    bullet = glyph("→", ">")
    if not supports_color(stream):
        sep = "  " if description else ""
        return f"  {bullet} {action}{sep}{description}"
    lead = paint(bullet, "cyan", "bold", stream=stream)
    act = paint(action, "bold", stream=stream)
    if not description:
        return f"  {lead} {act}"
    return f"  {lead} {act}  {dim(description, stream=stream)}"


# Priority badge colors — high-contrast on both light and dark terminals.
_PRIORITY_STYLES: dict[str, tuple[str, ...]] = {
    "5": ("red", "bold"),
    "4": ("magenta", "bold"),
    "3": ("yellow", "bold"),
    "2": ("cyan", "bold"),
    "1": ("dim",),
}

_PRIORITY_BOLD_RE = re.compile(r"\*\*P([1-5])\*\*")
_PRIORITY_BARE_RE = re.compile(r"(?<![A-Za-z0-9*\x1b])P([1-5])(?![A-Za-z0-9*])")
_H2_RE = re.compile(r"^(##\s+)(.+)$", re.MULTILINE)
_H3_RE = re.compile(r"^(###\s+)(.+)$", re.MULTILINE)
_H4_RE = re.compile(r"^(####\s+)(.+)$", re.MULTILINE)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")


def colorize_markdown(text: str, stream: IO[str] | None = None) -> str:
    """Colorize markdown headers, priority badges, and `paths`.

    Returns the input unchanged when ``NO_COLOR`` is set or ``TERM=dumb``.
    Markdown markers (``**``, ``##``, backticks) are preserved so the output
    still works as a paste-into-Claude artifact even with colors on.
    """
    if not supports_color(stream):
        return text

    def _color_priority_bold(m: re.Match[str]) -> str:
        styles = _PRIORITY_STYLES[m.group(1)]
        return paint(f"**P{m.group(1)}**", *styles, stream=stream)

    def _color_priority_bare(m: re.Match[str]) -> str:
        styles = _PRIORITY_STYLES[m.group(1)]
        return paint(f"P{m.group(1)}", *styles, stream=stream)

    def _color_h2(m: re.Match[str]) -> str:
        return m.group(1) + paint(m.group(2), "cyan", "bold", stream=stream)

    def _color_h3(m: re.Match[str]) -> str:
        return m.group(1) + paint(m.group(2), "blue", "bold", stream=stream)

    def _color_h4(m: re.Match[str]) -> str:
        return m.group(1) + paint(m.group(2), "bold", stream=stream)

    def _color_path(m: re.Match[str]) -> str:
        return "`" + paint(m.group(1), "green", stream=stream) + "`"

    text = _PRIORITY_BOLD_RE.sub(_color_priority_bold, text)
    text = _PRIORITY_BARE_RE.sub(_color_priority_bare, text)
    text = _H4_RE.sub(_color_h4, text)
    text = _H3_RE.sub(_color_h3, text)
    text = _H2_RE.sub(_color_h2, text)
    text = _BACKTICK_RE.sub(_color_path, text)
    return text
