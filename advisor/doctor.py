"""``advisor doctor`` — extended diagnostic command.

Goes beyond ``advisor status`` by also checking the surrounding environment:

* Python version (3.10+ required)
* ``git`` availability (needed for ``--since`` / ``--staged`` / ``--branch``)
* ``claude`` CLI availability (needed to actually run the pipeline)
* ``~/.claude`` directory health (exists, is a real directory, not a symlink)
* Advisor install status (nudge + skill present, current)
* Env-var detection (prints any ``ADVISOR_*`` overrides currently in effect)

Returns a :class:`DoctorReport` that can be serialized to JSON for
scripted consumption.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from .install import OPT_OUT_ENV, Status, get_installed_skill_version
from .install import status as install_status

# Env vars scanned by ``default_team_config`` + other advisor surfaces.
# ``OPT_OUT_ENV`` (currently ``ADVISOR_NO_NUDGE``) is appended below so this
# list stays in sync with :mod:`advisor.install` even if the opt-out env var
# is ever renamed.

HealthLevel = Literal["ok", "warn", "fail"]


@dataclass(frozen=True, slots=True)
class Check:
    """A single doctor check result."""

    name: str
    level: HealthLevel
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Aggregated doctor output — one check per thing inspected."""

    healthy: bool
    checks: list[Check]
    env_overrides: dict[str, str]
    advisor_version: str
    python_version: str
    platform: str

    def to_dict(self) -> dict[str, object]:
        return {
            "healthy": self.healthy,
            "checks": [c.to_dict() for c in self.checks],
            "env_overrides": self.env_overrides,
            "advisor_version": self.advisor_version,
            "python_version": self.python_version,
            "platform": self.platform,
        }


# ENV VARS scanned by default_team_config + other advisor surfaces.
# Keep ``OPT_OUT_ENV`` in the tuple so doctor auto-follows any rename.
_KNOWN_ENV_VARS = (
    "ADVISOR_MODEL",
    "ADVISOR_RUNNER_MODEL",
    "ADVISOR_MAX_RUNNERS",
    "ADVISOR_FILE_TYPES",
    "ADVISOR_MIN_PRIORITY",
    "ADVISOR_TEST_COMMAND",
    OPT_OUT_ENV,
)


def _check_python_version() -> Check:
    # pyproject.toml gates Python >= 3.10 via requires-python at install
    # time, but a broken uv environment, the wrong shim, or a manual
    # ``python3`` invocation can still bypass that and reach this code on
    # an older interpreter. Surface it as a hard fail rather than green.
    info = sys.version_info
    label = f"Python {info.major}.{info.minor}"
    # Keep this threshold in sync with `requires-python` in pyproject.toml.
    if (info.major, info.minor) < (3, 10):
        return Check("python", "fail", f"{label} — advisor requires Python 3.10+")
    return Check("python", "ok", label)


def _check_git() -> Check:
    if shutil.which("git"):
        return Check("git", "ok", "git available on PATH")
    return Check(
        "git",
        "warn",
        "git not on PATH; --since/--staged/--branch will be unavailable",
    )


def _check_claude_cli() -> Check:
    if shutil.which("claude"):
        return Check("claude-cli", "ok", "`claude` CLI available on PATH")
    return Check(
        "claude-cli",
        "warn",
        "`claude` CLI not on PATH; you cannot run the live pipeline from this shell",
    )


def _check_claude_home() -> Check:
    claude_dir = Path.home() / ".claude"
    if claude_dir.is_symlink():
        return Check(
            "claude-home",
            "warn",
            f"{claude_dir} is a symlink; advisor install refuses to write through symlinks",
        )
    if not claude_dir.exists():
        return Check(
            "claude-home",
            "warn",
            f"{claude_dir} does not exist (will be created on first `advisor install`)",
        )
    if not claude_dir.is_dir():
        return Check("claude-home", "fail", f"{claude_dir} exists but is not a directory")
    return Check("claude-home", "ok", f"{claude_dir} is a regular directory")


def _check_install(
    status: Status, installed_version: str | None, current_version: str
) -> list[Check]:
    checks: list[Check] = []
    for component in (status.nudge, status.skill):
        name = component.name
        if not component.present:
            checks.append(
                Check(
                    f"install-{name}",
                    "warn",
                    f"{name} not installed (run: advisor install)",
                )
            )
        elif not component.current:
            # Upgrade to a version-aware message when the skill badge is readable.
            if name == "skill" and installed_version and installed_version != current_version:
                checks.append(
                    Check(
                        f"install-{name}",
                        "warn",
                        f"{name} is outdated "
                        f"(installed: {installed_version}, available: {current_version}) "
                        "— run: advisor install",
                    )
                )
            else:
                checks.append(
                    Check(
                        f"install-{name}",
                        "warn",
                        f"{name} is outdated (run: advisor install)",
                    )
                )
        else:
            checks.append(Check(f"install-{name}", "ok", f"{name} installed and current"))
    if status.opt_out:
        checks.append(
            Check(
                "opt-out",
                "warn",
                f"auto-install disabled via {OPT_OUT_ENV}",
            )
        )
    return checks


def _collect_env_overrides() -> dict[str, str]:
    """Return env-var overrides currently in effect (only keys set to non-empty values)."""
    return {k: val for k in _KNOWN_ENV_VARS if (val := os.environ.get(k))}


def run_doctor(
    *,
    nudge_path: Path | None = None,
    skill_path: Path | None = None,
    version: str = "dev",
) -> DoctorReport:
    """Collect every check and return a :class:`DoctorReport`.

    Never raises — every probe is wrapped so a single broken check won't
    block the rest. The returned report is ``healthy=False`` if any check
    is ``fail``; warnings do not flip it.
    """
    quiet = os.environ.get("ADVISOR_QUIET") == "1"
    if not quiet and sys.stderr.isatty():
        from . import _style

        sys.stderr.write(_style.dim("running checks…") + "\r")
        sys.stderr.flush()
    status = install_status(nudge_path=nudge_path, skill_path=skill_path)
    installed = get_installed_skill_version(path=skill_path)
    checks: list[Check] = [
        _check_python_version(),
        _check_git(),
        _check_claude_cli(),
        _check_claude_home(),
        *_check_install(status, installed, version),
    ]
    if not quiet and sys.stderr.isatty():
        sys.stderr.write("\033[2K\r")
        sys.stderr.flush()
    healthy = not any(c.level == "fail" for c in checks)
    return DoctorReport(
        healthy=healthy,
        checks=checks,
        env_overrides=_collect_env_overrides(),
        advisor_version=version,
        python_version=platform.python_version(),
        platform=f"{platform.system()} {platform.release()}",
    )


def format_report(report: DoctorReport) -> str:
    """Human-readable (color-free) rendering of a :class:`DoctorReport`."""
    from . import _style

    lines = [
        _style.header_block(
            f"advisor doctor — {report.advisor_version}",
            [("python", report.python_version), ("platform", report.platform)],
            width=52,
        ),
        "",
    ]
    for check in report.checks:
        _label, fancy, plain, color = _style.STATE_GLYPHS[check.level]
        mark = (
            _style.paint(_style.glyph(fancy, plain), color) if color else _style.glyph(fancy, plain)
        )
        name_col = _style.paint(f"{check.name:<16}", "cyan", "bold")
        lines.append(f"  {mark} {name_col} {check.message}")
    if report.env_overrides:
        lines.append("")
        lines.append(_style.dim("  env overrides in effect:"))
        for k, v in report.env_overrides.items():
            lines.append(_style.dim(f"    {k}={v}"))
    lines.append("")
    footer = "healthy" if report.healthy else "unhealthy — fix the ✗ items above"
    color = "green" if report.healthy else "red"
    lines.append("  " + _style.paint(footer, color, "bold"))
    return "\n".join(lines)
