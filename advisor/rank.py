"""Priority ranker — scores files by likelihood of containing issues.

Rank targets before diving in.
Files handling user input, auth, external data, or crypto get highest priority.
Work top-down so agents spend time where it matters most.

## Language-aware scoring

The base :data:`PRIORITY_KEYWORDS` table is language-agnostic (``auth``,
``token``, ``sql``, etc. match in any codebase). Additional ecosystem-specific
terms live in :data:`LANGUAGE_EXTRA_KEYWORDS` — keyed by canonical language name
(``python``, ``javascript``, ``go``, ``rust``, ``java``, ``ruby``, ``php``).
:func:`_score_file` looks up the language from the file extension via
:data:`EXTENSION_LANGUAGE` and uses a combined regex that covers both the base
terms and the language's extras. Files in unrecognized languages score against
the base set only.
"""

from __future__ import annotations

import fnmatch
import re
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
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

# Language-specific additional keywords layered on top of ``PRIORITY_KEYWORDS``.
# Keep each list tight: terms that are *diagnostic* of the language's risk
# surface, not every ecosystem name. Adding too many low-value keywords
# dilutes the ranking.
LANGUAGE_EXTRA_KEYWORDS: dict[str, dict[int, tuple[str, ...]]] = {
    "python": {
        5: ("passlib", "pyjwt", "itsdangerous"),
        4: ("pickle", "loads", "yaml.load", "marshal", "pydantic"),
        3: ("flask", "django", "fastapi", "sqlalchemy", "psycopg", "pymongo"),
        2: ("os.environ", "secrets"),
    },
    "javascript": {
        5: ("passport", "next-auth", "nextauth", "firebase.auth"),
        4: (
            "innerhtml",
            "dangerouslysetinnerhtml",
            "eval",
            "document.write",
            "localstorage",
            "sessionstorage",
        ),
        3: ("express", "fastify", "nextjs", "next.js", "graphql", "prisma", "mongoose"),
        2: ("dotenv", "process.env"),
    },
    "go": {
        5: ("crypto/tls", "crypto/x509", "golang.org/x/oauth2"),
        4: ("encoding/json", "encoding/xml", "encoding/gob", "html/template"),
        3: ("net/http", "database/sql", "os/exec", "context.background"),
        2: ("os.getenv",),
    },
    "rust": {
        5: ("jsonwebtoken", "argon2", "oauth2"),
        4: ("serde_json", "serde_yaml", "unsafe", "transmute", "from_utf8_unchecked"),
        3: ("reqwest", "actix_web", "axum", "rocket", "tokio", "sqlx", "diesel"),
        2: ("std::env",),
    },
    "java": {
        5: ("spring.security", "shiro", "jjwt", "keycloak"),
        4: ("objectinputstream", "readobject", "xmldecoder", "jackson"),
        3: (
            "restcontroller",
            "requestmapping",
            "httpservletrequest",
            "preparedstatement",
            "runtime.getruntime",
        ),
        2: ("system.getenv",),
    },
    "ruby": {
        5: ("devise", "omniauth", "warden"),
        4: ("params", "marshal.load", "yaml.load"),
        3: ("rails", "rack", "sinatra", "activerecord"),
    },
    "php": {
        5: ("password_hash", "password_verify"),
        4: ("$_get", "$_post", "$_request", "$_files", "unserialize"),
        3: ("mysqli", "pdo", "wp_", "laravel", "symfony"),
    },
}

# File-extension → canonical language name. The canonical name must appear
# in :data:`LANGUAGE_EXTRA_KEYWORDS` to get ecosystem-specific scoring;
# extensions mapped to an unknown language silently fall back to the base
# set.
EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "javascript",
    ".tsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "java",
    ".scala": "java",
    ".rb": "ruby",
    ".rake": "ruby",
    ".php": "php",
}


def language_for_path(path: str) -> str | None:
    """Return canonical language name for a file path, or ``None``.

    Looks up the file extension (including multi-dot forms like ``.d.ts``
    falling back to ``.ts``) in :data:`EXTENSION_LANGUAGE`.
    """
    suffix = Path(path).suffix.lower()
    return EXTENSION_LANGUAGE.get(suffix)


# Shebang interpreter → canonical language name. Used as a fallback when
# a file has no recognized extension (common for CLI scripts like
# ``/usr/local/bin/deploy`` with ``#!/usr/bin/env python3``). Only the
# interpreter basename is examined, so ``python3.12``, ``python``, and
# ``/opt/python/bin/python`` all resolve identically.
_SHEBANG_INTERPRETERS: dict[str, str] = {
    "python": "python",
    "python2": "python",
    "python3": "python",
    "node": "javascript",
    "deno": "javascript",
    "bun": "javascript",
    "ruby": "ruby",
    "php": "php",
}


def _language_from_shebang(first_line: str) -> str | None:
    """Extract a canonical language from a ``#!...`` line, or ``None``.

    Handles the common forms:
        ``#!/usr/bin/python3``            → ``python``
        ``#!/usr/bin/env python3``        → ``python``
        ``#!/usr/bin/env -S python3 -u``  → ``python``
    Unrecognized interpreters return ``None`` so callers fall back to
    the base (language-less) keyword scoring.
    """
    first_line = first_line.lstrip("﻿")
    if not first_line.startswith("#!"):
        return None
    tokens = first_line[2:].strip().split()
    if not tokens:
        return None
    # ``env`` forms: pick the first non-flag argument after ``env``.
    first = tokens[0].rsplit("/", 1)[-1]
    if first == "env":
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            first = tok.rsplit("/", 1)[-1]
            break
        else:
            return None
    # Strip version suffixes like ``python3.12`` → ``python3``.
    base = first.split(".", 1)[0]
    return _SHEBANG_INTERPRETERS.get(base) or _SHEBANG_INTERPRETERS.get(first)


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
        ".next",
        ".nuxt",
        "target",  # rust/java
        "vendor",  # go/ruby/php
        ".bundle",
        ".gradle",
        ".idea",
        ".vscode",
        "coverage",
        "htmlcov",
        ".turbo",
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
        ".class",
        ".jar",
        ".o",
        ".a",
        ".dll",
        ".exe",
        ".map",
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
            if stripped.startswith("!"):
                warnings.warn(
                    f"{path}: negation pattern {stripped!r} is not supported and will be ignored",
                    UserWarning,
                    stacklevel=2,
                )
                continue
            if stripped.startswith("/"):
                warnings.warn(
                    f"{path}: anchored pattern {stripped!r} is not fully supported — "
                    "matching will behave as if the leading '/' were absent (unanchored)",
                    UserWarning,
                    stacklevel=2,
                )
            patterns.append(stripped.lstrip("/"))
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
                body = pattern[i + 1 : end]
                if not body:
                    parts.append(re.escape("["))
                    i += 1
                else:
                    if body.startswith("!"):
                        body = "^" + body[1:]
                    if body in ("^", ""):
                        # Empty char class or negation-only — treat as literal
                        # to avoid compiling ``[^]`` (a regex error).
                        parts.append(re.escape(pattern[i : end + 1]))
                    else:
                        parts.append("[" + body + "]")
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
    # Normalize path separators: glob patterns always use ``/`` (including
    # user-authored ``.advisorignore`` entries), but ``str(PurePath)`` uses
    # the OS separator — ``\`` on Windows. Without normalization,
    # ``src/**/*.py`` compiled to a regex using ``/`` would never match
    # ``src\a\b\c.py``. Normalize once, apply everywhere.
    path_str = path.as_posix()
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
        # Match against any path component for bare-word patterns only
        # (no glob metacharacters AND no `.`). Filename-shaped patterns
        # like `foo.py` are excluded — they should match via the filename
        # or full-path strategies above, not as directory components,
        # otherwise a single dir named `foo.py/` would shadow every file
        # beneath it.
        if not any(c in pattern for c in "*?[.") and any(
            fnmatch.fnmatch(part, pattern.rstrip("/")) for part in path.parts
        ):
            return True
    return False


def _merged_keywords_for(language: str | None) -> dict[int, tuple[str, ...]]:
    """Return merged keyword table for a language (base + language extras)."""
    if not language or language not in LANGUAGE_EXTRA_KEYWORDS:
        return PRIORITY_KEYWORDS
    extras = LANGUAGE_EXTRA_KEYWORDS[language]
    merged: dict[int, tuple[str, ...]] = {}
    for priority, kws in PRIORITY_KEYWORDS.items():
        extra = extras.get(priority, ())
        # Deduplicate while preserving order (base first, extras second)
        seen: dict[str, None] = {}
        for kw in (*kws, *extra):
            seen.setdefault(kw, None)
        merged[priority] = tuple(seen)
    # Also include any priority the language added that wasn't in base
    for priority, extra in extras.items():
        if priority not in merged:
            merged[priority] = tuple(dict.fromkeys(extra))
    return merged


def _regex_with_extras(
    language: str | None,
    extras: dict[int, tuple[str, ...]],
) -> tuple[re.Pattern[str], dict[str, tuple[int, str]]]:
    """Build a one-shot regex that overlays ``extras`` on top of the language baseline.

    Memoized via :func:`_regex_with_extras_cached` keyed on a hashable
    snapshot of ``extras``. Without caching, a preset run with N files
    triggered N identical regex compilations.
    """
    extras_key = tuple(sorted((p, kws) for p, kws in extras.items()))
    return _regex_with_extras_cached(language, extras_key)


@lru_cache(maxsize=16)
def _regex_with_extras_cached(
    language: str | None,
    extras_key: tuple[tuple[int, tuple[str, ...]], ...],
) -> tuple[re.Pattern[str], dict[str, tuple[int, str]]]:
    extras = dict(extras_key)
    base = _merged_keywords_for(language)
    merged: dict[int, tuple[str, ...]] = {}
    for priority, kws in base.items():
        extra = extras.get(priority, ())
        seen: dict[str, None] = {}
        for kw in (*kws, *extra):
            seen.setdefault(kw, None)
        merged[priority] = tuple(seen)
    for priority, extra in extras.items():
        if priority not in merged:
            merged[priority] = tuple(dict.fromkeys(extra))

    parts: list[str] = []
    mapping: dict[str, tuple[int, str]] = {}
    for priority, kws in merged.items():
        for idx, kw in enumerate(kws):
            group = f"p{priority}_{idx}"
            parts.append(rf"(?P<{group}>\b{re.escape(kw)}\b)")
            mapping[group] = (priority, kw)
    if not parts:
        return re.compile(r"(?!)"), mapping
    return re.compile("|".join(parts), re.IGNORECASE), mapping


@lru_cache(maxsize=16)
def _combined_regex_for(language: str | None) -> tuple[re.Pattern[str], dict[str, tuple[int, str]]]:
    """Build and cache a combined regex + group-map for a language.

    Named groups encode ``(priority, keyword_index)`` so one
    ``re.finditer`` pass replaces dozens of separate ``pattern.search``
    calls per file. Cached per language — each language's keyword set is
    built at most once per process.
    """
    keywords = _merged_keywords_for(language)
    parts: list[str] = []
    mapping: dict[str, tuple[int, str]] = {}
    for priority, kws in keywords.items():
        for idx, kw in enumerate(kws):
            group = f"p{priority}_{idx}"
            parts.append(rf"(?P<{group}>\b{re.escape(kw)}\b)")
            mapping[group] = (priority, kw)
    if not parts:
        return re.compile(r"(?!)"), mapping
    return re.compile("|".join(parts), re.IGNORECASE), mapping


# Back-compat export: the module used to expose a precomputed base regex as
# ``_COMBINED_KEYWORD_RE`` / ``_COMBINED_GROUP_MAP``. Importers (tests, etc.)
# get a lazily-initialized reference so merely importing :mod:`advisor.rank`
# doesn't force the regex to compile when the caller (e.g. ``advisor
# --version``) never ranks a file.
def __getattr__(name: str) -> object:
    if name == "_COMBINED_KEYWORD_RE":
        return _combined_regex_for(None)[0]
    if name == "_COMBINED_GROUP_MAP":
        return _combined_regex_for(None)[1]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _score_file(
    path: str,
    content: str,
    *,
    extra_keywords: dict[int, tuple[str, ...]] | None = None,
) -> tuple[int, tuple[str, ...]]:
    """Score a single file based on path and content keywords.

    Keywords use word-boundary matching so "key" does not match "keyword".
    Reasons are attributed only to the winning priority tier — lower-tier
    matches (e.g. "test" P1) are not mixed into a P5 file's reasons list.

    Only the first ``CONTENT_SCAN_LIMIT`` characters of content are scanned.
    The language is detected from the file extension and augments the base
    keyword set with ecosystem-specific terms when available. When the
    extension is unrecognized, the content's first line is inspected for
    a ``#!`` shebang — so a file named ``deploy`` with
    ``#!/usr/bin/env python3`` still gets Python-specific scoring.

    ``extra_keywords`` layers on top of the language baseline — used by
    rule-pack presets to add ecosystem-framework terms (e.g. ``csrf``,
    ``jsonwebtoken``) without hard-coding them in :data:`PRIORITY_KEYWORDS`.
    """
    language = language_for_path(path)
    if language is None and content:
        first_newline = content.find("\n")
        first_line = content[:first_newline] if first_newline != -1 else content
        language = _language_from_shebang(first_line)
    if extra_keywords:
        regex, group_map = _regex_with_extras(language, extra_keywords)
    else:
        regex, group_map = _combined_regex_for(language)
    combined = f"{path} {content[:CONTENT_SCAN_LIMIT]}"
    matches_per_tier: dict[int, list[str]] = {}

    for m in regex.finditer(combined):
        group = m.lastgroup
        if group is None:
            continue
        priority, kw = group_map[group]
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
    *,
    max_workers: int | None = None,
    extra_keywords: dict[int, tuple[str, ...]] | None = None,
    history_scores: dict[str, float] | None = None,
    history_counts: dict[str, int] | None = None,
    history_window_days: int = 90,
) -> list[RankedFile]:
    """Rank files by vulnerability likelihood, highest priority first.

    Args:
        file_paths: List of file paths to rank.
        read_fn: Optional callable(path) -> str that returns file content.
                 If None, ranks by filename alone.
        ignore_patterns: Optional list of glob patterns to skip. Use
                        load_advisorignore() to get patterns from file.
        max_workers: Thread-pool size for ``read_fn`` I/O. ``None`` (default)
                    picks ``min(32, os.cpu_count() or 4) * 4`` — enough to
                    saturate SSD read queues without swamping small VMs.
                    Set to ``1`` to disable parallelism entirely (handy for
                    deterministic tests or debugging).
        extra_keywords: Optional per-tier keyword overlay (e.g. from a
                    :class:`~advisor.presets.RulePack`). Layered on top of
                    the language-aware baseline. Each tier's extras are
                    deduped while preserving order.
        history_scores: Optional per-file repeat-offender scores (see
                    :func:`advisor.history.file_repeat_scores`). Bumps
                    a file's priority by **at most +1 tier** — a P3
                    file with a high history score becomes P4 but never
                    leaps to P5 from history alone. When a boost is
                    applied, ``"repeat offender"`` is appended to the
                    file's reasons list.

    Returns:
        A new list of RankedFile sorted by priority descending, then by path
        ascending for deterministic tie-breaking across platforms.

    File reads run in a thread pool when ``read_fn`` is provided — scoring
    itself is pure CPU (regex matching) and stays on the caller's thread.
    For small repos (< ~20 files) the pool is skipped to avoid its
    startup overhead.
    """
    patterns = ignore_patterns or []

    # Skip directory / extension / ignore-pattern filters first so we
    # don't pay for reading a file we'll drop anyway.
    kept_paths: list[str] = []
    for fp in file_paths:
        p = Path(fp)
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.suffix in SKIP_EXTENSIONS or any(
            p.name.endswith(s) for s in (".min.js", ".min.mjs", ".min.cjs", ".min.css")
        ):
            continue
        if _matches_any_pattern(fp, patterns):
            continue
        kept_paths.append(fp)

    if not kept_paths:
        return []

    contents = _read_contents_parallel(kept_paths, read_fn, max_workers)

    ranked: list[RankedFile] = []
    for fp, content in zip(kept_paths, contents, strict=True):
        priority, reasons = _score_file(fp, content, extra_keywords=extra_keywords)
        if history_scores is not None:
            boost = _history_boost(fp, history_scores)
            if boost > 0:
                boosted = min(5, priority + 1)  # +1 tier cap
                # Tier-bump is gated on boosted > priority (so P5 stays P5
                # instead of wrapping), but the "repeat offender" label
                # should appear whenever there *is* a boost signal — a P5
                # that keeps showing up still deserves the annotation.
                if boosted > priority:
                    priority = boosted
                count_label = ""
                if history_counts:
                    n = _history_count_for(fp, history_counts)
                    if n > 0:
                        count_label = (
                            f": {n} finding{'s' if n != 1 else ''} in last {history_window_days}d"
                        )
                reasons = (*reasons, f"repeat offender{count_label}")
        ranked.append(RankedFile(path=fp, priority=priority, reasons=reasons))

    return sorted(ranked, key=lambda r: (-r.priority, r.path))


# Minimum score that earns a +1 tier boost. Chosen so a single low-severity
# hit from six months ago doesn't trip the bump, but a cluster of recent
# findings does.
_HISTORY_BOOST_THRESHOLD = 1.5


def _history_boost(file_path: str, history_scores: dict[str, float]) -> float:
    """Return the history score for ``file_path`` above the boost threshold.

    The caller may pass scores keyed by absolute path, repo-relative path,
    or filename-only — we check all three. Returns 0.0 when no score
    meets the boost threshold.
    """
    candidates = (
        file_path,
        str(Path(file_path)),
        Path(file_path).as_posix(),
        Path(file_path).name,
    )
    best = 0.0
    for k in candidates:
        score = history_scores.get(k, 0.0)
        if score > best:
            best = score
    if best < _HISTORY_BOOST_THRESHOLD:
        return 0.0
    return best


def _history_count_for(file_path: str, history_counts: dict[str, int]) -> int:
    """Return the max count across alias keys for ``file_path`` (abs, posix, name).

    Takes the max (not the sum) so a file that appears under multiple
    alias keys — e.g. absolute and repo-relative — is counted once rather
    than inflated.
    """
    candidates = (
        file_path,
        str(Path(file_path)),
        Path(file_path).as_posix(),
        Path(file_path).name,
    )
    best = 0
    for k in candidates:
        n = history_counts.get(k, 0)
        if n > best:
            best = n
    return best


def _read_contents_parallel(
    paths: list[str],
    read_fn: Callable[[str], str] | None,
    max_workers: int | None,
) -> list[str]:
    """Read every path via ``read_fn`` using a bounded thread pool.

    Returns a list of contents aligned with ``paths``. Any read error
    yields an empty string for that path so a single unreadable file
    can't abort the rank.
    """
    if read_fn is None:
        return [""] * len(paths)

    def _safe(p: str) -> str:
        try:
            return read_fn(p)
        except (OSError, UnicodeDecodeError):
            return ""

    # Small jobs: serial is faster than spinning up a pool.
    if len(paths) < 20 or (max_workers is not None and max_workers <= 1):
        return [_safe(p) for p in paths]

    import os
    from concurrent.futures import ThreadPoolExecutor

    workers = max_workers if max_workers is not None else min(32, (os.cpu_count() or 4) * 4)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        # ``executor.map`` preserves input order, which is exactly what
        # we need to keep the result list aligned with ``paths``.
        return list(executor.map(_safe, paths))


def rank_to_prompt(ranked: list[RankedFile], top_n: int = 10) -> str:
    """Format ranked files into a prompt-ready priority list."""
    top_n = max(0, top_n)
    lines = ["## File Priority Ranking", ""]
    for i, rf in enumerate(ranked[:top_n], 1):
        reasons_str = ", ".join(rf.reasons) if rf.reasons else "general"
        lines.append(f"{i}. **P{rf.priority}** `{rf.path}` — {reasons_str}")
    if len(ranked) > top_n:
        lines.append(f"\n_(Showing top {top_n} of {len(ranked)} ranked files)_")
    return "\n".join(lines)
