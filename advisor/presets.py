"""Rule-pack presets — curated TeamConfig tweaks for common stacks.

Presets are pure data. They cannot execute code; they only contribute
additional priority keywords and adjust :class:`~advisor.orchestrate.TeamConfig`
default fields (``file_types``, ``min_priority``, ``test_command``).

Applying a preset does two things at CLI dispatch time:
    1. Replace the default ``--file-types`` / ``--min-priority`` /
       ``--test-cmd`` values when the user didn't override them.
    2. Layer ``extra_keywords_by_tier`` on top of the language-aware
       baseline in :func:`advisor.rank._score_file` — additive, never
       subtractive.

Presets ship six out of the box. Pick via ``advisor plan --preset <name>``
or list with ``advisor presets`` / ``advisor presets --json``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class RulePack:
    """A curated set of tuning knobs for a common stack.

    Attributes:
        name: Short identifier used in ``--preset``. Lower-case, dashed.
        description: One-line description, printed by ``advisor presets``.
        file_types: Glob pattern passed to ``--file-types`` when unset.
        min_priority: Default minimum priority tier.
        extra_keywords_by_tier: Ecosystem-specific keywords layered on
            top of the language baseline, keyed by priority tier.
        test_command: Default ``--test-cmd`` value when unset. ``None``
            disables test orchestration.
        notes: Arbitrary human notes (e.g. caveats, recommended
            companion flags). Printed below the preset's entry.
    """

    name: str
    description: str
    file_types: str
    min_priority: int
    extra_keywords_by_tier: dict[int, tuple[str, ...]]
    test_command: str | None
    notes: tuple[str, ...]


# ── Preset definitions ──────────────────────────────────────────
#
# Keep each preset's keyword list tight: terms that are *diagnostic* of
# the stack's risk surface, not every framework name. Adding too many
# low-value keywords dilutes the ranking (a utility file would start
# scoring as high-priority just by mentioning "request").

PRESETS: Final[dict[str, RulePack]] = {
    "python-web": RulePack(
        name="python-web",
        description="Flask / Django / FastAPI — auth + request handling focus",
        file_types="*.py",
        min_priority=3,
        extra_keywords_by_tier={
            5: ("csrf", "session", "login_required", "jwt", "oauth"),
            4: ("request.form", "request.json", "deserialize", "pickle.loads"),
        },
        test_command="pytest -q",
        notes=("pairs well with `--since origin/main` for PR reviews",),
    ),
    "python-cli": RulePack(
        name="python-cli",
        description="argparse / click CLIs — subprocess + shell focus",
        file_types="*.py",
        min_priority=3,
        extra_keywords_by_tier={
            3: ("subprocess", "shell=True", "os.system"),
        },
        test_command="pytest -q",
        notes=(),
    ),
    "node-api": RulePack(
        name="node-api",
        description="Express / Fastify / Koa — JWT, body parsing, eval surfaces",
        file_types="*.js,*.ts",
        min_priority=3,
        extra_keywords_by_tier={
            5: ("jsonwebtoken", "bcrypt", "session", "cookie-parser"),
            4: ("body-parser", "req.body", "eval", "Function("),
        },
        test_command="npm test",
        notes=(),
    ),
    "typescript-react": RulePack(
        name="typescript-react",
        description="React + TypeScript — DOM sinks and storage",
        file_types="*.ts,*.tsx",
        min_priority=3,
        extra_keywords_by_tier={
            4: ("dangerouslySetInnerHTML", "innerHTML", "href={", "localStorage"),
        },
        test_command="npm test",
        notes=(),
    ),
    "go-service": RulePack(
        name="go-service",
        description="Go services — net/http, database/sql, exec",
        file_types="*.go",
        min_priority=3,
        extra_keywords_by_tier={
            3: ("net/http", "sql.Query", "exec.Command", "unsafe."),
            4: ("ParseForm", "Unmarshal"),
        },
        test_command="go test ./...",
        notes=(),
    ),
    "rust-crate": RulePack(
        name="rust-crate",
        description="Rust libraries / crates — unsafe and unwinding",
        file_types="*.rs",
        min_priority=3,
        extra_keywords_by_tier={
            3: ("unsafe", "transmute", "from_raw", "catch_unwind"),
            4: ("unwrap()",),
        },
        test_command="cargo test",
        notes=("`unwrap()` is flagged P4 — expected in tests, suspicious in prod",),
    ),
}


def get_preset(name: str) -> RulePack:
    """Return the :class:`RulePack` named ``name``.

    Raises :class:`ValueError` with the list of available presets when
    ``name`` is unknown — so the CLI's error message tells the user what
    *is* valid without a separate lookup.
    """
    if name not in PRESETS:
        available = ", ".join(sorted(PRESETS))
        raise ValueError(f"unknown preset {name!r}. available: {available}")
    return PRESETS[name]


def list_presets() -> list[RulePack]:
    """Return all presets, sorted by name."""
    return [PRESETS[name] for name in sorted(PRESETS)]
