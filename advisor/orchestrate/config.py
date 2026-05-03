"""Configuration dataclass for the advisor review team."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

from .. import _style

# Known Claude Code model shortcuts. Per the Agent() tool, only the
# bare-family aliases below and full ``claude-<family>-<version>`` IDs
# (e.g. ``claude-opus-4-7``) are accepted. Mid-form strings like
# ``opus-4-5`` are NOT accepted — they were in earlier versions of this
# whitelist but never verified, and Claude Code rejects them. Use a
# bare alias for "always-latest" or a long-form ID to pin a specific
# version. The regex below covers the long-form path.
KNOWN_MODEL_SHORTCUTS = frozenset(
    {
        "opus",
        "sonnet",
        "haiku",
    }
)

# Pool size ceiling — single source of truth for both the CLI surface
# (``__main__._clamp_max_runners``) and the orchestrate library
# (``runner_prompts._POOL_SIZE_CEILING``). Lives here because
# ``orchestrate.config`` is already imported by both layers; pulling
# the constant into one place removes the silent sync risk noted in
# the pass-Q audit.
POOL_SIZE_CEILING: int = 20

# Long-form Claude model IDs follow ``claude-<family>-<version>-YYYYMMDD``.
# ``<version>`` is one or more dot/dash-separated digit groups (e.g. ``4``,
# ``4-5``, ``4.5``); the trailing ``-YYYYMMDD`` date stamp is optional.
# Anchored to reject leading/trailing dots/dashes and double separators
# that the previous ``[\d.-]+`` would have silently allowed.
_LONG_FORM_MODEL_RE = re.compile(
    r"^claude-(opus|sonnet|haiku)-\d+([.-]\d+)*(-\d{8})?$",
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

    An invalid integer (e.g. ``ADVISOR_MAX_RUNNERS=xyz``) falls back to
    ``default`` — env vars are soft defaults, not hard inputs — but we
    emit a one-line stderr warning so a user who typoed the value sees
    that their override was ignored. The CLI's ``type=_pos_int_arg``
    path errors loudly; this keeps the env-var path equally honest
    without elevating it to a fatal.
    """
    raw = os.environ.get(env_key)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        print(
            _style.warning_box(f"{env_key}={raw!r} is not an integer; using default {default}"),
            file=sys.stderr,
        )
        return default


def default_team_config(
    target_dir: str,
    team_name: str = "review",
    file_types: str = "*.py",
    max_runners: int | None = None,
    min_priority: int = 3,
    context: str = "",
    advisor_model: str = "claude-opus-4-7",
    runner_model: str = "claude-sonnet-4-6",
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

    # NOTE: ``advisor_model="claude-opus-4-7"`` and
    # ``runner_model="claude-sonnet-4-6"`` are the documented default
    # *sentinels* — when a caller leaves these as the literal default
    # strings, env vars are allowed to override. A caller who explicitly
    # wants those exact pinned versions must instead pass them via the
    # env vars or a different surface, since the equality check here
    # treats them as "not set". This is a known ergonomic trade-off: it
    # lets ``ADVISOR_MODEL`` work without forcing every test/caller to
    # thread a ``warn_unknown_model=False``.
    #
    # Long-form IDs are used as defaults because Claude Code's Agent()
    # tool accepts only bare-family aliases (``opus``, ``sonnet``,
    # ``haiku``) and full ``claude-<family>-<version>`` IDs. Short forms
    # like ``opus-4-7`` are NOT accepted by the live tool — pinning the
    # long form guarantees the spawn works on the current CC version
    # and stays at this exact model until someone bumps it.
    if advisor_model == "claude-opus-4-7":
        advisor_model = _env_or("ADVISOR_MODEL", advisor_model)
    if runner_model == "claude-sonnet-4-6":
        runner_model = _env_or("ADVISOR_RUNNER_MODEL", runner_model)
    if max_runners is None:
        raw = _env_int_or("ADVISOR_MAX_RUNNERS", 5)
        if raw < 1:
            # Mirror the explicit-arg branch below so an env-var typo
            # (ADVISOR_MAX_RUNNERS=0) doesn't silently revert to the default.
            print(
                _style.warning_box(f"ADVISOR_MAX_RUNNERS={raw} is < 1; using 5"),
                file=sys.stderr,
            )
        max_runners = raw if raw >= 1 else 5
    elif max_runners < 1:
        # Mirror the ceiling-clamp warning below: silent floor-clamping made
        # ``--max-runners 0`` (or a negative typo) invisible to users, who
        # then wonder why their pool came up at 1.
        print(
            _style.warning_box(f"max_runners={max_runners} is < 1; using 1"),
            file=sys.stderr,
        )
        max_runners = 1
    # Match the CLI's _MAX_RUNNERS_CEILING clamp so the env-var path and
    # explicit-API path can't spawn an unbounded pool through a typo.
    # Surface ceiling hits with a one-line warning — mirrors the CLI's
    # _clamp_max_runners() so users see the same feedback no matter which
    # surface they came in through.
    if max_runners > POOL_SIZE_CEILING:
        print(
            _style.warning_box(
                f"max_runners={max_runners} exceeds ceiling of "
                f"{POOL_SIZE_CEILING}; using {POOL_SIZE_CEILING}"
            ),
            file=sys.stderr,
        )
        max_runners = POOL_SIZE_CEILING
    if file_types_is_default:
        file_types = _env_or("ADVISOR_FILE_TYPES", file_types)
    if min_priority_is_default:
        min_priority = _env_int_or("ADVISOR_MIN_PRIORITY", min_priority)
    # Clamp to the valid P1–P5 range. Argparse guards the CLI, but the
    # env-var path (ADVISOR_MIN_PRIORITY) and direct API callers could
    # otherwise pass anything. Surface the clamp with a warning so a
    # silent typo (ADVISOR_MIN_PRIORITY=10) doesn't masquerade as the
    # configured default — mirrors the max_runners floor/ceiling
    # warnings above so all bound-violations have the same visibility.
    if not 1 <= min_priority <= 5:
        clamped = max(1, min(5, min_priority))
        print(
            _style.warning_box(f"min_priority={min_priority} outside P1–P5; using {clamped}"),
            file=sys.stderr,
        )
        min_priority = clamped
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
        # Re-clamp after the preset merge — a preset that ships an
        # out-of-range ``min_priority`` would otherwise land in
        # :class:`TeamConfig` unclamped.
        min_priority = max(1, min(5, min_priority))

    if warn_unknown_model:
        for label, model in (("advisor_model", advisor_model), ("runner_model", runner_model)):
            if not is_known_model(model):
                msg = (
                    f"{label}={model!r} does not look like a known Claude Code model "
                    f"shortcut or long-form ID; Claude Code may reject it"
                )
                print(_style.warning_box(msg), file=sys.stderr)

    # Floor the runner budget integers at 1 — zero/negative would silently
    # construct a config that then raises inside build_fix_assignment_message
    # on the very first fix (fix_number=1 > effective_cap=0). Clamping here
    # turns a runtime ValueError into a soft floor at construction time.
    max_fixes_per_runner = max(1, max_fixes_per_runner)
    large_file_max_fixes = max(1, large_file_max_fixes)
    large_file_line_threshold = max(1, large_file_line_threshold)
    runner_output_char_ceiling = max(1, runner_output_char_ceiling)
    runner_file_read_ceiling = max(1, runner_file_read_ceiling)

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
