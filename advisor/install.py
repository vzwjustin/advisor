"""Install helpers — append a nudge block to the user's CLAUDE.md.

Keeps the nudge idempotent via sentinel markers so `advisor install` can run
repeatedly and `advisor uninstall` cleanly removes it. Pure string helpers for
testability; the CLI wrapper handles filesystem IO.
"""

from __future__ import annotations

import enum
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from ._fs import atomic_write_text as _shared_atomic_write
from .skill_asset import SKILL_MD

#: Matches ``<!-- advisor:0.4.0 -->``; used to extract installed skill version
#: without hashing or diffing the whole file.
_BADGE_RE = re.compile(r"<!--\s*advisor:([^\s>]+)\s*-->")


def parse_badge(text: str) -> str | None:
    """Return the version declared by the first advisor badge in ``text``.

    Returns ``None`` when no badge is present — e.g. a skill installed by
    advisor <= 0.4.0 that predates the version-badge convention.
    """
    m = _BADGE_RE.search(text)
    return m.group(1) if m else None


def get_installed_skill_version(path: Path | None = None) -> str | None:
    """Read ``~/.claude/skills/advisor/SKILL.md`` and return its advisor version.

    Returns ``None`` if the file does not exist, is unreadable, or predates
    the badge convention.
    """
    target = path or default_skill_path()
    try:
        return parse_badge(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


class InstallAction(str, enum.Enum):
    """Outcome of an install/uninstall operation.

    Subclassing ``str`` keeps backwards compatibility with call sites that
    compare ``result.action == "installed"`` — the enum members are still
    equal to their string values.
    """

    INSTALLED = "installed"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    REMOVED = "removed"
    ABSENT = "absent"
    SKIPPED = "skipped"

    def __str__(self) -> str:  # pragma: no cover — trivial
        return self.value


def _atomic_write_text(target: Path, text: str) -> None:
    """Atomically write `text` to `target` via a unique tmp file in the same dir.

    Thin wrapper over :func:`advisor._fs.atomic_write_text` that hard-wires
    the install-path hardening: refuse to write through a symlink target
    (defense against shared-host TOCTOU) and chmod the result to ``0o644``
    so editors and other tools can read it. Kept as a module-level
    function so external tests can patch it.
    """
    _shared_atomic_write(target, text, reject_symlink=True, mode=0o644)


OPT_OUT_ENV = "ADVISOR_NO_NUDGE"

START_MARKER = "<!-- advisor:nudge:start -->"
END_MARKER = "<!-- advisor:nudge:end -->"

SKILL_DIR_NAME = "advisor"
SKILL_FILE_NAME = "SKILL.md"

NUDGE_BODY = """## Advisor Tool (review-and-fix pipeline)

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

**Startup rule (ADHD-friendly output):** When `/advisor` is invoked, the
FIRST tools called must be `TeamDelete` then `TeamCreate` — native Claude
Code tools that show as clean orchestration, not Bash. Only AFTER the team
exists should any Bash run (for silent prompt-building only, output
redirected to /tmp). User sees: `**Advisor mode**` → TeamDelete →
TeamCreate → (silent bash) → Agent spawns. NO Bash before TeamCreate.

- Slash command: `/advisor [optional target dir]`
- Package: https://github.com/vzwjustin/advisor
- Install: `uvx advisor install` (this block + skill) · `pipx install advisor`
- `advisor pipeline <dir>` — print the full pipeline reference
"""


@dataclass(frozen=True)
class InstallResult:
    path: Path
    action: str  # value from :class:`InstallAction` (kept as str for back-compat)
    error: str | None = None  # populated when a non-fatal write failure occurred


@dataclass(frozen=True)
class ComponentStatus:
    """State of a single installed component (nudge or skill)."""

    name: str
    path: Path
    present: bool
    current: bool  # body matches the bundled version


@dataclass(frozen=True)
class Status:
    """Snapshot of advisor's installed state."""

    nudge: ComponentStatus
    skill: ComponentStatus
    opt_out: bool


def render_block(body: str = NUDGE_BODY) -> str:
    """Return the full sentinel-wrapped nudge block."""
    return f"{START_MARKER}\n{body.rstrip()}\n{END_MARKER}\n"


def _strip_all_blocks(existing: str) -> str:
    """Remove every sentinel-wrapped block, including nested or duplicate ones.

    Iterates until no markers remain at all — including orphaned START markers
    left behind by pathological interleaving (START-START-END-END).
    """
    cleaned = existing
    # rpartition picks the LAST end/start pair on each pass; non-nested
    # multi-block input (START-END-START-END) requires multiple iterations
    # because each pass strips one block and leaves the others intact.
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
        updated = f"{stripped}\n\n{block}".lstrip("\n") if stripped else block
        if updated.strip() == existing.strip():
            return existing, InstallAction.UNCHANGED.value
        return updated, InstallAction.UPDATED.value

    if not existing.strip():
        return block, InstallAction.INSTALLED.value

    separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    return f"{existing}{separator}{block}", InstallAction.INSTALLED.value


def remove_nudge(existing: str) -> tuple[str, str]:
    """Return (new_contents, action) with every sentinel marker/block removed.

    Treat orphaned markers as removable corruption instead of reporting
    ``absent``. This keeps uninstall resilient when prior partial writes or
    manual edits left only one marker behind.
    """
    if START_MARKER not in existing and END_MARKER not in existing:
        return existing, InstallAction.ABSENT.value
    stripped = _strip_all_blocks(existing).strip()
    cleaned = f"{stripped}\n" if stripped else ""
    return cleaned, InstallAction.REMOVED.value


def default_claude_md() -> Path:
    return Path.home() / ".claude" / "CLAUDE.md"


def install(path: Path | None = None, body: str = NUDGE_BODY) -> InstallResult:
    target = path or default_claude_md()
    # Defense-in-depth: if no explicit path was supplied, refuse to write
    # outside the user's home dir. Protects against a manipulated ``$HOME``
    # redirecting the nudge to an unrelated file. Explicit ``path=`` is
    # respected as-is — tests and power-users need to target arbitrary files.
    if path is None:
        resolved = target.resolve()
        if not resolved.is_relative_to(Path.home().resolve()):
            raise OSError(f"refusing to install nudge outside $HOME: {resolved}")
        target = resolved
    target.parent.mkdir(parents=True, exist_ok=True)
    current = target.read_text(encoding="utf-8") if target.exists() else ""
    new_contents, action = apply_nudge(current, body)
    if action != InstallAction.UNCHANGED.value:
        _atomic_write_text(target, new_contents)
    return InstallResult(path=target, action=action)


def should_auto_nudge(env: dict[str, str] | None = None) -> bool:
    """Return False if opt-out env var is set, else True."""
    source = env if env is not None else os.environ
    value = source.get(OPT_OUT_ENV, "").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def ensure_nudge(
    path: Path | None = None,
    env: dict[str, str] | None = None,
    stream: IO[str] | None = None,
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
        return InstallResult(path=target, action=InstallAction.UNCHANGED.value)

    nudge_result = InstallResult(path=target, action=InstallAction.UNCHANGED.value)
    skill_result_action = InstallAction.UNCHANGED.value
    skill_target = skill_path or default_skill_path()

    out = stream if stream is not None else sys.stderr
    from . import _style

    errors: list[str] = []
    try:
        current = target.read_text(encoding="utf-8") if target.exists() else ""
        expected_block = render_block(NUDGE_BODY)
        if (
            START_MARKER not in current
            or END_MARKER not in current
            or expected_block not in current
        ):
            nudge_result = install(path=target, body=NUDGE_BODY)
    except (OSError, UnicodeDecodeError) as exc:
        # Warnings for auto-install failures are non-fatal by design — we
        # don't want `advisor plan` to bail because ~/.claude is readonly.
        # But stay visible: use the warning glyph, not dim text, so a user
        # who misses the warning once will still see it on the next run.
        msg = f"nudge write failed ({target}): {exc}"
        errors.append(msg)
        print(_style.warning_box(msg), file=out)

    try:
        if not skill_target.exists() or skill_target.read_text(encoding="utf-8") != SKILL_MD:
            skill_res = install_skill(path=skill_target)
            skill_result_action = skill_res.action
    except (OSError, UnicodeDecodeError) as exc:
        msg = f"skill write failed ({skill_target}): {exc}"
        errors.append(msg)
        print(_style.warning_box(msg), file=out)
        skill_result_action = InstallAction.UNCHANGED.value

    _updated = {InstallAction.INSTALLED.value, InstallAction.UPDATED.value}
    if nudge_result.action in _updated or skill_result_action in _updated:
        pieces: list[str] = []
        if nudge_result.action in _updated:
            pieces.append(f"  nudge     → {nudge_result.path}")
        if skill_result_action in _updated:
            pieces.append(f"  skill     → {skill_target}")

        lines = [
            "",
            _style.banner("advisor first-run setup", width=35),
            "",
            _style.success_box("Setup complete!", stream=out),
        ]
        lines.extend(pieces)
        lines.append("")
        rocket = _style.glyph("🚀", ">")
        lines.append(f"  {_style.paint(f'{rocket} Quick start:', 'cyan', 'bold', stream=out)}")
        lines.append(_style.cta("/advisor <dir>", "Start a code review", stream=out))
        lines.append(_style.cta("advisor status", "Check installation", stream=out))
        lines.append(_style.cta("advisor --help", "See all commands", stream=out))
        lines.append("")
        lines.append(
            _style.dim(
                f"  opt out: {OPT_OUT_ENV}=1  ·  remove: advisor uninstall",
                stream=out,
            )
        )
        print("\n".join(lines), file=out)

    # If anything failed (nudge or skill), surface it on the returned
    # result. The caller (``__main__.main``) still proceeds normally — a
    # readonly ~/.claude must not break the rest of the CLI — but
    # programmatic consumers can inspect ``result.error`` to detect partial
    # installs.
    if errors:
        return InstallResult(
            path=nudge_result.path,
            action=nudge_result.action,
            error="; ".join(errors),
        )
    return nudge_result


def uninstall(path: Path | None = None) -> InstallResult:
    target = path or default_claude_md()
    if not target.exists():
        return InstallResult(path=target, action=InstallAction.ABSENT.value)
    current = target.read_text(encoding="utf-8")
    new_contents, action = remove_nudge(current)
    if action == InstallAction.REMOVED.value:
        _atomic_write_text(target, new_contents)
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
    # Same $HOME guard as ``install()`` — refuse to auto-write outside the
    # user's home when no explicit path is supplied. Reassign ``target`` to
    # the resolved path so all subsequent operations (mkdir, exists, write)
    # land on the canonical location instead of the unresolved one — keeps
    # behavior identical to ``install()`` and avoids a confusing situation
    # where the symlink-rejection check resolved one path but the write
    # then operated on a different one.
    if path is None:
        resolved = target.resolve()
        if not resolved.is_relative_to(Path.home().resolve()):
            raise OSError(f"refusing to install skill outside $HOME: {resolved}")
        target = resolved
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = ""
        if current == body:
            return InstallResult(path=target, action=InstallAction.UNCHANGED.value)
        _atomic_write_text(target, body)
        return InstallResult(path=target, action=InstallAction.UPDATED.value)

    _atomic_write_text(target, body)
    return InstallResult(path=target, action=InstallAction.INSTALLED.value)


def status(
    nudge_path: Path | None = None,
    skill_path: Path | None = None,
    env: dict[str, str] | None = None,
) -> Status:
    """Inspect the local advisor install — does not write anything."""
    nudge_target = nudge_path or default_claude_md()
    skill_target = skill_path or default_skill_path()
    expected_block = render_block().strip()

    nudge_present = False
    nudge_current = False
    if nudge_target.exists():
        try:
            text = nudge_target.read_text(encoding="utf-8")
            nudge_present = START_MARKER in text and END_MARKER in text
            nudge_current = nudge_present and expected_block in text
        except (OSError, UnicodeDecodeError):
            pass

    skill_present = skill_target.exists()
    skill_current = False
    if skill_present:
        try:
            skill_current = skill_target.read_text(encoding="utf-8") == SKILL_MD
        except (OSError, UnicodeDecodeError):
            skill_current = False

    return Status(
        nudge=ComponentStatus(
            name="nudge",
            path=nudge_target,
            present=nudge_present,
            current=nudge_current,
        ),
        skill=ComponentStatus(
            name="skill",
            path=skill_target,
            present=skill_present,
            current=skill_current,
        ),
        opt_out=not should_auto_nudge(env),
    )


def uninstall_skill(path: Path | None = None) -> InstallResult:
    """Remove the advisor SKILL.md and its parent directory if empty.

    Leaves ~/.claude/skills itself alone; only touches our own subdirectory.
    Raises OSError if the file exists but cannot be deleted (e.g. permissions).
    """
    target = path or default_skill_path()
    if not target.exists():
        return InstallResult(path=target, action=InstallAction.ABSENT.value)
    # Reject symlinks for symmetry with ``_atomic_write_text``: the
    # install path refuses to write through a symlink, so the uninstall
    # path refuses to delete one. Better to require the user to clean up
    # an unexpected symlink themselves than to silently follow it.
    if target.is_symlink():
        raise OSError(f"refusing to unlink symlink at {target}; remove it manually if intended")
    target.unlink()
    parent = target.parent
    try:
        if parent.name == SKILL_DIR_NAME and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    return InstallResult(path=target, action=InstallAction.REMOVED.value)
