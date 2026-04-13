"""Priority ranker — scores files by likelihood of containing issues.

Glasswing technique #2: rank targets before diving in.
Files handling user input, auth, external data, or crypto get highest priority.
Work top-down so agents spend time where it matters most.
"""

from dataclasses import dataclass
from pathlib import Path

# Priority 5 = most likely to have issues, 1 = least
PRIORITY_KEYWORDS: dict[int, list[str]] = {
    5: [
        "auth", "login", "password", "token", "session", "cookie",
        "oauth", "jwt", "credential", "secret", "key", "cert",
    ],
    4: [
        "input", "request", "upload", "form", "parse", "deserializ",
        "user", "admin", "permission", "role", "access",
    ],
    3: [
        "http", "api", "endpoint", "route", "handler", "middleware",
        "query", "sql", "database", "db", "exec", "shell", "command",
    ],
    2: [
        "config", "setting", "env", "cache", "log", "error",
        "crypto", "encrypt", "decrypt", "hash", "sign",
    ],
    1: [
        "util", "helper", "constant", "type", "model", "schema",
        "test", "mock", "fixture",
    ],
}

SKIP_DIRS = {
    "__pycache__", "node_modules", ".git", ".venv", "venv",
    "dist", "build", ".tox", ".mypy_cache", ".pytest_cache",
}

SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".dylib", ".lock", ".svg", ".png",
    ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf",
}


@dataclass(frozen=True)
class RankedFile:
    """A file with its computed priority score."""
    path: str
    priority: int
    reasons: tuple[str, ...]


def _score_file(path: str, content_lower: str) -> tuple[int, list[str]]:
    """Score a single file based on path and content keywords."""
    best_priority = 0
    reasons: list[str] = []
    combined = path.lower() + " " + content_lower[:2000]

    for priority, keywords in PRIORITY_KEYWORDS.items():
        for kw in keywords:
            if kw in combined:
                if priority > best_priority:
                    best_priority = priority
                reasons.append(kw)

    return best_priority or 1, reasons


def rank_files(file_paths: list[str], read_fn=None) -> list[RankedFile]:
    """Rank files by vulnerability likelihood, highest priority first.

    Args:
        file_paths: List of file paths to rank.
        read_fn: Optional callable(path) -> str that returns file content.
                 If None, ranks by filename alone.

    Returns:
        List of RankedFile sorted by priority descending.
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

        priority, reasons = _score_file(fp, content.lower())
        ranked.append(RankedFile(
            path=fp,
            priority=priority,
            reasons=tuple(dict.fromkeys(reasons)),  # dedupe, preserve order
        ))

    return sorted(ranked, key=lambda r: r.priority, reverse=True)


def rank_to_prompt(ranked: list[RankedFile], top_n: int = 10) -> str:
    """Format ranked files into a prompt-ready priority list.

    Args:
        ranked: Output of rank_files().
        top_n: How many files to include.

    Returns:
        Markdown-formatted priority list for agent prompts.
    """
    lines = ["## File Priority Ranking", ""]
    for i, rf in enumerate(ranked[:top_n], 1):
        reasons_str = ", ".join(rf.reasons) if rf.reasons else "general"
        lines.append(
            f"{i}. **P{rf.priority}** `{rf.path}` — {reasons_str}"
        )
    return "\n".join(lines)
