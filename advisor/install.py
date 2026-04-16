"""Install helpers — append a nudge block to the user's CLAUDE.md.

Keeps the nudge idempotent via sentinel markers so `advisor install` can run
repeatedly and `advisor uninstall` cleanly removes it. Pure string helpers for
testability; the CLI wrapper handles filesystem IO.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .skill_asset import SKILL_MD

OPT_OUT_ENV = "ADVISOR_NO_NUDGE"

START_MARKER = "<!-- advisor:nudge:start -->"
END_MARKER = "<!-- advisor:nudge:end -->"

SKILL_DIR_NAME = "advisor"
SKILL_FILE_NAME = "SKILL.md"

NUDGE_BODY = """## Advisor Tool (Glasswing review-and-fix pipeline)

For strategic multi-file work — reviews, audits, root-cause hunts,
concurrency bugs, architectural decisions, or coordinated fix loops across
3+ files — invoke `/advisor` (or just say "run the advisor"). Skip it for
trivial single-file edits. `advisor install` puts the slash command at
`~/.claude/skills/advisor/SKILL.md` automatically.

**Architecture:** Opus is the strategist, Sonnet runners are its hands.
Opus wakes up first, does Glob+Grep discovery itself (its large context
window holds the map), ranks files P1–P5, and tells the team-lead how many
runners to spawn — no hardcoded pool size. Opus then dispatches explore
assignments, and optionally fix assignments, to the runner pool.

Runners and Opus stay in **live two-way conversation** throughout: runners
ask questions when stuck, send progress pings, and receive real-time
answers; Opus watches each runner and verifies their output the moment it
lands. Runners work ONLY on what Opus hands them.

- Slash command: `/advisor [optional target dir]`
- Package: https://github.com/vzwjustin/advisor
- Install: `uvx advisor install` (this block + skill) · `pipx install advisor`
- `advisor prompt advisor <dir>` — print the exact Opus prompt body
- `advisor pipeline <dir>` — print the full pipeline reference
"""


@dataclass(frozen=True)
class InstallResult:
    path: Path
    action: str  # "installed" | "updated" | "unchanged" | "removed" | "absent"


def render_block(body: str = NUDGE_BODY) -> str:
    """Return the full sentinel-wrapped nudge block."""
    return f"{START_MARKER}\n{body.rstrip()}\n{END_MARKER}\n"


def _strip_all_blocks(existing: str) -> str:
    """Remove every sentinel-wrapped block, including nested or duplicate ones.

    Iterates until no markers remain at all — including orphaned START markers
    left behind by pathological interleaving (START-START-END-END).
    """
    cleaned = existing
    while START_MARKER in cleaned or END_MARKER in cleaned:
        if START_MARKER in cleaned and END_MARKER in cleaned:
            before_end, _, after_end = cleaned.rpartition(END_MARKER)
            before_start, _, _ = before_end.rpartition(START_MARKER)
            cleaned = f"{before_start.rstrip()}\n{after_end.lstrip()}"
        elif START_MARKER in cleaned:
            before, _, after = cleaned.partition(START_MARKER)
            cleaned = f"{before.rstrip()}\n{after.lstrip()}"
        else:
            before, _, after = cleaned.partition(END_MARKER)
            cleaned = f"{before.rstrip()}\n{after.lstrip()}"
    return cleaned


def apply_nudge(existing: str, body: str = NUDGE_BODY) -> tuple[str, str]:
    """Return (new_contents, action) without mutating `existing`.

    Idempotent: strips any existing sentinel blocks (including duplicates from
    prior buggy installs) and appends a clean one. Action is "installed",
    "updated", or "unchanged".
    """
    block = render_block(body)
    has_block = START_MARKER in existing and END_MARKER in existing

    if has_block:
        stripped = _strip_all_blocks(existing).strip()
        updated = (
            f"{stripped}\n\n{block}".lstrip("\n") if stripped else block
        )
        if updated == existing:
            return existing, "unchanged"
        return updated, "updated"

    if not existing.strip():
        return block, "installed"

    separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return f"{existing}{separator}{block}", "installed"


def remove_nudge(existing: str) -> tuple[str, str]:
    """Return (new_contents, action) with every sentinel block removed."""
    if START_MARKER not in existing or END_MARKER not in existing:
        return existing, "absent"
    stripped = _strip_all_blocks(existing).strip()
    cleaned = f"{stripped}\n" if stripped else ""
    return cleaned, "removed"


def default_claude_md() -> Path:
    return Path.home() / ".claude" / "CLAUDE.md"


def install(path: Path | None = None, body: str = NUDGE_BODY) -> InstallResult:
    target = path or default_claude_md()
    target.parent.mkdir(parents=True, exist_ok=True)
    current = target.read_text(encoding="utf-8") if target.exists() else ""
    new_contents, action = apply_nudge(current, body)
    if action != "unchanged":
        target.write_text(new_contents, encoding="utf-8")
    return InstallResult(path=target, action=action)


def should_auto_nudge(env: dict[str, str] | None = None) -> bool:
    """Return False if opt-out env var is set, else True."""
    source = env if env is not None else os.environ
    value = source.get(OPT_OUT_ENV, "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def ensure_nudge(
    path: Path | None = None,
    env: dict[str, str] | None = None,
    stream=None,
    skill_path: Path | None = None,
) -> InstallResult:
    """First-run hook: silently install the nudge AND the /advisor skill.

    This runs automatically on every advisor CLI invocation (except
    `install` / `uninstall`), so vibe coders never have to remember to
    call `advisor install` explicitly — the first time they run anything,
    their ~/.claude/CLAUDE.md gets the nudge block and the `/advisor`
    slash command is written to ~/.claude/skills/advisor/SKILL.md.

    Contract:
    - Skips entirely if `ADVISOR_NO_NUDGE` is set.
    - No-op if both the sentinel block and the skill file are already present.
    - Prints a single friendly notice to `stream` (default stderr) when
      any piece is freshly installed.
    - Swallows filesystem and decode errors so CLI commands never fail here.
    """
    target = path or default_claude_md()
    if not should_auto_nudge(env):
        return InstallResult(path=target, action="unchanged")

    nudge_result = InstallResult(path=target, action="unchanged")
    skill_result_action = "unchanged"
    skill_target = skill_path or default_skill_path()

    try:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        if START_MARKER not in current or END_MARKER not in current:
            nudge_result = install(path=target)
    except (OSError, UnicodeDecodeError):
        # Nudge install failed; still try the skill install below.
        pass

    try:
        if not skill_target.exists() or skill_target.read_text(encoding="utf-8") != SKILL_MD:
            skill_res = install_skill(path=skill_target)
            skill_result_action = skill_res.action
    except (OSError, UnicodeDecodeError):
        skill_result_action = "unchanged"

    if nudge_result.action == "installed" or skill_result_action in ("installed", "updated"):
        out = stream if stream is not None else sys.stderr
        pieces: list[str] = []
        if nudge_result.action == "installed":
            pieces.append(f"nudge → {nudge_result.path}")
        if skill_result_action in ("installed", "updated"):
            pieces.append(f"/advisor skill → {skill_target}")
        lines = ["advisor: first-run setup complete."]
        for piece in pieces:
            lines.append(f"  {piece}")
        lines.append(f"Opt out: {OPT_OUT_ENV}=1  |  Remove: `advisor uninstall`")
        print("\n".join(lines), file=out)

    return nudge_result


def uninstall(path: Path | None = None) -> InstallResult:
    target = path or default_claude_md()
    if not target.exists():
        return InstallResult(path=target, action="absent")
    current = target.read_text(encoding="utf-8")
    new_contents, action = remove_nudge(current)
    if action == "removed":
        target.write_text(new_contents, encoding="utf-8")
    return InstallResult(path=target, action=action)


# ── Skill install (writes ~/.claude/skills/advisor/SKILL.md) ────────


def default_skills_root() -> Path:
    return Path.home() / ".claude" / "skills"


def default_skill_path() -> Path:
    return default_skills_root() / SKILL_DIR_NAME / SKILL_FILE_NAME


def install_skill(
    path: Path | None = None,
    body: str = SKILL_MD,
) -> InstallResult:
    """Write the bundled SKILL.md to `path` (default ~/.claude/skills/advisor/SKILL.md).

    Overwrites only the SKILL.md file inside the advisor skill directory —
    never touches other files or directories under ~/.claude/skills.
    """
    target = path or default_skill_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = ""
        if current == body:
            return InstallResult(path=target, action="unchanged")
        target.write_text(body, encoding="utf-8")
        return InstallResult(path=target, action="updated")

    target.write_text(body, encoding="utf-8")
    return InstallResult(path=target, action="installed")


def uninstall_skill(path: Path | None = None) -> InstallResult:
    """Remove the advisor SKILL.md and its parent directory if empty.

    Leaves ~/.claude/skills itself alone; only touches our own subdirectory.
    """
    target = path or default_skill_path()
    if not target.exists():
        return InstallResult(path=target, action="absent")
    try:
        target.unlink()
    except OSError:
        return InstallResult(path=target, action="absent")
    parent = target.parent
    try:
        if parent.name == SKILL_DIR_NAME and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    return InstallResult(path=target, action="removed")
