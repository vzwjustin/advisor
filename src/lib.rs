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
pub mod checkpoint;
pub mod codex_skill;
pub mod config;
pub mod cost;
pub mod doctor;
pub mod fence;
pub mod focus;
pub mod fs;
pub mod git_scope;
pub mod history;
pub mod install;
pub mod jsonutil;
pub mod live;
pub mod models;
pub mod orchestrate;
pub mod pr_comment;
pub mod presets;
pub mod rank;
pub mod runner_budget;
pub mod sarif;
pub mod skill_asset;
pub mod style;
pub mod suppressions;
pub mod verify;
pub mod version;
pub mod web;

// Re-export the most-used items at the crate root, mirroring the curated
// surface of `advisor/__init__.py`.
pub use audit::{
    audit_to_dict, audit_transcript, format_audit_report, AuditCheckpoint, AuditReport,
};
pub use baseline::{
    diff_against_baseline, filter_against_baseline, findings_to_entries, read_baseline,
    write_baseline, BaselineDiff, BaselineEntry,
};
pub use checkpoint::{checkpoint_path, list_checkpoints, load_checkpoint, Checkpoint};
pub use codex_skill::{build_codex_runner_prompt, render_codex_skill_md};
pub use config::{
    default_team_config, is_known_model, normalize_model_id, TeamConfig, TeamConfigInput,
    CLAUDE_CODE_MODEL_PREFIX, DEFAULT_ADVISOR_MODEL, DEFAULT_RUNNER_MODEL, POOL_SIZE_CEILING,
};
pub use cost::{estimate_cost, format_estimate, load_pricing, CostEstimate};
pub use doctor::{format_report, run_doctor, Check, DoctorReport, HealthLevel};
pub use fence::{fence, sanitize_inline};
pub use focus::{
    create_focus_batches, create_focus_tasks, format_batch_plan, format_dispatch_plan, FocusBatch,
    FocusTask,
};
pub use fs::{normalize_path, validate_file_types, CONTENT_SCAN_LIMIT};
pub use git_scope::resolve_git_scope;
pub use history::{
    file_repeat_counts, file_repeat_scores, format_history_block, history_path, load_recent,
    load_recent_findings, new_run_id, summarize, HistoryEntry,
};
pub use install::{
    apply_nudge, check_for_update_cached, get_installed_skill_version, get_status, install,
    install_skill, install_update_skill, invalidate_update_check_cache, parse_badge,
    uninstall_nudge, uninstall_skill, ComponentStatus, InstallAction, InstallResult, Status,
    NUDGE_BODY, OPT_OUT_ENV,
};
pub use install::{check_harness_agent_types, HarnessTypesStatus};
pub use live::{
    append_event, latest_seq, live_events_path, load_recent_events, LIVE_DIR_NAME, LIVE_FILE_NAME,
    LIVE_SCHEMA_VERSION,
};
pub use models::{Finding, RankedFile, Severity};
pub use orchestrate::{
    build_advisor_agent, build_coder_prompt, build_explorer_pool_agents, build_explorer_prompt,
    build_runner_pool_agents, render_pipeline, ADVISOR_SUBAGENT_TYPE, EXPLORER_SUBAGENT_TYPE,
    HARNESS_AGENT_TYPES, RUNNER_SUBAGENT_TYPE,
};
pub use pr_comment::format_pr_comment;
pub use presets::{get_preset, list_presets, RulePack};
pub use rank::{
    language_for_path, load_advisorignore, rank_files, rank_files_with_base, rank_to_prompt,
};
pub use runner_budget::{new_budget, update_budget, BudgetStatus, RunnerBudget, ScopeAnchor};
pub use sarif::{
    findings_to_sarif, level_for, synthesize_rule_id, SARIF_SCHEMA_URI, SARIF_VERSION,
};
pub use skill_asset::{skill_md, skill_md_update, version_badge};
pub use style::strip_ansi;
pub use suppressions::{apply_suppressions, load_suppressions, Suppression};
pub use verify::{
    build_verify_prompt, format_findings_block, parse_findings_from_text, parse_findings_with_drift,
};
pub use version::resolve_version;
