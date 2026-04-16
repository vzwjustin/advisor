"""Tiny terminal-styling helpers — ANSI only when stdout is a TTY.

Respects ``NO_COLOR`` (https://no-color.org). When colors are off, every
helper returns the input string unchanged so output stays byte-identical
for pipes, tests, and dumb terminals.
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
    if "NO_COLOR" in os.environ:
        return False
    s = stream if stream is not None else sys.stdout
    return bool(getattr(s, "isatty", lambda: False)())


def paint(text: str, *styles: str, stream: IO[str] | None = None) -> str:
    if not styles or not supports_color(stream):
        return text
    prefix = "".join(_CODES[s] for s in styles if s in _CODES)
    return f"{prefix}{text}{_RESET}" if prefix else text


def glyph(fancy: str, plain: str, stream: IO[str] | None = None) -> str:
    """Use a Unicode glyph on color-capable TTYs, ASCII fallback elsewhere."""
    return fancy if supports_color(stream) else plain


def err(text: str) -> str:
    return paint(text, "red", "bold", stream=sys.stderr)


def ok(text: str) -> str:
    return paint(text, "green", stream=sys.stdout)


def dim(text: str, stream: IO[str] | None = None) -> str:
    return paint(text, "dim", stream=stream)


# Priority badge colors — high-contrast on both light and dark terminals.
_PRIORITY_STYLES: dict[str, tuple[str, ...]] = {
    "5": ("red", "bold"),
    "4": ("magenta", "bold"),
    "3": ("yellow", "bold"),
    "2": ("cyan",),
    "1": ("dim",),
}

_PRIORITY_BOLD_RE = re.compile(r"\*\*P([1-5])\*\*")
_PRIORITY_BARE_RE = re.compile(r"(?<![A-Za-z0-9*\x1b])P([1-5])(?![A-Za-z0-9*])")
_H2_RE = re.compile(r"^(##\s+)(.+)$", re.MULTILINE)
_H3_RE = re.compile(r"^(###\s+)(.+)$", re.MULTILINE)
_H4_RE = re.compile(r"^(####\s+)(.+)$", re.MULTILINE)
_BACKTICK_RE = re.compile(r"`([^`\n]+)`")


def colorize_markdown(text: str, stream: IO[str] | None = None) -> str:
    """Colorize markdown headers, priority badges, and `paths` for TTY.

    Returns the input unchanged when stdout is not a TTY or NO_COLOR is set.
    Markdown markers (``**``, ``##``, backticks) are preserved so the output
    still works as a paste-into-Claude artifact when colors happen to be on.
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
