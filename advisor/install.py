"""Install helpers — append a nudge block to the user's CLAUDE.md.

Keeps the nudge idempotent via sentinel markers so `advisor install` can run
repeatedly and `advisor uninstall` cleanly removes it. Pure string helpers for
testability; the CLI wrapper handles filesystem IO.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

START_MARKER = "<!-- advisor:nudge:start -->"
END_MARKER = "<!-- advisor:nudge:end -->"

NUDGE_BODY = """## Advisor Tool (Glasswing)

For complex or ambiguous tasks (3+ files, architectural decisions, root-cause debugging, concurrency bugs), reach for the **advisor** tool before committing to an approach and again before declaring done. Skip it for trivial edits.

- Package: https://github.com/vzwjustin/advisor
- Install: `uvx advisor install` (this block) · `pipx install advisor`
- Run: `advisor pipeline <dir>` prints the full team pipeline to paste into Claude Code.
"""


@dataclass(frozen=True)
class InstallResult:
    path: Path
    action: str  # "installed" | "updated" | "unchanged" | "removed" | "absent"


def render_block(body: str = NUDGE_BODY) -> str:
    """Return the full sentinel-wrapped nudge block."""
    return f"{START_MARKER}\n{body.rstrip()}\n{END_MARKER}\n"


def apply_nudge(existing: str, body: str = NUDGE_BODY) -> tuple[str, str]:
    """Return (new_contents, action) without mutating `existing`.

    Idempotent: replaces an existing sentinel block if present, otherwise
    appends with a blank-line separator. Action is "installed", "updated", or
    "unchanged".
    """
    block = render_block(body)
    if START_MARKER in existing and END_MARKER in existing:
        before, _, rest = existing.partition(START_MARKER)
        _, _, after = rest.partition(END_MARKER)
        after = after.lstrip("\n")
        updated = f"{before.rstrip()}\n\n{block}{after}".lstrip("\n")
        if updated == existing:
            return existing, "unchanged"
        return updated, "updated"

    if not existing.strip():
        return block, "installed"

    separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return f"{existing}{separator}{block}", "installed"


def remove_nudge(existing: str) -> tuple[str, str]:
    """Return (new_contents, action) with the sentinel block removed."""
    if START_MARKER not in existing or END_MARKER not in existing:
        return existing, "absent"
    before, _, rest = existing.partition(START_MARKER)
    _, _, after = rest.partition(END_MARKER)
    cleaned = f"{before.rstrip()}\n{after.lstrip()}".strip() + "\n"
    return cleaned, "removed"


def default_claude_md() -> Path:
    return Path.home() / ".claude" / "CLAUDE.md"


def install(path: Path | None = None, body: str = NUDGE_BODY) -> InstallResult:
    target = path or default_claude_md()
    target.parent.mkdir(parents=True, exist_ok=True)
    current = target.read_text() if target.exists() else ""
    new_contents, action = apply_nudge(current, body)
    if action != "unchanged":
        target.write_text(new_contents)
    return InstallResult(path=target, action=action)


def uninstall(path: Path | None = None) -> InstallResult:
    target = path or default_claude_md()
    if not target.exists():
        return InstallResult(path=target, action="absent")
    current = target.read_text()
    new_contents, action = remove_nudge(current)
    if action == "removed":
        target.write_text(new_contents)
    return InstallResult(path=target, action=action)
