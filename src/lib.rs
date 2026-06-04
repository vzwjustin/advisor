//! advisor — Rust port (in progress).
//!
//! An Opus-led code-review-and-fix pipeline for Claude Code. This crate is a
//! work-in-progress port of the Python `advisor` package; see `RUST_PORT_PLAN.md`
//! for the migration plan and `PORT_NOTES.md` for the current parity status.
//!
//! The modules below mirror their Python counterparts one-to-one. Only the
//! foundational, pure, parity-verified slices are ported so far; the rest remain
//! in Python and ship alongside this binary until parity is proven.

pub mod audit;
pub mod baseline;
pub mod config;
pub mod cost;
pub mod fence;
pub mod focus;
pub mod fs;
pub mod jsonutil;
pub mod models;
pub mod pr_comment;
pub mod presets;
pub mod rank;
pub mod sarif;
pub mod style;
pub mod suppressions;
pub mod verify;
pub mod version;

// Re-export the most-used items at the crate root, mirroring the curated
// surface of `advisor/__init__.py`.
pub use audit::{
    audit_to_dict, audit_transcript, format_audit_report, AuditCheckpoint, AuditReport,
};
pub use baseline::{
    diff_against_baseline, filter_against_baseline, findings_to_entries, read_baseline,
    write_baseline, BaselineDiff, BaselineEntry,
};
pub use config::{
    default_team_config, is_known_model, TeamConfig, TeamConfigInput, DEFAULT_ADVISOR_MODEL,
    DEFAULT_RUNNER_MODEL, POOL_SIZE_CEILING,
};
pub use fence::{fence, sanitize_inline};
pub use focus::{
    create_focus_batches, create_focus_tasks, format_batch_plan, format_dispatch_plan, FocusBatch,
    FocusTask,
};
pub use fs::{normalize_path, validate_file_types, CONTENT_SCAN_LIMIT};
pub use models::{Finding, RankedFile, Severity};
pub use pr_comment::format_pr_comment;
pub use presets::{get_preset, list_presets, RulePack};
pub use rank::{language_for_path, load_advisorignore, rank_files, rank_to_prompt};
pub use sarif::{
    findings_to_sarif, level_for, synthesize_rule_id, SARIF_SCHEMA_URI, SARIF_VERSION,
};
pub use style::strip_ansi;
pub use suppressions::{apply_suppressions, load_suppressions, Suppression};
pub use verify::{
    build_verify_prompt, format_findings_block, parse_findings_from_text, parse_findings_with_drift,
};
pub use version::resolve_version;
