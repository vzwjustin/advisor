"""Configuration dataclass for the advisor review team."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

from .. import _style

# Known Claude Code model shortcuts (the strings actually accepted by the
# Agent() tool). Long-form IDs like ``claude-opus-4-5-20250929`` are also
# valid — we only warn on something that fails both the shortcut set and
# the long-form shape.
KNOWN_MODEL_SHORTCUTS = frozenset(
    {
        "opus",
        "opus-4",
        "opus-4-5",
        "sonnet",
        "sonnet-4",
        "sonnet-4-5",
        "haiku",
        "haiku-4",
        "haiku-4-5",
    }
)

# Long-form Claude model IDs follow ``claude-<family>-<version>-YYYYMMDD``
_LONG_FORM_MODEL_RE = re.compile(
    r"^claude-(opus|sonnet|haiku)-[\d.-]+(-\d{8})?$",
    re.IGNORECASE,
)


def is_known_model(name: str) -> bool:
    """Return True if ``name`` looks like a valid Claude Code model string.

    Accepts both the short aliases (``opus``, ``sonnet``, …) and the
    long-form ``claude-<family>-<version>-YYYYMMDD`` IDs Anthropic ships
    for API calls. Unknown names are not fatal — the caller decides
    whether to warn.
    """
    if name in KNOWN_MODEL_SHORTCUTS:
        return True
    return bool(_LONG_FORM_MODEL_RE.match(name))


@dataclass(frozen=True, slots=True)
class TeamConfig:
    """Configuration for the advisor review team.

    ``max_fixes_per_runner`` caps sequential fix assignments per runner
    conversation before the advisor rotates to a fresh runner. Prevents
    context-pressure stalls on long fix waves.

    ``test_command`` is an optional shell command (e.g. ``"pytest -q"``)
    that the advisor runs after each fix wave. When set, the advisor
    prompt instructs the team to re-dispatch the failing fix to the
    runner that produced it. An empty string disables test orchestration.
    """

    team_name: str
    target_dir: str
    file_types: str
    max_runners: int
    min_priority: int
    context: str
    advisor_model: str
    runner_model: str
    max_fixes_per_runner: int = 5
    large_file_line_threshold: int = 800
    large_file_max_fixes: int = 3
    test_command: str = ""
    preset: str | None = None
    # Per-runner output character ceiling. Soft nudge at 60%, hard
    # rotate at 80%. 80k chars is ~20k tokens — well inside the safe
    # zone for Sonnet subagents with the rest of the conversation still
    # fitting. Measured in characters (``len(str)``), not bytes — the
    # field is a token-spend proxy, not a storage measurement.
    runner_output_char_ceiling: int = 80_000
    # Distinct-file-read ceiling. Heavy cross-referencing eats context
    # faster than fix-count suggests; this is the secondary proxy from
    # the runner prompt, now enforced on the advisor side too.
    runner_file_read_ceiling: int = 20


def _env_or(env_key: str, default: str) -> str:
    """Return ``os.environ[env_key]`` if set (and non-empty), else default."""
    val = os.environ.get(env_key)
    return val if val else default


def _env_int_or(env_key: str, default: int) -> int:
    """Return ``int(os.environ[env_key])`` if valid, else default.

    An invalid integer (e.g. ``ADVISOR_MAX_RUNNERS=xyz``) is silently
    ignored — we treat env vars as soft defaults, not hard inputs.
    """
    raw = os.environ.get(env_key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def default_team_config(
    target_dir: str,
    team_name: str = "review",
    file_types: str = "*.py",
    max_runners: int | None = None,
    min_priority: int = 3,
    context: str = "",
    advisor_model: str = "opus",
    runner_model: str = "sonnet",
    max_fixes_per_runner: int = 5,
    large_file_line_threshold: int = 800,
    large_file_max_fixes: int = 3,
    test_command: str = "",
    warn_unknown_model: bool = True,
    preset: str | None = None,
    runner_output_char_ceiling: int = 80_000,
    runner_file_read_ceiling: int = 20,
) -> TeamConfig:
    """Create a default team configuration.

    Env-var fallbacks (checked only when the argument is the default):

    * ``ADVISOR_MODEL`` → ``advisor_model``
    * ``ADVISOR_RUNNER_MODEL`` → ``runner_model``
    * ``ADVISOR_MAX_RUNNERS`` → ``max_runners``
    * ``ADVISOR_FILE_TYPES`` → ``file_types``
    * ``ADVISOR_MIN_PRIORITY`` → ``min_priority``
    * ``ADVISOR_TEST_COMMAND`` → ``test_command``

    Env vars are only consulted when the argument is the default sentinel.
    For ``max_runners``, pass ``None`` (the default) to read from env,
    or pass an explicit int to bypass env entirely — explicit always wins.

    ``warn_unknown_model=True`` (default) prints a one-line warning on
    stderr when either model name fails :func:`is_known_model`. The
    config is still constructed — the warning is advisory so callers
    aren't locked out when Anthropic ships a new model ID.
    """
    # Capture "caller left this at the default sentinel" BEFORE any env
    # mutation — otherwise ADVISOR_FILE_TYPES / ADVISOR_MIN_PRIORITY could
    # change the live value and silently shadow the preset below, since
    # the preset-default check uses value equality.
    file_types_is_default = file_types == "*.py"
    min_priority_is_default = min_priority == 3
    test_command_is_default = test_command == ""

    if advisor_model == "opus":
        advisor_model = _env_or("ADVISOR_MODEL", advisor_model)
    if runner_model == "sonnet":
        runner_model = _env_or("ADVISOR_RUNNER_MODEL", runner_model)
    if max_runners is None:
        raw = _env_int_or("ADVISOR_MAX_RUNNERS", 5)
        max_runners = raw if raw >= 1 else 5
    if file_types_is_default:
        file_types = _env_or("ADVISOR_FILE_TYPES", file_types)
    if min_priority_is_default:
        min_priority = _env_int_or("ADVISOR_MIN_PRIORITY", min_priority)
    # Clamp to the valid P1–P5 range. Argparse guards the CLI, but the
    # env-var path (ADVISOR_MIN_PRIORITY) and direct API callers could
    # otherwise pass anything.
    min_priority = max(1, min(5, min_priority))
    if test_command_is_default:
        test_command = _env_or("ADVISOR_TEST_COMMAND", test_command)

    # Preset merge — only fills in fields the caller left at their
    # documented default sentinels. Explicit overrides always win. The
    # checks use the pre-env snapshots so env-derived values don't get
    # clobbered by the preset.
    if preset:
        # Imported lazily so advisor.orchestrate.config has no top-level
        # dependency on advisor.presets (orchestrate sits below presets
        # in the layering).
        from ..presets import get_preset

        pack = get_preset(preset)
        if file_types_is_default and file_types == "*.py":
            file_types = pack.file_types
        if min_priority_is_default and min_priority == 3:
            min_priority = pack.min_priority
        if test_command_is_default and test_command == "" and pack.test_command:
            test_command = pack.test_command

    if warn_unknown_model:
        for label, model in (("advisor_model", advisor_model), ("runner_model", runner_model)):
            if not is_known_model(model):
                msg = (
                    f"{label}={model!r} does not look like a known Claude Code model "
                    f"shortcut or long-form ID; Claude Code may reject it"
                )
                print(_style.warning_box(msg), file=sys.stderr)

    return TeamConfig(
        team_name=team_name,
        target_dir=target_dir,
        file_types=file_types,
        max_runners=max_runners,
        min_priority=min_priority,
        context=context,
        advisor_model=advisor_model,
        runner_model=runner_model,
        max_fixes_per_runner=max_fixes_per_runner,
        large_file_line_threshold=large_file_line_threshold,
        large_file_max_fixes=large_file_max_fixes,
        test_command=test_command,
        preset=preset,
        runner_output_char_ceiling=runner_output_char_ceiling,
        runner_file_read_ceiling=runner_file_read_ceiling,
    )
