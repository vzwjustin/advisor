"""Configuration dataclass for the advisor review team."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TeamConfig:
    """Configuration for the advisor review team."""

    team_name: str
    target_dir: str
    file_types: str
    max_runners: int
    min_priority: int
    context: str
    advisor_model: str
    runner_model: str
    max_fixes_per_runner: int = 5


def default_team_config(
    target_dir: str,
    team_name: str = "review",
    file_types: str = "*.py",
    max_runners: int = 5,
    min_priority: int = 3,
    context: str = "",
    advisor_model: str = "opus",
    runner_model: str = "sonnet",
    max_fixes_per_runner: int = 5,
) -> TeamConfig:
    """Create a default team configuration.

    ``max_fixes_per_runner`` caps sequential fix assignments per runner
    conversation before the advisor rotates to a fresh runner. Prevents
    context-pressure stalls on long fix waves.
    """
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
    )
