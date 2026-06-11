//! Harness subagent types used by the live advisor pipeline.
//!
//! Built-in Cursor / Claude Code types only — custom types like
//! `advisor-executor` or `code-review` are not guaranteed to resolve in every
//! harness and caused false-green installs plus spawn failures.

/// Opus strategist — orchestrates discovery, ranking, verification, synthesis.
pub const ADVISOR_SUBAGENT_TYPE: &str = "generalPurpose";

/// Haiku read-only explorers in the three-tier explore wave.
pub const EXPLORER_SUBAGENT_TYPE: &str = "explore";

/// Sonnet fix workers (and legacy two-tier runners that read + fix).
pub const RUNNER_SUBAGENT_TYPE: &str = "generalPurpose";

/// All subagent types the skill spawns — used by `advisor status` / `doctor`.
pub const HARNESS_AGENT_TYPES: &[(&str, &str)] = &[
    ("advisor", ADVISOR_SUBAGENT_TYPE),
    ("explorer", EXPLORER_SUBAGENT_TYPE),
    ("runner", RUNNER_SUBAGENT_TYPE),
];

/// Deprecated custom types that must not appear in spawn specs.
pub const DEPRECATED_SUBAGENT_TYPES: &[&str] = &["advisor-executor", "code-review"];
