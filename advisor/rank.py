"""Priority ranker — scores files by likelihood of containing issues.

Rank targets before diving in.
Files handling user input, auth, external data, or crypto get highest priority.
Work top-down so agents spend time where it matters most.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePath

# Bytes scanned per file for keyword matching — covers typical import block + first function/class.
CONTENT_SCAN_LIMIT = 2000

# Priority 5 = most likely to have issues, 1 = least.
# All keywords are word-boundary matched; include explicit variants rather
# than prefix fragments so we don't accidentally match "keyword" for "key".
PRIORITY_KEYWORDS: dict[int, tuple[str, ...]] = {
    5: (
        "auth",
        "login",
        "password",
        "token",
        "session",
        "cookie",
        "oauth",
        "jwt",
        "credential",
        "secret",
        "cert",
        "api_key",
        "private_key",
        "passphrase",
        "hmac",
    ),
    4: (
        "input",
        "request",
        "upload",
        "form",
        "parse",
        "serialize",
        "deserialize",
        "deserializer",
        "deserialization",
        "admin",
        "permission",
        "role",
        "access",
    ),
    3: (
        "http",
        "api",
        "endpoint",
        "route",
        "handler",
        "middleware",
        "query",
        "sql",
        "database",
        "exec",
        "shell",
        "command",
        "subprocess",
    ),
    2: (
        "config",
        "setting",
        "env",
        "cache",
        "log",
        "error",
        "crypto",
        "encrypt",
        "decrypt",
        "hash",
        "sign",
    ),
    1: (
        "util",
        "helper",
        "constant",
        "schema",
        "test",
        "mock",
        "fixture",
    ),
}

SKIP_DIRS = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".git",
        ".venv",
        "venv",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
    }
)

SKIP_EXTENSIONS = frozenset(
    {
        ".pyc",
        ".pyo",
        ".so",
        ".dylib",
        ".lock",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
    }
)

ADVISORIGNORE_FILENAME = ".advisorignore"


@dataclass(frozen=True, slots=True)
class RankedFile:
    """A file with its computed priority score."""

    path: str
    priority: int
    reasons: tuple[str, ...]


def load_advisorignore(base_dir: str | Path) -> list[str]:
    """Load ignore patterns from .advisorignore file if it exists.

    Args:
        base_dir: Directory to look for .advisorignore file.

    Returns:
        List of glob patterns from the file, empty list if file doesn't exist.
        Comments (lines starting with #) and blank lines are ignored.

    A malformed or unreadable file (permission error, invalid UTF-8, etc.)
    emits a ``UserWarning`` and returns ``[]`` — silent-fallback could cause
    files the user intended to skip to be reviewed. A missing file is
    expected and returns ``[]`` without warning.
    """
    path = Path(base_dir) / ADVISORIGNORE_FILENAME
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        import warnings

        warnings.warn(
            f"could not read {path}: {exc}; treating as no ignore patterns",
            UserWarning,
            stacklevel=2,
        )
        return []
    patterns = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            patterns.append(stripped)
    return patterns


def _double_star_to_regex(pattern: str) -> re.Pattern[str]:
    """Translate a glob pattern with ``**`` into a regex that matches the
    whole path. ``**`` matches any number of path components (including
    zero), ``*`` matches anything except ``/``, and ``?`` matches a single
    non-``/`` char. Follows standard glob semantics — ``src/**/*.py``
    matches ``src/foo.py`` *and* ``src/a/b/foo.py``.

    Python <3.13 has no ``PurePath.full_match``; ``PurePath.match`` treats
    ``**`` as a single component, so we build the regex ourselves.
    """
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            # `**` (possibly followed by `/`) → match any path including sep
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                # consume optional trailing `/` so `**/x` matches `x` too
                if i + 2 < len(pattern) and pattern[i + 2] == "/":
                    parts.append("(?:.*/)?")
                    i += 3
                else:
                    parts.append(".*")
                    i += 2
            else:
                parts.append("[^/]*")
                i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        elif c == "[":
            end = pattern.find("]", i)
            if end == -1:
                parts.append(re.escape(c))
                i += 1
            else:
                parts.append(pattern[i : end + 1])
                i = end + 1
        else:
            parts.append(re.escape(c))
            i += 1
    return re.compile("^" + "".join(parts) + "$")


def _matches_any_pattern(file_path: str, patterns: list[str]) -> bool:
    """Check if a file path matches any glob pattern.

    Supports:
    - fnmatch-style wildcards (``*``, ``?``, ``[seq]``) on a single path
      component (filename match)
    - ``**`` recursive wildcards (via ``PurePath.match`` + full-path fallback)
    - directory-specific patterns (ending with ``/``) match if any path
      component equals the pattern
    - bare-word patterns (no glob metacharacters) match any path component
    """
    if not patterns:
        return False
    path = PurePath(file_path)
    path_str = str(path)
    name = path.name
    for pattern in patterns:
        # Pattern ending with / matches directories only
        if pattern.endswith("/"):
            dir_pattern = pattern.rstrip("/")
            if any(fnmatch.fnmatch(part, dir_pattern) for part in path.parts):
                return True
            continue
        # ``**`` recursive glob — PurePath.match treats ``**`` as a single
        # component on Python <3.13, so translate to a regex ourselves.
        if "**" in pattern:
            try:
                if _double_star_to_regex(pattern).match(path_str):
                    return True
            except re.error:
                # Malformed regex translation; fall through to other strategies
                pass
            continue
        # Match against filename only
        if fnmatch.fnmatch(name, pattern):
            return True
        # Match against full path
        if fnmatch.fnmatch(path_str, pattern):
            return True
        # Match against any path component for bare-word patterns only (no glob metacharacters).
        # Wildcard patterns like *.py are excluded to avoid matching directory components
        # with dotted names (e.g., scripts.py/) when only filename matching was intended.
        if not any(c in pattern for c in "*?[") and any(
            fnmatch.fnmatch(part, pattern.rstrip("/")) for part in path.parts
        ):
            return True
    return False


# Pre-compiled word-boundary regexes per priority tier.
_COMPILED_KEYWORDS: dict[int, tuple[tuple[str, re.Pattern[str]], ...]] = {
    priority: tuple((kw, re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)) for kw in keywords)
    for priority, keywords in PRIORITY_KEYWORDS.items()
}


# Single combined regex spanning every keyword across every tier. Named
# groups encode ``(priority, keyword_index)`` so one ``re.finditer`` pass
# replaces ~50 separate ``pattern.search`` calls per file — a meaningful
# speedup on large codebases (thousands of files scanned per run). The
# per-tier dict above is still useful for unit tests that want to verify
# individual keyword patterns compile correctly.
def _build_combined() -> tuple[re.Pattern[str], dict[str, tuple[int, str]]]:
    parts: list[str] = []
    mapping: dict[str, tuple[int, str]] = {}
    for priority, keywords in PRIORITY_KEYWORDS.items():
        for idx, kw in enumerate(keywords):
            group = f"p{priority}_{idx}"
            parts.append(rf"(?P<{group}>\b{re.escape(kw)}\b)")
            mapping[group] = (priority, kw)
    return re.compile("|".join(parts), re.IGNORECASE), mapping


_COMBINED_KEYWORD_RE, _COMBINED_GROUP_MAP = _build_combined()


def _score_file(path: str, content: str) -> tuple[int, tuple[str, ...]]:
    """Score a single file based on path and content keywords.

    Keywords use word-boundary matching so "key" does not match "keyword".
    Reasons are attributed only to the winning priority tier — lower-tier
    matches (e.g. "test" P1) are not mixed into a P5 file's reasons list.

    Only the first ``CONTENT_SCAN_LIMIT`` characters of content are scanned.
    """
    combined = f"{path} {content[:CONTENT_SCAN_LIMIT]}"
    matches_per_tier: dict[int, list[str]] = {}

    for m in _COMBINED_KEYWORD_RE.finditer(combined):
        group = m.lastgroup
        if group is None:
            continue
        priority, kw = _COMBINED_GROUP_MAP[group]
        matches_per_tier.setdefault(priority, []).append(kw)

    if not matches_per_tier:
        return 1, ()

    best_priority = max(matches_per_tier)
    winning_reasons = tuple(dict.fromkeys(matches_per_tier[best_priority]))
    return best_priority, winning_reasons


def rank_files(
    file_paths: list[str],
    read_fn: Callable[[str], str] | None = None,
    ignore_patterns: list[str] | None = None,
) -> list[RankedFile]:
    """Rank files by vulnerability likelihood, highest priority first.

    Args:
        file_paths: List of file paths to rank.
        read_fn: Optional callable(path) -> str that returns file content.
                 If None, ranks by filename alone.
        ignore_patterns: Optional list of glob patterns to skip. Use
                        load_advisorignore() to get patterns from file.

    Returns:
        A new list of RankedFile sorted by priority descending, then by path
        ascending for deterministic tie-breaking across platforms.
    """
    ranked: list[RankedFile] = []
    patterns = ignore_patterns or []

    for fp in file_paths:
        p = Path(fp)

        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in SKIP_EXTENSIONS:
            continue
        if _matches_any_pattern(fp, patterns):
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
