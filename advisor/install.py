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
from .skill_asset import SKILL_MD, SKILL_MD_UPDATE

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


def _semver_tuple(v: str) -> tuple[int, ...] | None:
    """Parse ``v`` as a dotted-int prefix; return ``None`` on parse failure.

    Strips a trailing pre-release / build suffix (e.g. ``0.6.0-rc1``) so the
    comparison stays focused on the X.Y.Z prefix that drives upgrade order.
    """
    head = re.split(r"[-+]", v, maxsplit=1)[0]
    parts = head.split(".")
    try:
        return tuple(int(p) for p in parts if p)
    except ValueError:
        return None


def _is_semver_newer(installed: str, bundled: str) -> bool:
    """Return True if ``installed`` parses as strictly newer than ``bundled``."""
    a, b = _semver_tuple(installed), _semver_tuple(bundled)
    if a is None or b is None:
        return False
    return a > b


#: Bundled in the wheel via pyproject force-include. The repo-root fallback
#: lets ``advisor install`` show release notes when running from source.
_CHANGELOG_CANDIDATES = (
    Path(__file__).parent / "_changelog.md",
    Path(__file__).parent.parent / "CHANGELOG.md",
)


def _read_changelog() -> str | None:
    """Return the bundled CHANGELOG text, or ``None`` if unreadable."""
    for candidate in _CHANGELOG_CANDIDATES:
        try:
            return candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return None


def load_release_notes(version: str) -> str | None:
    """Return the body of the ``## [version]`` section in the bundled CHANGELOG.

    Used by ``advisor install`` to surface "what's new" on upgrade. Returns
    ``None`` when the changelog is missing, unreadable, or has no section
    for ``version``.
    """
    text = _read_changelog()
    if text is None:
        return None
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)",
        re.DOTALL | re.MULTILINE,
    )
    m = pattern.search(text)
    return m.group(1).strip() if m else None


_VERSION_HEADING_RE = re.compile(
    r"^## \[(?P<version>[^\]]+)\](?P<rest>[^\n]*)\n(?P<body>.*?)(?=^## \[|\Z)",
    re.DOTALL | re.MULTILINE,
)


def parse_changelog_sections(text: str, since: str | None = None) -> list[tuple[str, str, str]]:
    """Parse a CHANGELOG body into ``[(version, heading_rest, body), ...]``.

    Newest-first. Skips ``[Unreleased]``. When ``since`` is given, only
    sections strictly newer than that version are returned.
    """
    sections: list[tuple[str, str, str]] = []
    for m in _VERSION_HEADING_RE.finditer(text):
        version = m.group("version").strip()
        if version.lower() == "unreleased":
            continue
        if since is not None:
            cur = _semver_tuple(version)
            floor = _semver_tuple(since)
            if cur is not None and floor is not None and cur <= floor:
                continue
        sections.append((version, m.group("rest").rstrip(), m.group("body").strip()))
    return sections


def load_changelog_sections(since: str | None = None) -> list[tuple[str, str, str]]:
    """Return bundled CHANGELOG sections newest-first."""
    text = _read_changelog()
    return parse_changelog_sections(text, since=since) if text is not None else []


def fetch_pypi_latest_version(package: str = "advisor-agent", timeout: float = 5.0) -> str | None:
    """Return the latest version on PyPI, or ``None`` on network/parse failure."""
    import json as _json
    import urllib.error
    import urllib.request

    url = f"https://pypi.org/pypi/{package}/json"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = _json.load(resp)
    except (urllib.error.URLError, OSError, _json.JSONDecodeError, ValueError):
        return None
    info = data.get("info") if isinstance(data, dict) else None
    if not isinstance(info, dict):
        return None
    version = info.get("version")
    return version if isinstance(version, str) else None


def _update_check_cache_path() -> Path:
    """Return ``~/.claude/.advisor/update-check.json`` (parent created lazily)."""
    return Path.home() / ".claude" / ".advisor" / "update-check.json"


def check_for_update_cached(
    *,
    current: str,
    ttl_seconds: int = 86400,
    cache_path: Path | None = None,
) -> str | None:
    """Return the latest PyPI version when newer than ``current``, else ``None``.

    Caches the PyPI lookup for ``ttl_seconds`` (default 24h) so successive
    CLI invocations don't hammer the index. Network failures are silent —
    a stale cache is preferred over breaking the user's CLI.
    """
    import json as _json
    import time as _time

    path = cache_path or _update_check_cache_path()
    now = _time.time()
    cached_latest: str | None = None
    cached_at: float = 0.0
    try:
        cached = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, _json.JSONDecodeError, ValueError):
        cached = None
    if isinstance(cached, dict):
        latest_val = cached.get("latest")
        at_val = cached.get("checked_at")
        if isinstance(latest_val, str):
            cached_latest = latest_val
        if isinstance(at_val, (int, float)):
            cached_at = float(at_val)

    latest: str | None
    if cached_latest is not None and (now - cached_at) < ttl_seconds:
        latest = cached_latest
    else:
        fetched = fetch_pypi_latest_version()
        if fetched is None:
            # Network failed; fall back to the (possibly stale) cache value.
            latest = cached_latest
        else:
            latest = fetched
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    _json.dumps({"latest": latest, "checked_at": now}),
                    encoding="utf-8",
                )
            except OSError:
                pass  # Cache is best-effort.

    if latest is None:
        return None
    cur_t = _semver_tuple(current)
    lat_t = _semver_tuple(latest)
    if cur_t is None or lat_t is None:
        return None
    return latest if lat_t > cur_t else None


def fetch_remote_changelog(
    url: str = "https://raw.githubusercontent.com/vzwjustin/advisor/main/CHANGELOG.md",
    timeout: float = 5.0,
) -> str | None:
    """Fetch a remote CHANGELOG.md, or ``None`` on any network/decode failure."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            raw = resp.read()
    except (urllib.error.URLError, OSError):
        return None
    try:
        text: str = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text


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
UPDATE_SKILL_DIR_NAME = "advisor-update"

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
- Install: `uvx --from advisor-agent advisor install` (this block + skill) · `pipx install advisor-agent`
- `advisor pipeline <dir>` — print the full pipeline reference

## Behavioral Guidelines

Reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

Tradeoff: These guidelines bias toward caution over speed. For trivial tasks, use judgment.

### 1. Think Before Coding
Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First
Minimum code that solves the problem. Nothing speculative.
- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

### 3. Surgical Changes
Touch only what you must. Clean up only your own mess.

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

### 4. Goal-Driven Execution
Define success criteria. Loop until verified.

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"
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
    update_skill: ComponentStatus | None = None


def render_block(body: str = NUDGE_BODY) -> str:
    """Return the full sentinel-wrapped nudge block."""
    return f"{START_MARKER}\n{body.rstrip()}\n{END_MARKER}\n"


_BLOCK_RE = re.compile(
    re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER),
    re.DOTALL,
)
_ORPHAN_MARKER_RE = re.compile(re.escape(START_MARKER) + r"|" + re.escape(END_MARKER))


def _strip_all_blocks(existing: str) -> str:
    """Remove every sentinel-wrapped block, including nested or duplicate ones.

    Single non-greedy DOTALL regex strips well-formed START..END blocks in
    one pass — O(N) in input length regardless of marker count. A second
    pass clears any orphan markers left by pathological interleaving
    (e.g. START-START-END-END collapses the inner pair, leaving an outer
    orphan START + orphan END).
    """
    cleaned = _BLOCK_RE.sub("", existing)
    cleaned = _ORPHAN_MARKER_RE.sub("", cleaned)
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
    # redirecting the nudge to an unrelated file (env-var poisoning).
    # Symlinked dirs *under* a clean ``$HOME`` (e.g. dotfiles managers like
    # stow / chezmoi pointing ``~/.claude`` at ``~/dotfiles/claude``) are
    # legitimate and pass the guard — ``resolve()`` follows them and the
    # resolved path still lives under ``Path.home().resolve()``. Explicit
    # ``path=`` is respected as-is — tests and power-users need to target
    # arbitrary files.
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
        # Compare with trailing-newline differences ignored — atomic_write_text
        # may add/strip a final newline depending on platform, and a strict
        # byte-equality check would re-write the file on every CLI invocation
        # for a no-op delta.
        if not skill_target.exists() or skill_target.read_text(encoding="utf-8").rstrip(
            "\n"
        ) != SKILL_MD.rstrip("\n"):
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


def default_update_skill_path() -> Path:
    return default_skills_root() / UPDATE_SKILL_DIR_NAME / SKILL_FILE_NAME


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
        # Warn (do not block) when about to overwrite a SKILL.md whose
        # advisor-version badge is newer than the bundled body — a stale
        # ``advisor install`` from an older venv shouldn't silently roll
        # back a fresh skill the user installed via a newer release.
        installed_v = parse_badge(current)
        bundled_v = parse_badge(body)
        if (
            installed_v is not None
            and bundled_v is not None
            and _is_semver_newer(installed_v, bundled_v)
        ):
            print(
                f"warning: overwriting SKILL.md v{installed_v} with bundled "
                f"v{bundled_v} (downgrade); the installed copy is newer.",
                file=sys.stderr,
            )
        _atomic_write_text(target, body)
        return InstallResult(path=target, action=InstallAction.UPDATED.value)

    _atomic_write_text(target, body)
    return InstallResult(path=target, action=InstallAction.INSTALLED.value)


def install_update_skill(
    path: Path | None = None,
    body: str = SKILL_MD_UPDATE,
) -> InstallResult:
    """Write the bundled /advisor-update SKILL.md (default
    ``~/.claude/skills/advisor-update/SKILL.md``).

    Mirrors :func:`install_skill` — same $HOME guard, same symlink-rejection,
    same atomic write — but targets the sibling skill directory so Claude
    Code registers ``/advisor-update`` as its own slash command.
    """
    target = path or default_update_skill_path()
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
        installed_v = parse_badge(current)
        bundled_v = parse_badge(body)
        if (
            installed_v is not None
            and bundled_v is not None
            and _is_semver_newer(installed_v, bundled_v)
        ):
            print(
                f"warning: overwriting SKILL.md v{installed_v} with bundled "
                f"v{bundled_v} (downgrade); the installed copy is newer.",
                file=sys.stderr,
            )
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
    update_skill_target = default_update_skill_path()
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

    update_skill_present = update_skill_target.exists()
    update_skill_current = False
    if update_skill_present:
        try:
            update_skill_current = (
                update_skill_target.read_text(encoding="utf-8") == SKILL_MD_UPDATE
            )
        except (OSError, UnicodeDecodeError):
            update_skill_current = False

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
        update_skill=ComponentStatus(
            name="update-skill",
            path=update_skill_target,
            present=update_skill_present,
            current=update_skill_current,
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
    #
    # NOTE: There is a benign TOCTOU between ``is_symlink()`` and
    # ``unlink()``. ``unlink(2)`` on POSIX never follows symlinks — it
    # removes the symlink entry itself, not its target — so the worst
    # case if an attacker races a symlink in between is that we delete
    # the attacker's injected symlink rather than a sensitive file.
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


def uninstall_update_skill(path: Path | None = None) -> InstallResult:
    """Remove the /advisor-update SKILL.md and its parent directory if empty.

    Mirrors :func:`uninstall_skill` — same symlink rejection, same parent
    cleanup — but targets the sibling ``advisor-update`` skill directory.
    """
    target = path or default_update_skill_path()
    if not target.exists():
        return InstallResult(path=target, action=InstallAction.ABSENT.value)
    if target.is_symlink():
        raise OSError(f"refusing to unlink symlink at {target}; remove it manually if intended")
    target.unlink()
    parent = target.parent
    try:
        if parent.name == UPDATE_SKILL_DIR_NAME and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    return InstallResult(path=target, action=InstallAction.REMOVED.value)
