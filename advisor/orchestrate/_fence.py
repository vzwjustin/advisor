"""Fence-escape helper for embedding untrusted payloads in prompts."""

from __future__ import annotations


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
    return f"{bar}{lang}\n{payload}\n{bar}"
