"""Explorer (Haiku) prompts and agent specs for the three-tier pipeline."""

from __future__ import annotations

import sys
from collections.abc import Mapping, Sequence
from functools import lru_cache
from importlib.resources import files

from .. import _style
from ._fence import sanitize_inline
from .config import POOL_SIZE_CEILING, TeamConfig

_POOL_SIZE_CEILING = POOL_SIZE_CEILING


@lru_cache(maxsize=1)
def _load_template() -> str:
    return files("advisor.orchestrate._prompts").joinpath("explorer.txt").read_text(encoding="utf-8")


def _format_target_files(
    target_files: Sequence[str],
    guidance: Mapping[str, str],
) -> str:
    lines: list[str] = []
    for path in target_files:
        g = sanitize_inline(guidance.get(path, "").strip())
        suffix = f" — {g}" if g else ""
        lines.append(f"- `{sanitize_inline(path)}`{suffix}")
    return "\n".join(lines)


def build_explorer_prompt(
    config: TeamConfig,
    target_files: Sequence[str],
    guidance: Mapping[str, str],
    *,
    explorer_id: int = 1,
) -> str:
    """Build a read-only exploration prompt for one Haiku explorer.

    Args:
        config: Team configuration (model, team name, ceilings).
        target_files: File paths this explorer should read.
        guidance: Per-file one-line guidance from the advisor's dispatch plan.
        explorer_id: 1-based explorer identity for the prompt header.
    """
    if target_files:
        files_block = _format_target_files(target_files, guidance)
    else:
        files_block = (
            "_(No files yet — announce ready to team-lead and wait for the "
            "advisor's explore dispatch with your file batch.)_"
        )
    return (
        _load_template()
        .replace("{explorer_id}", str(explorer_id))
        .replace("{team_name}", sanitize_inline(config.team_name))
        .replace("{files_block}", files_block)
    )


def build_explorer_pool_agents(
    config: TeamConfig,
    pool_size: int | None = None,
) -> list[dict[str, object]]:
    """Agent specs for the initial explorer pool (Haiku, read-only)."""
    raw_size = pool_size if pool_size is not None else config.max_explorers
    limit = min(config.max_explorers, _POOL_SIZE_CEILING)
    if raw_size < 0:
        print(
            _style.warning_box(f"pool_size={raw_size} is < 0; using 0"),
            file=sys.stderr,
        )
        raw_size = 0
    if raw_size > limit:
        print(
            _style.warning_box(
                f"pool_size={raw_size} exceeds explorer pool limit of "
                f"{limit}; using {limit}"
            ),
            file=sys.stderr,
        )
    size = min(max(0, raw_size), limit)
    return [
        {
            "description": f"Pool explorer {i} — read-only file exploration",
            "name": f"explorer-{i}",
            "subagent_type": "explorer",
            "model": config.explorer_model,
            "team_name": config.team_name,
            "run_in_background": True,
            "prompt": build_explorer_prompt(config, (), {}, explorer_id=i),
        }
        for i in range(1, size + 1)
    ]
