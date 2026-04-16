"""Priority ranker — scores files by likelihood of containing issues.

Glasswing technique #2: rank targets before diving in.
Files handling user input, auth, external data, or crypto get highest priority.
Work top-down so agents spend time where it matters most.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

CONTENT_SCAN_LIMIT = 2000

# Priority 5 = most likely to have issues, 1 = least.
# All keywords are word-boundary matched; include explicit variants rather
# than prefix fragments so we don't accidentally match "keyword" for "key".
PRIORITY_KEYWORDS: dict[int, tuple[str, ...]] = {
    5: (
        "auth", "login", "password", "token", "session", "cookie",
        "oauth", "jwt", "credential", "secret", "cert",
        "api_key", "private_key", "passphrase", "hmac",
    ),
    4: (
        "input", "request", "upload", "form", "parse",
        "serialize", "deserialize", "deserializer", "deserialization",
        "admin", "permission", "role", "access",
    ),
    3: (
        "http", "api", "endpoint", "route", "handler", "middleware",
        "query", "sql", "database", "exec", "shell", "command",
        "subprocess",
    ),
    2: (
        "config", "setting", "env", "cache", "log", "error",
        "crypto", "encrypt", "decrypt", "hash", "sign",
    ),
    1: (
        "util", "helper", "constant", "schema",
        "test", "mock", "fixture",
    ),
}

SKIP_DIRS = frozenset({
    "__pycache__", "node_modules", ".git", ".venv", "venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
})

SKIP_EXTENSIONS = frozenset({
    ".pyc", ".pyo", ".so", ".dylib", ".lock", ".svg", ".png",
    ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf",
})


@dataclass(frozen=True)
class RankedFile:
    """A file with its computed priority score."""
    path: str
    priority: int
    reasons: tuple[str, ...]


# Pre-compiled word-boundary regexes per priority tier.
_COMPILED_KEYWORDS: dict[int, tuple[tuple[str, re.Pattern[str]], ...]] = {
    priority: tuple(
        (kw, re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE))
        for kw in keywords
    )
    for priority, keywords in PRIORITY_KEYWORDS.items()
}


def _score_file(path: str, content: str) -> tuple[int, tuple[str, ...]]:
    """Score a single file based on path and content keywords.

    Keywords use word-boundary matching so "key" does not match "keyword".
    Reasons are attributed only to the winning priority tier — lower-tier
    matches (e.g. "test" P1) are not mixed into a P5 file's reasons list.

    Only the first ``CONTENT_SCAN_LIMIT`` characters of content are scanned.
    """
    combined = f"{path} {content[:CONTENT_SCAN_LIMIT]}"
    matches_per_tier: dict[int, list[str]] = {}

    for priority, patterns in _COMPILED_KEYWORDS.items():
        tier_matches: list[str] = []
        for kw, pattern in patterns:
            if pattern.search(combined):
                tier_matches.append(kw)
        if tier_matches:
            matches_per_tier[priority] = tier_matches

    if not matches_per_tier:
        return 1, ()

    best_priority = max(matches_per_tier)
    winning_reasons = tuple(dict.fromkeys(matches_per_tier[best_priority]))
    return best_priority, winning_reasons


def rank_files(file_paths: list[str], read_fn: Callable[[str], str] | None = None) -> list[RankedFile]:
    """Rank files by vulnerability likelihood, highest priority first.

    Args:
        file_paths: List of file paths to rank.
        read_fn: Optional callable(path) -> str that returns file content.
                 If None, ranks by filename alone.

    Returns:
        A new list of RankedFile sorted by priority descending, then by path
        ascending for deterministic tie-breaking across platforms.
    """
    ranked: list[RankedFile] = []

    for fp in file_paths:
        p = Path(fp)

        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in SKIP_EXTENSIONS:
            continue

        content = ""
        if read_fn is not None:
            try:
                content = read_fn(fp)
            except (OSError, UnicodeDecodeError):
                content = ""

        priority, reasons = _score_file(fp, content)
        ranked.append(RankedFile(path=fp, priority=priority, reasons=reasons))

    return sorted(ranked, key=lambda r: (-r.priority, r.path))


def rank_to_prompt(ranked: list[RankedFile], top_n: int = 10) -> str:
    """Format ranked files into a prompt-ready priority list."""
    lines = ["## File Priority Ranking", ""]
    for i, rf in enumerate(ranked[:top_n], 1):
        reasons_str = ", ".join(rf.reasons) if rf.reasons else "general"
        lines.append(f"{i}. **P{rf.priority}** `{rf.path}` — {reasons_str}")
    if len(ranked) > top_n:
        lines.append(f"\n_(Showing top {top_n} of {len(ranked)} ranked files)_")
    return "\n".join(lines)
