"""Fence-escape helpers for embedding untrusted payloads in prompts."""

from __future__ import annotations

# Canonical line-break code points. ``str.splitlines()`` treats all of
# these as line breaks; markdown renderers and several LLM tokenizers
# also honor U+2028/U+2029/U+0085. Any one of them inside an inline
# backtick span or a fence info-line can split the surrounding context
# and leak untrusted content as instruction-like prose.
_LINEBREAK_TO_SPACE = (
    "\r\n",
    "\n",
    "\r",
    "\x0b",
    "\x0c",
    " ",
    " ",
    "\x85",
)
# Zero-width / invisible code points dropped entirely so a payload using
# them to smuggle invisible content past a downstream consumer leaves no
# trace.
_INVISIBLE_TO_DROP = (
    "\x00",
    "​",
    "‌",
    "‍",
    "﻿",
    "­",
)


def _strip_linebreaks(value: str) -> str:
    """Replace every canonical line-break with a space and drop invisibles.

    Single source of truth for the line-break / zero-width strip used by
    both :func:`sanitize_inline` and :func:`fence`'s ``safe_lang`` path.
    Keeping the list in one place prevents the two call sites from
    drifting out of sync.
    """
    for ch in _LINEBREAK_TO_SPACE:
        value = value.replace(ch, " ")
    for ch in _INVISIBLE_TO_DROP:
        value = value.replace(ch, "")
    return value


def sanitize_inline(value: str) -> str:
    """Neutralize markdown-fence breakers in a value rendered inline.

    Prompts embed user-controlled strings inside inline backtick spans
    (e.g. ``Target: `{target_dir}` ({file_types})`` in the advisor
    template, or ``- `{file_path}` (P{priority})`` in runner batch lists).
    A literal backtick closes the span early and leaks following text as
    instruction prose; an embedded newline collapses the surrounding
    sentence and can dump user-controlled content onto its own line where
    another ``{placeholder}`` might be reinterpreted. Swap backticks for
    typographic single quotes, then route through :func:`_strip_linebreaks`
    for the full canonical line-break / zero-width strip.

    Single source of truth for inline-span sanitization across the
    orchestrate package — :func:`fence` handles the fenced-block case;
    this handles the inline-span case.
    """
    return _strip_linebreaks(value.replace("`", "'"))


def fence(payload: str, *, lang: str = "") -> str:
    """Wrap ``payload`` in a code fence that the payload provably cannot escape.

    Picks the shortest fence of backticks (>=3) longer than the longest run of
    backticks inside ``payload``. Returns the complete fenced block including
    leading and trailing newlines. If ``lang`` is provided, it is placed after
    the opening fence.

    This defends against prompt-injection via fence collision: a payload
    containing ``` can no longer break out because the wrapper uses ```` or
    more. ``lang`` is routed through :func:`_strip_linebreaks` (after
    backticks are stripped) so an attacker can't break the opening fence
    header into two lines via VT/FF/LS/PS/NEL or smuggle zero-width
    content through the info-line.
    """
    longest = 0
    run = 0
    for ch in payload:
        if ch == "`":
            run += 1
            if run > longest:
                longest = run
        else:
            run = 0
    fence_len = max(3, longest + 1)
    bar = "`" * fence_len
    safe_lang = _strip_linebreaks(lang.replace("`", ""))
    return f"{bar}{safe_lang}\n{payload}\n{bar}"
