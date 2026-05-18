"""Fence-escape helpers for embedding untrusted payloads in prompts."""

from __future__ import annotations


def sanitize_inline(value: str) -> str:
    """Neutralize markdown-fence breakers in a value rendered inline.

    Prompts embed user-controlled strings inside inline backtick spans
    (e.g. ``Target: `{target_dir}` ({file_types})`` in the advisor
    template, or ``- `{file_path}` (P{priority})`` in runner batch lists).
    A literal backtick closes the span early and leaks following text as
    instruction prose; an embedded newline collapses the surrounding
    sentence and can dump user-controlled content onto its own line where
    another ``{placeholder}`` might be reinterpreted. Swap backticks for
    typographic single quotes and replace CR/LF with a space.

    Also strips the five non-LF/CR characters that ``str.splitlines()``
    treats as line breaks — U+000B (VT), U+000C (FF), U+0085 (NEXT
    LINE), U+2028 (LINE SEPARATOR), and U+2029 (PARAGRAPH SEPARATOR) —
    so a payload containing those cannot escape the inline span on
    renderers that honor them. NUL (U+0000) and the zero-width code
    points U+200B / U+200C / U+200D / U+FEFF / U+00AD are dropped
    entirely so a payload using them to smuggle invisible content past
    a downstream consumer leaves no trace.

    Single source of truth for inline-span sanitization across the
    orchestrate package — :func:`fence` handles the fenced-block case;
    this handles the inline-span case.
    """
    return (
        value.replace("`", "'")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("\x0b", " ")
        .replace("\x0c", " ")
        .replace("\u2028", " ")
        .replace("\u2029", " ")
        .replace("\x85", " ")
        .replace("\x00", "")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
        .replace("\u00ad", "")
    )


def fence(payload: str, *, lang: str = "") -> str:
    """Wrap ``payload`` in a code fence that the payload provably cannot escape.

    Picks the shortest fence of backticks (>=3) longer than the longest run of
    backticks inside ``payload``. Returns the complete fenced block including
    leading and trailing newlines. If ``lang`` is provided, it is placed after
    the opening fence.

    This defends against prompt-injection via fence collision: a payload
    containing ``` can no longer break out because the wrapper uses ```` or
    more.
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
    # Strip CR/LF and the other ``str.splitlines()`` line-break chars
    # (VT, FF) from ``lang`` so a caller passing untrusted text can't
    # break the opening fence header into two lines and inject content
    # as markdown above the fenced payload. Matches ``sanitize_inline``.
    safe_lang = (
        lang.replace("`", "")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
        .replace("\x0b", " ")
        .replace("\x0c", " ")
    )
    return f"{bar}{safe_lang}\n{payload}\n{bar}"
