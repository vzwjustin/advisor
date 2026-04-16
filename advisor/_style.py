"""Tiny terminal-styling helpers — ANSI only when stdout is a TTY.

Respects ``NO_COLOR`` (https://no-color.org). When colors are off, every
helper returns the input string unchanged so output stays byte-identical
for pipes, tests, and dumb terminals.
"""

from __future__ import annotations

import os
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
