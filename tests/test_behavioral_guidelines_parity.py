"""Catch the "5th surface missed" regression class found in audit pass V.

The Behavioral Guidelines block (4 rules — Think Before Coding /
Simplicity First / Surgical Changes / Goal-Driven Execution) is rolled
out across multiple surfaces. A previous rollout claimed completeness
but silently skipped ``install.py`` NUDGE_BODY — fresh installs went
without the block for a week.

This test pins each surface so a future drop is caught at CI time
rather than during the next adversarial audit.
"""

from __future__ import annotations

from pathlib import Path

from advisor.install import NUDGE_BODY
from advisor.orchestrate import runner_prompts

ADVISOR_TXT = Path(__file__).parent.parent / "advisor" / "orchestrate" / "_prompts" / "advisor.txt"

# These four heading fragments must appear in every surface that ships
# Behavioral Guidelines. Match is case-insensitive to tolerate
# advisor-perspective vs runner-perspective vs user-perspective wording
# (the *intent* is shared; capitalization sometimes shifts).
RULE_HEADINGS = (
    "think before",
    "simplicity first",
    "surgical changes",
    "goal-driven",
)


def _surface_text() -> dict[str, str]:
    """Return ``{surface_name: text}`` for every Behavioral Guidelines surface."""
    return {
        "advisor.txt": ADVISOR_TXT.read_text(encoding="utf-8"),
        "runner_prompts.py": Path(runner_prompts.__file__).read_text(encoding="utf-8"),
        "install.NUDGE_BODY": NUDGE_BODY,
    }


def test_each_surface_has_all_four_rules() -> None:
    for surface, text in _surface_text().items():
        lower = text.lower()
        missing = [h for h in RULE_HEADINGS if h not in lower]
        assert not missing, f"{surface} missing rule headings: {missing}"


def test_no_surface_has_a_rogue_subset() -> None:
    """If only some rules show up, that's worse than zero — silent drift."""
    for surface, text in _surface_text().items():
        lower = text.lower()
        present = [h for h in RULE_HEADINGS if h in lower]
        assert len(present) in (0, 4), (
            f"{surface} has a partial rule set {present} — either include all four or none"
        )
