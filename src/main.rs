//! `advisor` CLI binary (Rust port — in progress).
//!
//! Only the subcommands whose behavior has been ported and parity-verified are
//! wired up here. The full argparse surface (see RUST_PORT_PLAN.md §2) is being
//! migrated incrementally; until then the Python CLI remains the reference
//! implementation and ships alongside this binary.

use std::collections::HashSet;
use std::io::{IsTerminal, Read, Write};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::{Parser, Subcommand};

use advisor::audit::AuditCheckpoint;
use advisor::baseline::{
    diff_against_baseline, filter_against_baseline, findings_to_entries, read_baseline,
    write_baseline,
};
use advisor::codex_skill::build_codex_runner_prompt;
use advisor::config::{default_team_config, TeamConfigInput};
use advisor::fence::sanitize_inline;
use advisor::focus::{
    self, create_focus_batches, create_focus_tasks, FocusBatch, FocusTask, DEFAULT_TASK_PROMPT,
};
use advisor::fs::normalize_path;
use advisor::install::{
    codex_cli_available, get_installed_skill_version, get_status, install, install_skill,
    install_update_skill, load_changelog_sections, load_release_notes, uninstall_nudge,
    uninstall_skill, uninstall_update_skill, InstallAction, NUDGE_BODY,
};
use advisor::jsonutil::ensure_ascii;
use advisor::live::{append_event, latest_seq, live_events_path, load_recent_events};
use advisor::models::Severity;
use advisor::orchestrate::advisor_prompt::build_advisor_prompt;
use advisor::orchestrate::runner_prompts::build_runner_pool_prompt;
use advisor::orchestrate::verify_dispatch::build_verify_dispatch_prompt;
use advisor::presets;
use advisor::rank::{self, load_advisorignore, path_matches_file_types, rank_files_with_base};
use advisor::style::{ignore_sigpipe, writeln_stdout, StdoutWrite};
use advisor::verify::{parse_findings_from_text, INCOMPLETE_FILE_PATH};
use advisor::Finding;

/// Print to stdout; return exit 0 when the downstream pipe closed early (PR #1).
macro_rules! outln {
    () => {
        outln!("")
    };
    ($($arg:tt)*) => {
        if writeln_stdout(&format!($($arg)*)) == StdoutWrite::BrokenPipe {
            return ExitCode::SUCCESS;
        }
    };
}

/// Like [`outln!`] but for helpers that do not return [`ExitCode`].
macro_rules! outln_continue {
    () => {
        let _ = writeln_stdout("");
    };
    ($($arg:tt)*) => {
        let _ = writeln_stdout(&format!($($arg)*));
    };
}

/// Max bytes read from stdin / a findings file (`_STDIN_LIMIT`, 50 MiB).
const STDIN_LIMIT: usize = 50 * 1024 * 1024;

#[derive(Parser)]
#[command(
    name = "advisor",
    version,
    about = "Opus-led code-review-and-fix pipeline for Claude Code (Rust port, in progress)"
)]
struct Cli {
    #[command(subcommand)]
    command: Command,
}

#[derive(Subcommand)]
enum Command {
    /// List available rule-pack presets.
    Presets {
        /// Emit the preset catalog as JSON (byte-compatible with the Python CLI).
        #[arg(long)]
        json: bool,
    },
    /// Rank local files and print a dispatch plan — no agents spawned.
    Plan {
        /// Target directory to scan (default: current directory).
        #[arg(default_value = ".")]
        target: String,
        /// Glob pattern(s) for files, comma-separated (e.g. `*.py` or `*.js,*.ts`).
        #[arg(long, default_value = "*.py")]
        file_types: String,
        /// Minimum priority tier (1-5) to include as a task.
        #[arg(long, default_value_t = 3, value_parser = clap::value_parser!(u8).range(1..=5))]
        min_priority: u8,
        /// Group tasks into batches of this size (0 = flat plan).
        #[arg(long, default_value_t = 0)]
        batch_size: usize,
        /// Apply a rule-pack preset (see `advisor presets`).
        #[arg(long)]
        preset: Option<String>,
        /// Emit the plan as JSON (byte-compatible with the Python CLI).
        #[arg(long)]
        json: bool,
        /// Ignore `.advisor/history.jsonl` (deterministic CI).
        #[arg(long)]
        no_history: bool,
        /// Include a token/cost estimate in the JSON output.
        #[arg(long)]
        estimate: bool,
        /// Load model pricing (cents per 1M tokens) from a JSON file.
        #[arg(long)]
        pricing: Option<String>,
        /// Print the default pricing JSON template and exit.
        #[arg(long = "dump-pricing-template")]
        dump_pricing_template: bool,
        /// Scope to files changed since git REF.
        #[arg(long)]
        since: Option<String>,
        /// Scope to currently staged files.
        #[arg(long)]
        staged: bool,
        /// Scope to files changed vs BASE (PR-style).
        #[arg(long)]
        branch: Option<String>,
    },
    /// Snapshot findings as a baseline, or diff current findings against it.
    Baseline {
        #[command(subcommand)]
        action: BaselineAction,
    },
    /// Analyze a run transcript against a saved checkpoint.
    Audit {
        /// Checkpoint run id (see `advisor checkpoints`).
        run_id: String,
        #[arg(default_value = ".")]
        target: String,
        /// Transcript file ("-" = stdin, the default).
        #[arg(long, default_value = "-")]
        transcript: String,
        /// Emit the report as JSON.
        #[arg(long)]
        json: bool,
        /// Write in-batch findings as SARIF 2.1.0 to PATH.
        #[arg(long)]
        sarif: Option<String>,
        /// Exit 4 if any in-batch finding meets/exceeds LEVEL.
        #[arg(long = "fail-on", default_value = "never", value_parser = ["never", "low", "medium", "high", "critical"])]
        fail_on: String,
        /// Output format.
        #[arg(long, default_value = "pretty", value_parser = ["pretty", "json", "pr-comment"])]
        format: String,
        /// Suppress findings matching this baseline JSONL.
        #[arg(long = "baseline")]
        baseline_path: Option<String>,
    },
    /// List or delete saved `.advisor/run-<id>.json` checkpoints.
    Checkpoints {
        #[arg(default_value = ".")]
        target: String,
        /// Delete a single checkpoint by run id.
        #[arg(long)]
        rm: Option<String>,
        /// Delete all checkpoints for the target.
        #[arg(long)]
        clear: bool,
        /// Emit as JSON.
        #[arg(long)]
        json: bool,
    },
    /// Show recent confirmed findings from `.advisor/history.jsonl`.
    History {
        #[arg(default_value = ".")]
        target: String,
        /// Max entries to show.
        #[arg(long, default_value_t = 20)]
        limit: usize,
        /// Show aggregate stats instead of the recent list.
        #[arg(long)]
        stats: bool,
        /// Emit as JSON.
        #[arg(long)]
        json: bool,
    },
    /// Append confirmed findings (stdin JSON/NDJSON) to history.jsonl.
    #[command(name = "history-append")]
    HistoryAppend {
        #[arg(default_value = ".")]
        target: String,
        /// Override the run id (default: a fresh timestamped id).
        #[arg(long = "run-id")]
        run_id: Option<String>,
        /// Skip entries already present in the last 500 (by run_id+path+sev+desc).
        #[arg(long)]
        dedup: bool,
        /// Emit a JSON summary.
        #[arg(long)]
        json: bool,
        /// Suppress the human-readable success line.
        #[arg(long)]
        quiet: bool,
    },
    /// List active (or expired) false-positive suppressions.
    Suppressions {
        #[arg(default_value = ".")]
        target: String,
        /// List all entries (default behavior).
        #[arg(long)]
        list: bool,
        /// Show only expired entries.
        #[arg(long)]
        expired: bool,
        /// Emit as JSON (byte-compatible with the Python CLI).
        #[arg(long)]
        json: bool,
    },
    /// Print the full pipeline reference for a target directory.
    Pipeline {
        /// Target directory (used to fill model names / team name in the reference).
        #[arg(default_value = ".")]
        target: String,
        /// Emit pipeline as JSON.
        #[arg(long)]
        json: bool,
        /// Suppress decorations (CTA/tips).
        #[arg(long)]
        quiet: bool,
        /// Override the advisor model.
        #[arg(long)]
        advisor_model: Option<String>,
        /// Override the runner model.
        #[arg(long)]
        runner_model: Option<String>,
        /// Team name.
        #[arg(long, default_value = "review")]
        team: String,
    },
    /// Print the strict team-lifecycle protocol as an ad-hoc reference.
    Protocol {
        /// Emit protocol as JSON.
        #[arg(long)]
        json: bool,
        /// Suppress the trailing CTA line.
        #[arg(long)]
        quiet: bool,
    },
    /// Print version and environment details.
    Version {
        /// Emit as JSON.
        #[arg(long)]
        json: bool,
    },
    /// Emit / inspect the live event stream (.advisor/live/events.jsonl).
    Live {
        #[command(subcommand)]
        action: LiveAction,
    },
    /// Print a step prompt for pasting into Claude Code.
    Prompt {
        /// Which step: advisor, runner, verify.
        #[arg(value_parser = ["advisor", "runner", "verify"])]
        step: String,
        /// Target directory.
        #[arg(default_value = ".")]
        target: String,
        /// Runner number (only used with --step runner).
        #[arg(long, default_value_t = 1)]
        runner_id: usize,
        /// File count hint for verify prompt.
        #[arg(long)]
        file_count: Option<usize>,
        /// Max runners hint for verify prompt.
        #[arg(long, default_value_t = 5)]
        max_runners: usize,
        /// Emit as JSON.
        #[arg(long)]
        json: bool,
        /// Suppress TTY framing.
        #[arg(long)]
        quiet: bool,
        /// Override the advisor model.
        #[arg(long)]
        advisor_model: Option<String>,
        /// Override the runner model.
        #[arg(long)]
        runner_model: Option<String>,
        /// Team name.
        #[arg(long, default_value = "review")]
        team: String,
        /// Ignore .advisor/history.jsonl.
        #[arg(long)]
        no_history: bool,
        /// Apply a rule-pack preset.
        #[arg(long)]
        preset: Option<String>,
        /// Glob pattern(s) for files.
        #[arg(long, default_value = "*.py")]
        file_types: String,
        /// User goal / review focus (use `-` to read from stdin).
        #[arg(long)]
        context: Option<String>,
    },
    /// Install the /advisor skill AND append the CLAUDE.md nudge.
    Install {
        /// Path to CLAUDE.md (default: ~/.claude/CLAUDE.md).
        #[arg(long)]
        path: Option<String>,
        /// Path to SKILL.md (default: ~/.claude/skills/advisor/SKILL.md).
        #[arg(long = "skill-path")]
        skill_path: Option<String>,
        /// Check install health only — do not write.
        #[arg(long)]
        check: bool,
        /// Emit as JSON.
        #[arg(long)]
        json: bool,
        /// Suppress decorations.
        #[arg(long)]
        quiet: bool,
        /// Exit 3 if not fully installed.
        #[arg(long)]
        strict: bool,
        /// Skip writing the SKILL.md (nudge only).
        #[arg(long = "skip-skill")]
        skip_skill: bool,
    },
    /// Remove the /advisor skill and CLAUDE.md nudge block.
    Uninstall {
        /// Path to CLAUDE.md.
        #[arg(long)]
        path: Option<String>,
        /// Path to SKILL.md.
        #[arg(long = "skill-path")]
        skill_path: Option<String>,
        /// Emit as JSON.
        #[arg(long)]
        json: bool,
        /// Suppress decorations.
        #[arg(long)]
        quiet: bool,
    },
    /// Print a health summary of the local advisor install.
    Status {
        /// Path to CLAUDE.md.
        #[arg(long)]
        path: Option<String>,
        /// Path to SKILL.md.
        #[arg(long = "skill-path")]
        skill_path: Option<String>,
        /// Emit as JSON.
        #[arg(long)]
        json: bool,
        /// Suppress decorations.
        #[arg(long)]
        quiet: bool,
        /// Exit 3 if not healthy.
        #[arg(long)]
        strict: bool,
    },
    /// Extended diagnostics: status + git/Claude/env checks.
    Doctor {
        /// Path to CLAUDE.md.
        #[arg(long)]
        path: Option<String>,
        /// Path to SKILL.md.
        #[arg(long = "skill-path")]
        skill_path: Option<String>,
        /// Emit as JSON.
        #[arg(long)]
        json: bool,
        /// Suppress decorations.
        #[arg(long)]
        quiet: bool,
        /// Exit 3 if not healthy.
        #[arg(long)]
        strict: bool,
    },
    /// Print bundled CHANGELOG entries — single version or --since X.Y.Z.
    Changelog {
        /// Print notes for this specific version (e.g. 0.8.4).
        version: Option<String>,
        /// Only show sections strictly newer than this version.
        #[arg(long)]
        since: Option<String>,
        /// Emit as JSON.
        #[arg(long)]
        json: bool,
        /// Suppress trailing CTA.
        #[arg(long)]
        quiet: bool,
    },
    /// Self-upgrade advisor-agent to the latest PyPI release.
    Update {
        /// Skip confirmation prompt.
        #[arg(long)]
        yes: bool,
        /// Suppress output.
        #[arg(long)]
        quiet: bool,
        /// Emit as JSON.
        #[arg(long)]
        json: bool,
    },
    /// Launch the local web dashboard (127.0.0.1).
    Ui {
        /// Target directory.
        #[arg(default_value = ".")]
        target: String,
        /// Port to listen on (0 = OS-assigned).
        #[arg(long, default_value_t = 7070)]
        port: u16,
        /// Host to bind.
        #[arg(long, default_value = "127.0.0.1")]
        host: String,
        /// Emit startup info as JSON (no server launched).
        #[arg(long)]
        json: bool,
        /// Suppress startup banner.
        #[arg(long)]
        quiet: bool,
    },
    /// Emit a Codex-flavored dispatch CSV (one row per runner batch).
    #[command(name = "codex-plan-csv")]
    CodexPlanCsv {
        /// Target directory to scan.
        #[arg(default_value = ".")]
        target: String,
        /// Glob pattern(s) for files, comma-separated.
        #[arg(long, default_value = "*.py")]
        file_types: String,
        /// Minimum priority tier (1-5) to include.
        #[arg(long, default_value_t = 3, value_parser = clap::value_parser!(u8).range(1..=5))]
        min_priority: u8,
        /// Files per batch (default 5).
        #[arg(long, default_value_t = 5)]
        batch_size: usize,
        /// Apply a rule-pack preset.
        #[arg(long)]
        preset: Option<String>,
        /// Write CSV to PATH instead of a tempfile.
        #[arg(long)]
        out: Option<String>,
        /// Suppress the Codex-not-on-PATH warning.
        #[arg(long)]
        quiet: bool,
        /// Scope to files changed since git REF.
        #[arg(long)]
        since: Option<String>,
        /// Scope to currently staged files.
        #[arg(long)]
        staged: bool,
        /// Scope to files changed vs BASE.
        #[arg(long)]
        branch: Option<String>,
    },
}

#[derive(Subcommand)]
enum LiveAction {
    /// Append one event to the live stream.
    Record {
        #[arg(default_value = ".")]
        target: String,
        /// Event kind (e.g. run_start, report_relay).
        #[arg(long)]
        kind: String,
        /// JSON object payload ("-" = read from stdin).
        #[arg(long)]
        data: Option<String>,
        /// Emit as JSON.
        #[arg(long)]
        json: bool,
        /// Suppress the success line.
        #[arg(long)]
        quiet: bool,
    },
    /// Print recent events.
    Tail {
        #[arg(default_value = ".")]
        target: String,
        /// Max events to show.
        #[arg(long, default_value_t = 50)]
        limit: usize,
        /// Only show events after this sequence number.
        #[arg(long)]
        since: Option<i64>,
        /// Emit as JSON.
        #[arg(long)]
        json: bool,
    },
    /// Delete the live events file.
    Clear {
        #[arg(default_value = ".")]
        target: String,
        /// Suppress the success line.
        #[arg(long)]
        quiet: bool,
    },
}

#[derive(Subcommand)]
enum BaselineAction {
    /// Snapshot findings (stdin or --from) as the accepted baseline.
    Create {
        #[arg(default_value = ".")]
        target: String,
        /// Read findings from PATH (JSON or markdown) instead of stdin.
        #[arg(long = "from")]
        from_file: Option<String>,
        /// Write the baseline to PATH (default: <target>/.advisor/baseline.jsonl).
        #[arg(long)]
        output: Option<String>,
        /// Suppress the success line.
        #[arg(long)]
        quiet: bool,
    },
    /// Diff current findings (stdin or --from) against a saved baseline.
    Diff {
        #[arg(default_value = ".")]
        target: String,
        /// Read findings from PATH (JSON or markdown) instead of stdin.
        #[arg(long = "from")]
        from_file: Option<String>,
        /// Read the baseline from PATH (default: <target>/.advisor/baseline.jsonl).
        #[arg(long = "baseline")]
        baseline_path: Option<String>,
        /// Emit the diff as JSON (byte-compatible with the Python CLI).
        #[arg(long)]
        json: bool,
    },
}

fn main() -> ExitCode {
    ignore_sigpipe();
    let cli = Cli::parse();
    match cli.command {
        Command::Presets { json } => cmd_presets(json),
        Command::Plan {
            target,
            file_types,
            min_priority,
            batch_size,
            preset,
            json,
            no_history,
            estimate,
            pricing,
            dump_pricing_template,
            since,
            staged,
            branch,
        } => cmd_plan(PlanArgs {
            target,
            file_types,
            min_priority,
            batch_size,
            preset,
            json,
            no_history,
            estimate,
            pricing,
            dump_pricing_template,
            since,
            staged,
            branch,
        }),
        Command::Baseline { action } => cmd_baseline(action),
        Command::Audit {
            run_id,
            target,
            transcript,
            json,
            sarif,
            fail_on,
            format,
            baseline_path,
        } => cmd_audit(AuditArgs {
            run_id,
            target,
            transcript,
            json,
            sarif,
            fail_on,
            format,
            baseline_path,
        }),
        Command::Checkpoints {
            target,
            rm,
            clear,
            json,
        } => cmd_checkpoints(&target, rm, clear, json),
        Command::History {
            target,
            limit,
            stats,
            json,
        } => cmd_history(&target, limit, stats, json),
        Command::HistoryAppend {
            target,
            run_id,
            dedup,
            json,
            quiet,
        } => cmd_history_append(&target, run_id, dedup, json, quiet),
        Command::Suppressions {
            target,
            list,
            expired,
            json,
        } => {
            let _ = list; // default-on flag, no behavior change
            cmd_suppressions(&target, expired, json)
        }
        Command::Install {
            path,
            skill_path,
            check,
            json,
            quiet,
            strict,
            skip_skill,
        } => cmd_install(path, skill_path, check, json, quiet, strict, skip_skill),
        Command::Uninstall {
            path,
            skill_path,
            json,
            quiet,
        } => cmd_uninstall(path, skill_path, json, quiet),
        Command::Status {
            path,
            skill_path,
            json,
            quiet,
            strict,
        } => cmd_status(path, skill_path, json, quiet, strict),
        Command::Doctor {
            path,
            skill_path,
            json,
            quiet,
            strict,
        } => cmd_doctor(path, skill_path, json, quiet, strict),
        Command::Changelog {
            version,
            since,
            json,
            quiet,
        } => cmd_changelog(version, since, json, quiet),
        Command::Update { yes, quiet, json } => cmd_update(yes, quiet, json),
        Command::Ui {
            target,
            port,
            host,
            json,
            quiet,
        } => cmd_ui(&target, port, &host, json, quiet),
        Command::Pipeline {
            target,
            json,
            quiet,
            advisor_model,
            runner_model,
            team,
        } => cmd_pipeline(&target, json, quiet, advisor_model, runner_model, &team),
        Command::Protocol { json, quiet } => cmd_protocol(json, quiet),
        Command::Version { json } => cmd_version(json),
        Command::Live { action } => cmd_live(action),
        Command::Prompt {
            step,
            target,
            runner_id,
            file_count,
            max_runners,
            json,
            quiet,
            advisor_model,
            runner_model,
            team,
            no_history,
            preset,
            file_types,
            context,
        } => cmd_prompt(PromptArgs {
            step,
            target,
            runner_id,
            file_count,
            max_runners,
            json,
            quiet,
            advisor_model,
            runner_model,
            team,
            no_history,
            preset,
            file_types,
            context,
        }),
        Command::CodexPlanCsv {
            target,
            file_types,
            min_priority,
            batch_size,
            preset,
            out,
            quiet,
            since,
            staged,
            branch,
        } => cmd_codex_plan_csv(CodexPlanCsvArgs {
            target,
            file_types,
            min_priority,
            batch_size,
            preset,
            out,
            quiet,
            since,
            staged,
            branch,
        }),
    }
}

/// Validate + normalize one incoming finding dict into a `HistoryEntry`.
/// Mirrors `_coerce_finding_for_append` (errors are user-facing → exit 2).
fn coerce_finding_for_append(
    payload: &serde_json::Value,
    default_run_id: &str,
    default_ts: &str,
) -> Result<advisor::history::HistoryEntry, String> {
    let map = payload
        .as_object()
        .ok_or_else(|| "expected JSON object".to_string())?;
    let req = |k: &str| -> Result<String, String> {
        match map.get(k).and_then(|v| v.as_str()) {
            Some(s) if !s.trim().is_empty() => Ok(s.to_string()),
            _ => Err(format!("missing or empty required field: {k:?}")),
        }
    };
    let file_path = req("file_path")?;
    let _ = req("description")?;
    let severity_raw = req("severity")?;
    let severity = severity_raw.trim().to_uppercase();
    if !matches!(severity.as_str(), "CRITICAL" | "HIGH" | "MEDIUM" | "LOW") {
        return Err(format!(
            "severity {severity_raw:?} not in ['CRITICAL', 'HIGH', 'LOW', 'MEDIUM']"
        ));
    }
    let status_raw = map
        .get("status")
        .and_then(|v| v.as_str())
        .unwrap_or("CONFIRMED");
    let status = status_raw.trim().to_uppercase();
    if !matches!(status.as_str(), "CONFIRMED" | "FIXED" | "REJECTED") {
        return Err(format!(
            "status {status_raw:?} not in ['CONFIRMED', 'FIXED', 'REJECTED']"
        ));
    }
    let run_id = map
        .get("run_id")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or(default_run_id)
        .trim()
        .to_string();
    let timestamp = map
        .get("timestamp")
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty())
        .unwrap_or(default_ts)
        .trim()
        .to_string();
    let description = map
        .get("description")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .trim()
        .to_string();
    let opt = |k: &str| {
        map.get(k)
            .and_then(|v| v.as_str())
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
    };
    Ok(advisor::history::HistoryEntry {
        timestamp,
        file_path: file_path.trim().to_string(),
        severity,
        description,
        status,
        run_id,
        schema_version: map
            .get("schema_version")
            .and_then(|v| v.as_str())
            .unwrap_or("1.0")
            .to_string(),
        evidence: opt("evidence"),
        fix: opt("fix"),
        rule_id: opt("rule_id"),
        tool: opt("tool"),
    })
}

/// `advisor history-append` — append confirmed findings from stdin. Mirrors
/// `cmd_history_append`.
fn cmd_history_append(
    target_str: &str,
    run_id: Option<String>,
    dedup: bool,
    json: bool,
    quiet: bool,
) -> ExitCode {
    let target = Path::new(target_str);
    let mut buf = Vec::new();
    if std::io::stdin()
        .take((STDIN_LIMIT + 1) as u64)
        .read_to_end(&mut buf)
        .is_err()
    {
        eprintln!("✗ failed reading stdin");
        return ExitCode::from(2);
    }
    if buf.len() > STDIN_LIMIT {
        eprintln!("✗ advisor history-append input exceeds {STDIN_LIMIT}-byte cap");
        return ExitCode::from(2);
    }
    let raw = String::from_utf8_lossy(&buf).into_owned();
    if raw.trim().is_empty() {
        eprintln!("no JSON input on stdin");
        return ExitCode::from(2);
    }
    let mut payloads: Vec<serde_json::Value> = Vec::new();
    if raw.trim_start().starts_with('[') {
        match serde_json::from_str::<serde_json::Value>(&raw) {
            Ok(serde_json::Value::Array(a)) => payloads = a,
            Ok(_) => {
                eprintln!("top-level JSON must be array or object");
                return ExitCode::from(2);
            }
            Err(e) => {
                eprintln!("invalid JSON array: {e}");
                return ExitCode::from(2);
            }
        }
    } else {
        for (i, line) in raw.lines().enumerate() {
            let s = line.trim();
            if s.is_empty() {
                continue;
            }
            match serde_json::from_str::<serde_json::Value>(s) {
                Ok(v) => payloads.push(v),
                Err(e) => {
                    eprintln!("line {}: invalid JSON: {e}", i + 1);
                    return ExitCode::from(2);
                }
            }
        }
    }
    if payloads.is_empty() {
        eprintln!("no findings parsed from stdin");
        return ExitCode::from(2);
    }
    let default_run_id = run_id.unwrap_or_else(advisor::history::new_run_id);
    let default_run_id = default_run_id.trim().to_string();
    // Default timestamp = now (seconds precision; explicit timestamps recommended
    // for reproducible writes — see PORT_NOTES re microsecond-precision default).
    let default_ts = advisor::history::entry_now("", "LOW", "", "CONFIRMED", "").timestamp;
    let mut entries = Vec::new();
    for p in &payloads {
        match coerce_finding_for_append(p, &default_run_id, &default_ts) {
            Ok(e) => entries.push(e),
            Err(msg) => {
                eprintln!("{msg}");
                return ExitCode::from(2);
            }
        }
    }
    if dedup {
        let existing =
            advisor::history::load_recent_findings(&advisor::history::history_path(target), 500);
        let mut seen: HashSet<(String, String, String, String)> = existing
            .iter()
            .map(|e| {
                (
                    e.run_id.clone(),
                    normalize_path(&e.file_path),
                    e.severity.clone(),
                    e.description.clone(),
                )
            })
            .collect();
        entries.retain(|e| {
            let key = (
                e.run_id.clone(),
                normalize_path(&e.file_path),
                e.severity.clone(),
                e.description.clone(),
            );
            seen.insert(key)
        });
    }
    if entries.is_empty() {
        if json {
            outln!(
                "{{\"appended\": 0, \"run_id\": {}}}",
                json_string(&default_run_id)
            );
        } else if !quiet {
            outln!("nothing to append (all entries deduped)");
        }
        return ExitCode::SUCCESS;
    }
    let path = match advisor::history::append_entries(target, &entries) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("✗ {e}");
            return ExitCode::from(2);
        }
    };
    if json {
        outln!(
            "{{\"schema_version\": \"1.0\", \"appended\": {}, \"run_id\": {}, \"history_path\": {}}}",
            entries.len(),
            json_string(&default_run_id),
            json_string(&path.to_string_lossy())
        );
    } else if !quiet {
        outln!(
            "appended {} finding(s) to {}",
            entries.len(),
            path.display()
        );
    }
    ExitCode::SUCCESS
}

/// Minimal JSON string literal (ASCII-escaped) for compact summary output.
fn json_string(s: &str) -> String {
    ensure_ascii(&serde_json::Value::String(s.to_string()).to_string())
}

/// `advisor checkpoints` — list / delete saved run checkpoints. Mirrors
/// `cmd_checkpoints` (JSON paths; pretty list deferred).
fn cmd_checkpoints(target_str: &str, rm: Option<String>, clear: bool, json: bool) -> ExitCode {
    let target = Path::new(target_str);
    let target_abs = target
        .canonicalize()
        .unwrap_or_else(|_| target.to_path_buf());
    if rm.is_some() && clear {
        eprintln!("✗ --rm and --clear are mutually exclusive");
        return ExitCode::from(2);
    }
    if let Some(id) = rm {
        let path = match advisor::checkpoint::checkpoint_path(target, &id) {
            Ok(p) => p,
            Err(e) => {
                eprintln!("✗ {e}");
                return ExitCode::from(2);
            }
        };
        if path.exists() {
            if let Err(e) = std::fs::remove_file(&path) {
                eprintln!("✗ {e}");
                return ExitCode::from(2);
            }
            outln!("✓ removed checkpoint {id}");
        } else {
            outln!("no checkpoint {id} at {}", path.display());
        }
        return ExitCode::SUCCESS;
    }
    if clear {
        let ids = advisor::checkpoint::list_checkpoints(target);
        let mut removed = 0;
        let mut failed = 0;
        for rid in &ids {
            if let Ok(p) = advisor::checkpoint::checkpoint_path(target, rid) {
                match std::fs::remove_file(&p) {
                    Ok(()) => removed += 1,
                    Err(_) => failed += 1,
                }
            }
        }
        if json {
            let payload = serde_json::json!({
                "schema_version": "1.0",
                "target": target_abs.to_string_lossy(),
                "removed": removed,
                "failed": failed,
            });
            outln!(
                "{}",
                ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
            );
        } else if removed > 0 || failed > 0 {
            let noun = if removed == 1 {
                "checkpoint"
            } else {
                "checkpoints"
            };
            let mut msg = format!("removed {removed} {noun}");
            if failed > 0 {
                msg.push_str(&format!(", failed {failed}"));
            }
            outln!("{msg}");
        } else {
            outln!("no checkpoints to remove");
        }
        return ExitCode::from(if failed == 0 { 0 } else { 1 });
    }
    let ids = advisor::checkpoint::list_checkpoints(target);
    if json {
        let payload = serde_json::json!({
            "schema_version": "1.0",
            "target": target_abs.to_string_lossy(),
            "count": ids.len(),
            "run_ids": ids,
        });
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
        return ExitCode::SUCCESS;
    }
    if ids.is_empty() {
        outln!("no checkpoints yet at {}/.advisor/", target.display());
        return ExitCode::SUCCESS;
    }
    outln!("## Checkpoints ({})", ids.len());
    for id in &ids {
        outln!("- `{id}`");
    }
    ExitCode::SUCCESS
}

/// `advisor history` — recent confirmed findings or aggregate stats. Mirrors
/// `cmd_history` for the JSON paths (pretty/colorized output deferred).
fn cmd_history(target_str: &str, limit: usize, stats: bool, json: bool) -> ExitCode {
    let target = Path::new(target_str);
    let target_abs = target
        .canonicalize()
        .unwrap_or_else(|_| target.to_path_buf());
    if stats {
        let all =
            advisor::history::load_recent_findings(&advisor::history::history_path(target), 500);
        let summary = advisor::history::summarize(&all, 10);
        if json {
            let payload = serde_json::json!({
                "schema_version": "1.0",
                "target": target_abs.to_string_lossy(),
                "stats": summary,
            });
            outln!(
                "{}",
                ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
            );
            return ExitCode::SUCCESS;
        }
        // Pretty stats output (colorized framing) is deferred; emit the JSON.
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&summary).unwrap_or_default())
        );
        return ExitCode::SUCCESS;
    }
    let entries = advisor::history::load_recent(target, limit);
    if json {
        let payload = serde_json::json!({
            "schema_version": "1.0",
            "target": target_abs.to_string_lossy(),
            "count": entries.len(),
            "entries": entries.iter().map(|e| serde_json::json!({
                "timestamp": e.timestamp,
                "file_path": e.file_path,
                "severity": e.severity,
                "description": e.description,
                "status": e.status,
                "run_id": e.run_id,
            })).collect::<Vec<_>>(),
        });
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
        return ExitCode::SUCCESS;
    }
    if entries.is_empty() {
        outln!(
            "no history yet at {}/.advisor/history.jsonl",
            target.display()
        );
        return ExitCode::SUCCESS;
    }
    print!("{}", advisor::history::format_history_block(&entries));
    ExitCode::SUCCESS
}

/// `advisor suppressions` — list active/expired suppressions. Mirrors
/// `cmd_suppressions`.
fn cmd_suppressions(target: &str, expired_only: bool, json: bool) -> ExitCode {
    let path = Path::new(target)
        .join(".advisor")
        .join("suppressions.jsonl");
    if !path.exists() {
        outln!("no suppressions file at {}", path.display());
        return ExitCode::SUCCESS;
    }
    let mut entries = match advisor::suppressions::load_suppressions(&path) {
        Ok(e) => e,
        Err(e) => {
            eprintln!("✗ {e}");
            return ExitCode::from(2);
        }
    };
    if expired_only {
        entries.retain(|e| e.expired);
    }
    if json {
        let payload = serde_json::json!({
            "schema_version": "1.0",
            "count": entries.len(),
            "entries": entries.iter().map(|e| serde_json::json!({
                "rule_id": e.rule_id,
                "file": e.file,
                "file_glob": e.file_glob,
                "reason": e.reason,
                "until": e.until,
                "expired": e.expired,
            })).collect::<Vec<_>>(),
        });
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
        return ExitCode::SUCCESS;
    }
    if entries.is_empty() {
        let label = if expired_only {
            "expired suppressions"
        } else {
            "suppressions"
        };
        outln!("no {label} in {}", path.display());
        return ExitCode::SUCCESS;
    }
    let mut lines = vec![
        format!("## Suppressions ({})", entries.len()),
        String::new(),
    ];
    for e in &entries {
        let scope = e
            .file
            .clone()
            .unwrap_or_else(|| format!("glob:{}", e.file_glob.clone().unwrap_or_default()));
        let stamp = e
            .until
            .as_ref()
            .map(|u| format!(" until {u}"))
            .unwrap_or_default();
        let mark = if e.expired { " (expired)" } else { "" };
        lines.push(format!("- `{}` → `{scope}`{stamp}{mark}", e.rule_id));
        if !e.reason.is_empty() {
            lines.push(format!("  - _{}_", e.reason));
        }
    }
    // Python: print(colorize_markdown("\n".join(lines) + "\n")) → body + "\n\n".
    let body = format!("{}\n\n", lines.join("\n"));
    print!("{body}");
    ExitCode::SUCCESS
}

/// `advisor presets [--json]` — mirrors the Python CLI handler.
fn cmd_presets(json: bool) -> ExitCode {
    let packs = presets::list_presets();
    if json {
        outln!("{}", presets::presets_json(&packs));
    } else {
        // `presets_pretty` already includes the trailing newline structure.
        print!("{}", presets::presets_pretty(&packs));
    }
    ExitCode::SUCCESS
}

struct PlanArgs {
    target: String,
    file_types: String,
    min_priority: u8,
    batch_size: usize,
    preset: Option<String>,
    json: bool,
    no_history: bool,
    estimate: bool,
    pricing: Option<String>,
    dump_pricing_template: bool,
    since: Option<String>,
    staged: bool,
    branch: Option<String>,
}

/// `advisor plan --dump-pricing-template` payload (mirrors the Python handler).
fn dump_pricing_template() -> String {
    let payload = serde_json::json!({
        "_comment": "values are cents per 1M tokens — verify at https://www.anthropic.com/pricing; opus/sonnet/haiku are all required, extra keys (like this one) are ignored",
        "opus": {"input": 1500, "output": 7500},
        "sonnet": {"input": 300, "output": 1500},
        "haiku": {"input": 25, "output": 125},
    });
    ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
}

/// `advisor plan` — discovery → rank → focus → JSON/pretty. Mirrors `cmd_plan`
/// for the core (non-resume, non-checkpoint) path. Git scope, history-informed
/// ranking, preset overlay, and cost estimate are all wired.
fn cmd_plan(args: PlanArgs) -> ExitCode {
    // `--dump-pricing-template` short-circuits all discovery.
    if args.dump_pricing_template {
        outln!("{}", dump_pricing_template());
        return ExitCode::SUCCESS;
    }

    // Load a `--pricing FILE` override up front (parse errors fail fast).
    let pricing_override = match &args.pricing {
        Some(p) => match advisor::cost::load_pricing(Path::new(p)) {
            Ok(map) => Some(map),
            Err(e) => {
                eprintln!("✗ {e}");
                return ExitCode::from(2);
            }
        },
        None => None,
    };

    let target = Path::new(&args.target);
    if !target.exists() {
        eprintln!("✗ target not found: {}", target.display());
        return ExitCode::from(2);
    }
    if !target.is_dir() {
        eprintln!(
            "✗ target must be a directory, got file: {}",
            target.display()
        );
        return ExitCode::from(2);
    }

    // Resolve config (preset merge fills file_types / min_priority defaults).
    let mut input = TeamConfigInput::new(args.target.clone());
    input.file_types = args.file_types.clone();
    input.min_priority = args.min_priority as i64;
    input.preset = args.preset.clone();
    let cfg = default_team_config(input);

    // Preset keyword overlay for ranking.
    let preset_extras: Option<Vec<(i64, Vec<String>)>> = match &args.preset {
        Some(name) => match presets::get_preset(name) {
            Ok(pack) => Some(
                pack.extra_keywords_by_tier
                    .iter()
                    .map(|(tier, kws)| (*tier, kws.iter().map(|s| s.to_string()).collect()))
                    .collect(),
            ),
            Err(e) => {
                eprintln!("✗ {e}");
                return ExitCode::from(2);
            }
        },
        None => None,
    };

    // Git-scope selectors take precedence over the recursive scan. Mirrors
    // `_resolve_plan_files`: filter git output by --file-types (fnmatch on the
    // filename) and intersect with the target directory.
    let git_selected = args.since.is_some() || args.staged || args.branch.is_some();
    let paths = if git_selected {
        match advisor::git_scope::resolve_git_scope(
            target,
            args.since.as_deref(),
            args.staged,
            args.branch.as_deref(),
        ) {
            Ok(Some(files)) => filter_git_scope(target, files, &cfg.file_types),
            Ok(None) => Vec::new(),
            Err(e) => {
                eprintln!("✗ {e}");
                return ExitCode::from(2);
            }
        }
    } else {
        match discover(target, &cfg.file_types) {
            Ok(p) => p,
            Err(e) => {
                eprintln!("✗ {e}");
                return ExitCode::from(2);
            }
        }
    };

    let ignore = load_advisorignore(&args.target);
    let read = |p: &str| read_head(p);

    // History-informed ranking (E9): repeat offenders float up. `--no-history`
    // disables it for deterministic CI. Mirrors cmd_plan's history block.
    let (history_scores, history_counts) = if args.no_history {
        (None, None)
    } else {
        let entries =
            advisor::history::load_recent_findings(&advisor::history::history_path(target), 500);
        if entries.is_empty() {
            (None, None)
        } else {
            (
                Some(advisor::history::file_repeat_scores(&entries, 30.0, None)),
                Some(advisor::history::file_repeat_counts(&entries, 90.0, None)),
            )
        }
    };

    let ranked = rank_files_with_base(
        &paths,
        Some(&read),
        &ignore,
        preset_extras.as_deref(),
        history_scores.as_ref(),
        history_counts.as_ref(),
        90,
        target,
    );
    let tasks = create_focus_tasks(&ranked, None, cfg.min_priority as u8, DEFAULT_TASK_PROMPT);
    let batches: Option<Vec<FocusBatch>> = if args.batch_size > 0 {
        // create_focus_batches only errors on files_per_batch < 1 (guarded) or
        // an invalid forced complexity (not used here), so this is infallible.
        create_focus_batches(&tasks, args.batch_size, focus::AUTO_COMPLEXITY).ok()
    } else {
        None
    };

    if args.json {
        let target_abs = target
            .canonicalize()
            .unwrap_or_else(|_| target.to_path_buf());
        // Optional cost estimate (Python passes target=None, so files are
        // stat'd by their absolute path directly).
        let estimate = if args.estimate {
            match advisor::cost::estimate_cost(
                &tasks,
                batches.as_deref(),
                &cfg.advisor_model,
                &cfg.runner_model,
                cfg.max_fixes_per_runner,
                Some(cfg.max_runners),
                &cfg.explorer_model,
                cfg.max_explorers,
                pricing_override.as_ref(),
                None,
            ) {
                Ok(est) => Some(est.to_dict()),
                Err(e) => {
                    eprintln!("✗ {e}");
                    return ExitCode::from(2);
                }
            }
        } else {
            None
        };
        outln!(
            "{}",
            plan_json(&target_abs, &tasks, batches.as_deref(), estimate)
        );
    } else if let Some(b) = &batches {
        print!("{}", focus::format_batch_plan(b));
    } else {
        print!("{}", focus::format_dispatch_plan(&tasks));
    }
    ExitCode::SUCCESS
}

/// Read the first `CONTENT_SCAN_LIMIT` characters of a file for keyword scanning.
/// Mirrors `advisor._fs.read_head` (best-effort; errors → empty).
fn read_head(path: &str) -> Option<String> {
    match std::fs::read(path) {
        Ok(bytes) => {
            let s = String::from_utf8_lossy(&bytes);
            Some(s.chars().take(rank::CONTENT_SCAN_LIMIT).collect())
        }
        Err(_) => Some(String::new()),
    }
}

/// Recursively discover files under `target` whose filename matches any
/// comma-separated sub-pattern of `file_types`. Mirrors `_safe_rglob`: returns
/// absolute paths (target canonicalized, joined with the walked components),
/// symlinks not followed. Order is unspecified — `rank_files` sorts the result.
fn discover(target: &Path, file_types: &str) -> Result<Vec<String>, String> {
    advisor::fs::validate_file_types(file_types)
        .map_err(|e| format!("invalid --file-types pattern: {e}"))?;
    let pats: Vec<&str> = file_types
        .split(',')
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .collect();
    if pats.is_empty() {
        return Ok(Vec::new());
    }
    let root = target
        .canonicalize()
        .map_err(|e| format!("filesystem error scanning {}: {e}", target.display()))?;

    let mut seen: HashSet<String> = HashSet::new();
    let mut out: Vec<String> = Vec::new();
    let mut stack: Vec<PathBuf> = vec![root.clone()];
    while let Some(dir) = stack.pop() {
        let entries = match std::fs::read_dir(&dir) {
            Ok(e) => e,
            Err(e) if dir == root => {
                return Err(format!("filesystem error scanning {}: {e}", dir.display()));
            }
            Err(_) => continue, // skip unreadable subdir, keep scanning
        };
        for entry in entries.flatten() {
            let ft = match entry.file_type() {
                Ok(t) => t,
                Err(_) => continue,
            };
            if ft.is_symlink() {
                continue; // do not follow symlinks (loop/escape safety)
            }
            let path = entry.path();
            if ft.is_dir() {
                stack.push(path);
            } else if ft.is_file() {
                let name = entry.file_name();
                let name = name.to_string_lossy();
                let rel = path
                    .strip_prefix(&root)
                    .unwrap_or(&path)
                    .to_string_lossy()
                    .replace('\\', "/");
                if path_matches_file_types(&rel, &name, &pats) {
                    let s = path.to_string_lossy().to_string();
                    if seen.insert(s.clone()) {
                        out.push(s);
                    }
                }
            }
        }
    }
    Ok(out)
}

/// Filter git-scope output by `--file-types` (fnmatch on the filename) and
/// intersect with `target`. Mirrors the git branch of `_resolve_plan_files`.
fn filter_git_scope(target: &Path, files: Vec<String>, file_types: &str) -> Vec<String> {
    let pats: Vec<&str> = file_types
        .split(',')
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .collect();
    let apply_types = !file_types.is_empty() && file_types != "*";
    let target_resolved = target.canonicalize().ok();
    files
        .into_iter()
        .filter(|p| {
            if apply_types {
                let path_obj = Path::new(p);
                let name = path_obj
                    .file_name()
                    .map(|n| n.to_string_lossy().into_owned())
                    .unwrap_or_default();
                let rel = match (&target_resolved, path_obj.canonicalize()) {
                    (Some(tr), Ok(rp)) if rp.starts_with(tr) => rp
                        .strip_prefix(tr)
                        .map(|r| r.to_string_lossy().replace('\\', "/"))
                        .unwrap_or_else(|_| name.clone()),
                    _ => p.replace('\\', "/"),
                };
                if !path_matches_file_types(&rel, &name, &pats) {
                    return false;
                }
            }
            match (&target_resolved, Path::new(p).canonicalize()) {
                (Some(tr), Ok(rp)) => rp.starts_with(tr),
                _ => false,
            }
        })
        .collect()
}

/// Serialize a plan to match the Python CLI's `plan --json` payload (key order,
/// 2-space indent, `ensure_ascii=True`). Mirrors `_plan_to_dict` + `json.dumps`.
fn plan_json(
    target_abs: &Path,
    tasks: &[FocusTask],
    batches: Option<&[FocusBatch]>,
    estimate: Option<serde_json::Value>,
) -> String {
    use serde::Serialize;

    #[derive(Serialize)]
    struct PlanTask<'a> {
        file_path: &'a str,
        priority: u8,
    }
    #[derive(Serialize)]
    struct PlanBatch<'a> {
        batch_id: usize,
        complexity: &'a str,
        top_priority: u8,
        tasks: Vec<PlanTask<'a>>,
    }
    #[derive(Serialize)]
    struct Payload<'a> {
        schema_version: &'a str,
        target: String,
        task_count: usize,
        tasks: Vec<PlanTask<'a>>,
        #[serde(skip_serializing_if = "Option::is_none")]
        batches: Option<Vec<PlanBatch<'a>>>,
        #[serde(skip_serializing_if = "Option::is_none")]
        estimate: Option<serde_json::Value>,
    }

    let task_data: Vec<PlanTask> = tasks
        .iter()
        .map(|t| PlanTask {
            file_path: &t.file_path,
            priority: t.priority,
        })
        .collect();
    let batch_data = batches.map(|bs| {
        bs.iter()
            .map(|b| PlanBatch {
                batch_id: b.batch_id,
                complexity: b.complexity.as_str(),
                top_priority: b.top_priority(),
                tasks: b
                    .tasks
                    .iter()
                    .map(|t| PlanTask {
                        file_path: &t.file_path,
                        priority: t.priority,
                    })
                    .collect(),
            })
            .collect()
    });
    let payload = Payload {
        schema_version: "1.0",
        target: target_abs.to_string_lossy().to_string(),
        task_count: tasks.len(),
        tasks: task_data,
        batches: batch_data,
        estimate,
    };
    ensure_ascii(&serde_json::to_string_pretty(&payload).expect("plan payload serializes"))
}

// ── findings input + baseline ──────────────────────────────────────

/// Read findings input from `--from FILE` or stdin (capped). Returns
/// `Ok(None)` when stdin is a TTY and no file is given (mirrors the Python
/// helper's empty-on-TTY behavior). Mirrors `_load_findings_from_input`'s I/O.
fn read_findings_input(from_file: Option<&str>) -> Result<Option<String>, String> {
    match from_file {
        Some(path) => {
            let p = Path::new(path);
            if !p.is_file() {
                return Err(format!("findings input {path} is not a regular file"));
            }
            let bytes = std::fs::read(p).map_err(|e| e.to_string())?;
            if bytes.len() > STDIN_LIMIT {
                return Err(format!(
                    "findings input {path} exceeds {STDIN_LIMIT}-byte cap; refusing to load"
                ));
            }
            Ok(Some(String::from_utf8_lossy(&bytes).into_owned()))
        }
        None => {
            if std::io::stdin().is_terminal() {
                return Ok(None);
            }
            let mut buf = Vec::new();
            std::io::stdin()
                .take((STDIN_LIMIT + 1) as u64)
                .read_to_end(&mut buf)
                .map_err(|e| e.to_string())?;
            if buf.len() > STDIN_LIMIT {
                return Err(format!(
                    "findings input exceeds {STDIN_LIMIT}-byte cap; refusing to load"
                ));
            }
            Ok(Some(String::from_utf8_lossy(&buf).into_owned()))
        }
    }
}

/// Parse findings input text — JSON (array, or object with `findings` /
/// `findings_in_batch`) or markdown fallback. Mirrors `_load_findings_from_input`'s
/// parsing (severity canonicalized, `<incomplete>` sentinel filtered).
fn parse_findings_input(text: &str) -> Vec<Finding> {
    let stripped = text.trim().trim_start_matches('\u{FEFF}');
    if stripped.starts_with('{') || stripped.starts_with('[') {
        if let Ok(doc) = serde_json::from_str::<serde_json::Value>(stripped) {
            let raw: Vec<serde_json::Value> = if let Some(obj) = doc.as_object() {
                obj.get("findings_in_batch")
                    .or_else(|| obj.get("findings"))
                    .and_then(|v| v.as_array())
                    .cloned()
                    .unwrap_or_default()
            } else if let Some(arr) = doc.as_array() {
                arr.clone()
            } else {
                Vec::new()
            };
            let mut findings = Vec::new();
            for f in &raw {
                let Some(m) = f.as_object() else { continue };
                let get = |k: &str| m.get(k).and_then(|v| v.as_str()).unwrap_or("");
                let file_path = match m.get("file_path").and_then(|v| v.as_str()) {
                    Some(s) => s.to_string(),
                    None => continue, // missing required key
                };
                if file_path == INCOMPLETE_FILE_PATH {
                    continue;
                }
                let (description, severity) = match (m.get("description"), m.get("severity")) {
                    (Some(d), Some(s)) if d.as_str().is_some() && s.as_str().is_some() => (
                        d.as_str().unwrap().to_string(),
                        s.as_str().unwrap().to_string(),
                    ),
                    _ => continue, // missing required keys
                };
                let rule_id = m
                    .get("rule_id")
                    .and_then(|v| v.as_str())
                    .map(|s| s.trim())
                    .filter(|s| !s.is_empty())
                    .map(|s| s.to_string());
                findings.push(Finding {
                    file_path,
                    severity: Severity::canonical(&severity).as_str().to_string(),
                    description,
                    evidence: get("evidence").to_string(),
                    fix: get("fix").to_string(),
                    rule_id,
                    expected_vs_actual: get("expected_vs_actual").to_string(),
                });
            }
            return findings;
        }
    }
    // Markdown fallback (mirrors parse_findings_from_text(None)).
    parse_findings_from_text(text, None)
}

/// `advisor baseline create|diff` — mirrors `cmd_baseline`.
fn cmd_baseline(action: BaselineAction) -> ExitCode {
    match action {
        BaselineAction::Create {
            target,
            from_file,
            output,
            quiet,
        } => {
            let target = Path::new(&target);
            let out = output
                .map(PathBuf::from)
                .unwrap_or_else(|| target.join(".advisor").join("baseline.jsonl"));
            let text = match read_findings_input(from_file.as_deref()) {
                Ok(t) => t,
                Err(e) => {
                    eprintln!("✗ {e}");
                    return ExitCode::from(2);
                }
            };
            let findings = text
                .as_deref()
                .map(parse_findings_input)
                .unwrap_or_default();
            if findings.is_empty() && from_file.is_none() && std::io::stdin().is_terminal() {
                eprintln!("✗ baseline create: no findings on stdin and no --from FILE; refusing to overwrite baseline with zero findings");
                return ExitCode::from(2);
            }
            let entries = findings_to_entries(&findings);
            if let Err(e) = write_baseline(&out, &entries) {
                eprintln!("✗ {e}");
                return ExitCode::from(2);
            }
            if !quiet {
                let word = if entries.len() == 1 {
                    "finding"
                } else {
                    "findings"
                };
                outln!(
                    "✓ baseline saved: {} ({} {word})",
                    out.display(),
                    entries.len()
                );
            }
            ExitCode::SUCCESS
        }
        BaselineAction::Diff {
            target,
            from_file,
            baseline_path,
            json,
        } => {
            let target = Path::new(&target);
            let bpath = baseline_path
                .clone()
                .map(PathBuf::from)
                .unwrap_or_else(|| target.join(".advisor").join("baseline.jsonl"));
            if !bpath.exists() {
                if baseline_path.is_some() {
                    eprintln!("✗ --baseline path not found: {}", bpath.display());
                } else {
                    eprintln!(
                        "✗ no baseline at {}; run `advisor baseline create` first or pass --baseline PATH",
                        bpath.display()
                    );
                }
                return ExitCode::from(2);
            }
            let baseline = read_baseline(&bpath);
            let text = match read_findings_input(from_file.as_deref()) {
                Ok(t) => t,
                Err(e) => {
                    eprintln!("✗ {e}");
                    return ExitCode::from(2);
                }
            };
            let findings = text
                .as_deref()
                .map(parse_findings_input)
                .unwrap_or_default();
            let diff = diff_against_baseline(&findings, &baseline);
            if json {
                let payload = serde_json::json!({
                    "schema_version": "1.0",
                    "new": diff.new.iter().map(|f| serde_json::json!({
                        "file_path": f.file_path, "severity": f.severity, "description": f.description,
                    })).collect::<Vec<_>>(),
                    "persisting_count": diff.persisting.len(),
                    "fixed": diff.fixed.iter().map(|e| serde_json::json!({
                        "file_path": e.file_path, "rule_id": e.rule_id, "description": e.description,
                    })).collect::<Vec<_>>(),
                });
                outln!(
                    "{}",
                    ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
                );
                return ExitCode::SUCCESS;
            }
            let mut lines = vec![
                "## Baseline diff".to_string(),
                String::new(),
                format!("New findings: **{}**", diff.new.len()),
                format!("Persisting: {}", diff.persisting.len()),
                format!("Fixed (in baseline, not seen): {}", diff.fixed.len()),
                String::new(),
            ];
            if !diff.new.is_empty() {
                lines.push("### New".to_string());
                for f in &diff.new {
                    lines.push(format!(
                        "- [{}] `{}` — {}",
                        f.severity, f.file_path, f.description
                    ));
                }
            }
            // Python prints `rstrip + "\n"` then print() adds another newline.
            let body = format!("{}\n\n", lines.join("\n").trim_end());
            print!("{body}");
            ExitCode::SUCCESS
        }
    }
}

// ── audit ──────────────────────────────────────────────────────────

struct AuditArgs {
    run_id: String,
    target: String,
    transcript: String,
    json: bool,
    sarif: Option<String>,
    fail_on: String,
    format: String,
    baseline_path: Option<String>,
}

/// Exit code 4 if any finding meets/exceeds `threshold`. Mirrors
/// `_fail_on_findings` (UNKNOWN ranks 99 so only `--fail-on never`/below skips it).
fn fail_on_exit(threshold: &str, findings: &[Finding]) -> Option<u8> {
    if threshold.is_empty() || threshold == "never" {
        return None;
    }
    let gate = match threshold {
        "low" => 1,
        "medium" => 2,
        "high" => 3,
        "critical" => 4,
        _ => 99,
    };
    let rank = |sev: &str| match sev.to_uppercase().as_str() {
        "LOW" => 1,
        "MEDIUM" => 2,
        "HIGH" => 3,
        "CRITICAL" => 4,
        "UNKNOWN" => 99,
        _ => 99,
    };
    if findings.iter().any(|f| rank(&f.severity) >= gate) {
        Some(4)
    } else {
        None
    }
}

/// Write a SARIF doc for `findings`. Mirrors `_write_sarif` (returns Some(exit)
/// on error). Writes `json.dumps(indent=2) + "\n"`.
fn write_sarif(path: &Path, findings: &[Finding], target: &Path) -> Option<u8> {
    match advisor::sarif::findings_to_sarif(findings, advisor::resolve_version(), target, "advisor")
    {
        Ok(doc) => {
            let rendered = format!("{}\n", advisor::sarif::to_pretty_json(&doc));
            match advisor::fs::atomic_write_text(path, &rendered) {
                Ok(()) => None,
                Err(e) => {
                    eprintln!("✗ sarif: {e}");
                    Some(1)
                }
            }
        }
        Err(e) => {
            eprintln!("✗ sarif: {e}");
            Some(1)
        }
    }
}

/// `advisor audit RUN_ID [TARGET]` — mirrors `cmd_audit`.
fn cmd_audit(args: AuditArgs) -> ExitCode {
    let target = Path::new(&args.target);
    let cp = match advisor::checkpoint::load_checkpoint(target, &args.run_id) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("✗ {e}");
            return ExitCode::from(2);
        }
    };

    // Read transcript (stdin or file), tolerating non-UTF-8 (errors=replace).
    let transcript = if args.transcript == "-" {
        if std::io::stdin().is_terminal() {
            eprintln!("✗ audit: no transcript on stdin; pipe the Claude Code conversation in");
            return ExitCode::from(2);
        }
        let mut buf = Vec::new();
        if std::io::stdin()
            .take((STDIN_LIMIT + 1) as u64)
            .read_to_end(&mut buf)
            .is_err()
        {
            eprintln!("✗ audit: failed reading stdin");
            return ExitCode::from(2);
        }
        if buf.len() > STDIN_LIMIT {
            eprintln!("✗ audit transcript exceeds {STDIN_LIMIT}-byte cap; refusing to load");
            return ExitCode::from(2);
        }
        String::from_utf8_lossy(&buf).into_owned()
    } else {
        let p = Path::new(&args.transcript);
        let bytes = match std::fs::read(p) {
            Ok(b) => b,
            Err(e) => {
                eprintln!("✗ {e}");
                return ExitCode::from(2);
            }
        };
        if bytes.len() > STDIN_LIMIT {
            eprintln!(
                "✗ audit transcript {} exceeds {STDIN_LIMIT}-byte cap; refusing to load",
                p.display()
            );
            return ExitCode::from(2);
        }
        String::from_utf8_lossy(&bytes).into_owned()
    };

    let audit_cp = AuditCheckpoint {
        run_id: cp.run_id.clone(),
        max_fixes_per_runner: cp.max_fixes_per_runner,
        large_file_line_threshold: cp.large_file_line_threshold,
        large_file_max_fixes: cp.large_file_max_fixes,
        tasks: cp.tasks.clone(),
        batches: cp.batches.clone(),
    };
    let mut report = advisor::audit::audit_transcript(&transcript, &audit_cp);

    // Baseline filter (in-batch findings only).
    if let Some(bp) = &args.baseline_path {
        let bpath = Path::new(bp);
        if !bpath.exists() {
            eprintln!("✗ --baseline path not found: {bp}");
            return ExitCode::from(2);
        }
        let baseline = read_baseline(bpath);
        let (kept, _) = filter_against_baseline(&report.findings_in_batch, &baseline);
        report.findings_in_batch = kept;
    }

    // Always consult .advisor/suppressions.jsonl.
    let suppr_path = target.join(".advisor").join("suppressions.jsonl");
    if suppr_path.exists() {
        match advisor::suppressions::load_suppressions(&suppr_path) {
            Ok(entries) => {
                let (kept, _dropped) =
                    advisor::suppressions::apply_suppressions(&report.findings_in_batch, &entries);
                report.findings_in_batch = kept;
            }
            Err(e) => {
                eprintln!("✗ {e}");
                return ExitCode::from(2);
            }
        }
    }

    if let Some(sp) = &args.sarif {
        if let Some(code) = write_sarif(Path::new(sp), &report.findings_in_batch, target) {
            return ExitCode::from(code);
        }
    }

    let fail_rc = fail_on_exit(&args.fail_on, &report.findings_in_batch);
    let exit = |rc: Option<u8>| ExitCode::from(rc.unwrap_or(0));

    if args.format == "pr-comment" {
        outln!(
            "{}",
            advisor::pr_comment::format_pr_comment(&report.findings_in_batch)
        );
        return exit(fail_rc);
    }

    let as_json = args.json || args.format == "json";
    if as_json {
        // {schema_version, **audit_to_dict(report)} — schema_version first.
        let mut map = serde_json::Map::new();
        map.insert("schema_version".to_string(), serde_json::json!("1.0"));
        if let serde_json::Value::Object(d) = advisor::audit::audit_to_dict(&report) {
            for (k, v) in d {
                map.insert(k, v);
            }
        }
        let payload = serde_json::Value::Object(map);
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
        return exit(fail_rc);
    }

    // Pretty (color disabled in the Rust port — colorize_markdown is a no-op here).
    outln!("{}", advisor::audit::format_audit_report(&report));
    exit(fail_rc)
}

// ── changelog ────────────────────────────────────────────────────────────────

fn cmd_changelog(
    version: Option<String>,
    since: Option<String>,
    json: bool,
    quiet: bool,
) -> ExitCode {
    if let Some(ref v) = version {
        let notes = load_release_notes(v);
        if json {
            let payload = serde_json::json!({
                "schema_version": "1.0",
                "version": v,
                "body": notes,
            });
            outln!(
                "{}",
                ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
            );
            return if notes.is_some() {
                ExitCode::SUCCESS
            } else {
                ExitCode::FAILURE
            };
        }
        match notes {
            Some(body) => {
                outln!("## v{v}");
                outln!("{body}");
                ExitCode::SUCCESS
            }
            None => {
                eprintln!("no changelog section for {v}");
                ExitCode::FAILURE
            }
        }
    } else {
        let sections = load_changelog_sections(since.as_deref());
        if json {
            let arr: Vec<serde_json::Value> = sections
                .iter()
                .map(|(v, h, b)| serde_json::json!({ "version": v, "heading": h, "body": b }))
                .collect();
            let payload = serde_json::json!({
                "schema_version": "1.0",
                "since": since,
                "sections": arr,
            });
            outln!(
                "{}",
                ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
            );
            return if sections.is_empty() {
                ExitCode::FAILURE
            } else {
                ExitCode::SUCCESS
            };
        }
        if sections.is_empty() {
            let target = match &since {
                Some(s) => format!("newer than {s}"),
                None => "available".to_string(),
            };
            eprintln!("no changelog sections {target}");
            return ExitCode::FAILURE;
        }
        for (i, (v, _heading, body)) in sections.iter().enumerate() {
            if i > 0 {
                outln!();
            }
            outln!("## v{v}");
            outln!("{body}");
        }
        if !quiet {
            outln!();
            outln!("Next: advisor update");
        }
        ExitCode::SUCCESS
    }
}

// ── update ────────────────────────────────────────────────────────────────────

fn supports_color() -> bool {
    if std::env::var("NO_COLOR").is_ok() {
        return false;
    }
    if std::env::var("CLICOLOR_FORCE")
        .map(|v| v != "0")
        .unwrap_or(false)
    {
        return true;
    }
    if std::env::var("CLICOLOR").map(|v| v == "0").unwrap_or(false) {
        return false;
    }
    if let Ok(term) = std::env::var("TERM") {
        if term == "dumb" {
            return false;
        }
    }
    std::io::stdout().is_terminal()
}

fn paint(text: &str, ansi_code: &str) -> String {
    if supports_color() {
        format!("\x1b[{}m{}\x1b[0m", ansi_code, text)
    } else {
        text.to_string()
    }
}

fn glyph(char_str: &str, fallback: &str) -> String {
    if supports_color() {
        char_str.to_string()
    } else {
        fallback.to_string()
    }
}

fn ok(text: &str) -> String {
    paint(text, "32")
}

fn error_box(text: &str) -> String {
    let text_clean = advisor::style::strip_ansi(text);
    let mark = glyph("✗", "[x]");
    if supports_color() {
        format!("{} {}", paint(&mark, "31;1"), paint(&text_clean, "31"))
    } else {
        format!("{mark} {text_clean}")
    }
}

fn cta(action: &str, description: &str) -> String {
    let bullet = glyph("→", ">");
    if supports_color() {
        let lead = paint(&bullet, "36;1");
        let act = paint(action, "1");
        if description.is_empty() {
            format!("  {lead} {act}")
        } else {
            let desc = paint(description, "2");
            format!("  {lead} {act}  {desc}")
        }
    } else {
        let sep = if description.is_empty() { "" } else { "  " };
        format!("  {bullet} {action}{sep}{description}")
    }
}

fn banner(text: &str, width: usize) -> String {
    let text_clean = advisor::style::strip_ansi(text);
    if !supports_color() {
        return format!("== {text_clean} ==");
    }
    let text_w = text_clean.chars().count();
    let effective_width = std::cmp::max(width, text_w + 4);
    let line = "━".repeat(effective_width);
    let inner = effective_width - 4;
    let left = (inner - text_w) / 2;
    let right = inner - text_w - left;
    let centered = format!("{}{}{}", " ".repeat(left), text_clean, " ".repeat(right));

    let center_padded = format!("  {}  ", paint(&centered, "1"));
    format!(
        "{}{}{}\n{}{}{}\n{}{}{}",
        paint("┏", "36"),
        paint(&line, "36"),
        paint("┓", "36"),
        paint("┃", "36"),
        center_padded,
        paint("┃", "36"),
        paint("┗", "36"),
        paint(&line, "36"),
        paint("┛", "36")
    )
}

fn detect_install_method() -> Option<(String, Vec<String>)> {
    let exe = std::env::current_exe().ok()?;
    let exe_dir = exe.parent()?;

    let is_local_target = exe_dir.ends_with("debug") || exe_dir.ends_with("release");
    let has_cargo_toml = std::path::Path::new("Cargo.toml").exists();

    if is_local_target || has_cargo_toml {
        return Some((
            "cargo install --path .".to_string(),
            vec![
                "cargo".to_string(),
                "install".to_string(),
                "--path".to_string(),
                ".".to_string(),
            ],
        ));
    }

    let exe_path_str = exe.to_string_lossy().to_string();
    if exe_path_str.contains(".cargo") {
        return Some((
            "cargo install advisor-rs".to_string(),
            vec![
                "cargo".to_string(),
                "install".to_string(),
                "advisor-rs".to_string(),
            ],
        ));
    }

    Some((
        "cargo install advisor-rs".to_string(),
        vec![
            "cargo".to_string(),
            "install".to_string(),
            "advisor-rs".to_string(),
        ],
    ))
}

fn cmd_update(yes: bool, quiet: bool, json: bool) -> ExitCode {
    let current = advisor::resolve_version();
    let method = detect_install_method();
    if method.is_none() {
        eprintln!(
            "{}",
            error_box(
                "Could not auto-detect install method. Upgrade manually:\n  cargo install advisor-rs"
            )
        );
        return ExitCode::from(1);
    }
    let (label, cmd) = method.unwrap();

    if !quiet && !json {
        outln!(
            "  {}",
            paint(
                &format!("checking PyPI for advisor-agent (current: v{current})..."),
                "2"
            )
        );
    }

    let latest = advisor::install::fetch_pypi_latest_version("advisor-agent", 5);
    let remote_changelog = advisor::install::fetch_remote_changelog(
        "https://raw.githubusercontent.com/vzwjustin/advisor/main/CHANGELOG.md",
        5,
    );

    let new_sections = if let Some(ref text) = remote_changelog {
        advisor::install::parse_changelog_sections(text, Some(current))
    } else {
        Vec::new()
    };

    let mut nothing_to_upgrade = false;
    if latest.is_none() && remote_changelog.is_none() {
        if !quiet && !json {
            outln!(
                "  {}",
                paint(
                    "(offline — preview unavailable, will run upgrade anyway)",
                    "2"
                )
            );
        }
    } else if new_sections.is_empty() {
        if let Some(ref lat) = latest {
            let lat_newer = advisor::install::is_semver_newer(lat, current);
            let cur_newer = advisor::install::is_semver_newer(current, lat);
            if lat_newer {
                if !quiet && !json {
                    outln!();
                    outln!("  {}", paint(&format!("(changelog preview unavailable — PyPI shows v{lat} newer than v{current})"), "2"));
                }
            } else {
                if !quiet && !json {
                    let check = glyph("✓", "[OK]");
                    let msg = if cur_newer {
                        format!("  {} ahead of published v{lat} (current: v{current} — dev or unreleased)", ok(&check))
                    } else {
                        format!(
                            "  {} already on the latest published version (v{lat})",
                            ok(&check)
                        )
                    };
                    outln!();
                    outln!("{msg}");
                }
                nothing_to_upgrade = true;
            }
        } else {
            if !quiet && !json {
                let check = glyph("✓", "[OK]");
                outln!();
                outln!(
                    "  {} already on the latest version (v{})",
                    ok(&check),
                    current
                );
            }
            nothing_to_upgrade = true;
        }
    } else if !quiet && !json {
        let target_ver = latest.clone().unwrap_or_else(|| new_sections[0].0.clone());
        let arrow = glyph("→", "->");
        let title = format!("v{current}  {arrow}  v{target_ver}");
        outln!();
        outln!("{}", banner(&title, 60));
        let n = new_sections.len();
        let summary = if n == 1 {
            "1 release ahead — here's what's new:".to_string()
        } else {
            format!("{n} releases ahead — here's what's new:")
        };
        outln!("  {}", paint(&summary, "2"));
        for (version, _heading, body) in &new_sections {
            outln!();
            outln!("{}", banner(&format!("v{version}"), 50));
            outln!("{body}");
        }
    }

    if nothing_to_upgrade {
        if json {
            let payload = serde_json::json!({
                "schema_version": "1.0",
                "current_version": current,
                "latest_version": latest.unwrap_or(current.to_string()),
                "upgraded": false,
            });
            outln!(
                "{}",
                ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
            );
        }
        return ExitCode::SUCCESS;
    }

    if json {
        let payload = serde_json::json!({
            "schema_version": "1.0",
            "current_version": current,
            "latest_version": latest.clone().unwrap_or(current.to_string()),
            "note": "advisor update with --json is not fully supported; run without --json to confirm interactive upgrade",
        });
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
        return ExitCode::FAILURE;
    }

    if !yes && std::io::stdin().is_terminal() {
        print!("\n  Proceed with `{label}` upgrade? [Y/n] ");
        let mut input = String::new();
        let _ = std::io::stdout().flush();
        if std::io::stdin().read_line(&mut input).is_err() {
            outln!("\n  aborted");
            return ExitCode::from(130);
        }
        let ans = input.trim().to_lowercase();
        if !ans.is_empty() && ans != "y" && ans != "yes" {
            outln!("  aborted");
            return ExitCode::SUCCESS;
        }
    }

    if !quiet {
        outln!();
        outln!("{}", cta(&label, &cmd.join(" ")));
    }

    let mut upgrade_cmd = std::process::Command::new(&cmd[0]);
    upgrade_cmd.args(&cmd[1..]);
    match upgrade_cmd.status() {
        Ok(status) => {
            if !status.success() {
                eprintln!("{}", error_box("upgrade failed"));
                return ExitCode::from(status.code().unwrap_or(1) as u8);
            }
        }
        Err(e) => {
            eprintln!("{}", error_box(&format!("upgrade failed: {e}")));
            return ExitCode::from(1);
        }
    }

    advisor::install::invalidate_update_check_cache(None);

    let mut final_latest = latest;
    if final_latest.is_none() && !quiet {
        final_latest = Some(advisor::resolve_version().to_string());
    }

    if !quiet {
        if let Some(ref lat) = final_latest {
            if lat != current {
                outln!();
                outln!("{}", banner(&format!("Updated: v{current} → v{lat}"), 60));
            }
        }
    }

    let mut install_cmd = std::process::Command::new(
        std::env::current_exe().unwrap_or_else(|_| std::path::PathBuf::from("advisor")),
    );
    install_cmd.arg("install");
    if quiet {
        install_cmd.arg("--quiet");
    }
    match install_cmd.output() {
        Ok(output) => {
            if !output.status.success() {
                let stdout = String::from_utf8_lossy(&output.stdout);
                let stderr = String::from_utf8_lossy(&output.stderr);
                eprintln!(
                    "{}",
                    error_box(&format!(
                        "post-upgrade install failed\nstdout:\n{}\nstderr:\n{}",
                        stdout.trim(),
                        stderr.trim()
                    ))
                );
            }
            ExitCode::from(output.status.code().unwrap_or(0) as u8)
        }
        Err(e) => {
            eprintln!(
                "{}",
                error_box(&format!("post-upgrade install re-exec failed: {e}"))
            );
            ExitCode::from(1)
        }
    }
}

// ── ui ────────────────────────────────────────────────────────────────────────

fn cmd_ui(target: &str, port: u16, host: &str, json: bool, quiet: bool) -> ExitCode {
    let target_path = std::path::Path::new(target);
    if !target_path.exists() {
        eprintln!("target not found: {target}");
        return ExitCode::from(2);
    }
    if !target_path.is_dir() {
        eprintln!("target is not a directory: {target}");
        return ExitCode::from(2);
    }

    if json {
        if port == 0 {
            eprintln!("✗ --json cannot be combined with --port 0 because no server is bound");
            return ExitCode::from(2);
        }
        let display_host = if host.contains(':') {
            format!("[{host}]")
        } else {
            host.to_string()
        };
        let url = format!("http://{display_host}:{port}");
        let payload = serde_json::json!({
            "schema_version": "1.0",
            "url": url,
            "server_running": false,
        });
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
        return ExitCode::SUCCESS;
    }

    let target_canonical = match target_path.canonicalize() {
        Ok(p) => p,
        Err(e) => {
            eprintln!("✗ filesystem error scanning {}: {e}", target_path.display());
            return ExitCode::from(2);
        }
    };

    let config = advisor::config::default_team_config(advisor::config::TeamConfigInput::new(
        target_canonical.to_string_lossy().to_string(),
    ));

    let state = advisor::web::AppState {
        target: target_canonical,
        default_file_types: config.file_types,
        default_min_priority: config.min_priority as u8,
        default_max_runners: config.max_runners as usize,
        default_advisor_model: config.advisor_model,
        default_runner_model: config.runner_model,
    };

    match advisor::web::run_server(state, host, port, false, quiet) {
        Ok(_) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("✗ {e}");
            ExitCode::from(1)
        }
    }
}

fn maybe_print_update_indicator() {
    let current = advisor::resolve_version();
    if let Some(update) = advisor::check_for_update_cached(current, 86400, None) {
        let warn_char = glyph("⚠", "[!]");
        let warn = paint(&warn_char, "33;1");
        let msg = paint(&format!("update available: v{update}"), "33");
        let cur = paint(
            &format!("(current: v{current} — run `advisor update`)"),
            "2",
        );
        outln_continue!();
        outln_continue!("  {warn} {msg} {cur}");
    }
}

// ── install / uninstall / status / doctor ────────────────────────────────────

const STRICT_NOOP_EXIT: u8 = 3;

fn format_component(label: &str, present: bool, current: bool) -> String {
    let mark = if present && current {
        "✓"
    } else if present {
        "~"
    } else {
        "✗"
    };
    let state = if present && current {
        "installed and current"
    } else if present {
        "installed but outdated"
    } else {
        "not installed"
    };
    format!("  {mark} {label}: {state}")
}

fn print_status(
    path: Option<String>,
    skill_path: Option<String>,
    json: bool,
    quiet: bool,
) -> (bool, ExitCode) {
    let nudge_path = path.as_deref().map(std::path::Path::new);
    let sp = skill_path.as_deref().map(std::path::Path::new);
    let update_sp = advisor::install::update_skill_path_for(sp);
    let s = get_status(nudge_path, sp, Some(&update_sp));
    let installed_v = get_installed_skill_version(sp);
    let version = advisor::resolve_version();

    let update_ok = s
        .update_skill
        .as_ref()
        .is_none_or(|u| u.present && u.current);
    let healthy = s.nudge.present
        && s.nudge.current
        && s.skill.present
        && s.skill.current
        && update_ok
        && s.harness_types.ok;

    if json {
        let mut payload = serde_json::json!({
            "schema_version": "1.0",
            "version": version,
            "installed_version": installed_v,
            "healthy": healthy,
            "opt_out": s.opt_out,
            "nudge": {
                "present": s.nudge.present,
                "current": s.nudge.current,
                "path": s.nudge.path.display().to_string(),
            },
            "skill": {
                "present": s.skill.present,
                "current": s.skill.current,
                "path": s.skill.path.display().to_string(),
            },
        });
        if let Some(u) = &s.update_skill {
            payload["update_skill"] = serde_json::json!({
                "present": u.present,
                "current": u.current,
                "path": u.path.display().to_string(),
            });
        }
        let expected: serde_json::Map<String, serde_json::Value> = s
            .harness_types
            .expected
            .iter()
            .map(|(role, typ)| (role.to_string(), serde_json::json!(typ)))
            .collect();
        payload["harness_agent_types"] = serde_json::json!({
            "ok": s.harness_types.ok,
            "issues": s.harness_types.issues,
            "expected": expected,
        });
        outln_continue!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
    } else if !quiet {
        outln_continue!("advisor {version}");
        if let Some(v) = &installed_v {
            outln_continue!("  installed skill version: {v}");
        }
        outln_continue!(
            "{}",
            format_component("nudge (CLAUDE.md)", s.nudge.present, s.nudge.current)
        );
        outln_continue!(
            "{}",
            format_component("skill (SKILL.md)", s.skill.present, s.skill.current)
        );
        if let Some(u) = &s.update_skill {
            outln_continue!("{}", format_component("update-skill", u.present, u.current));
        }
        if s.opt_out {
            outln_continue!("  (ADVISOR_NO_NUDGE is set)");
        }
        if s.harness_types.ok {
            outln_continue!("  ✓ harness agent types: built-in only");
        } else {
            for issue in &s.harness_types.issues {
                outln_continue!("  ~ harness: {issue}");
            }
        }
        maybe_print_update_indicator();
    }
    (healthy, ExitCode::SUCCESS)
}

fn cmd_install(
    path: Option<String>,
    skill_path: Option<String>,
    check: bool,
    json: bool,
    quiet: bool,
    strict: bool,
    skip_skill: bool,
) -> ExitCode {
    if check {
        let (healthy, _) = print_status(path, skill_path, json, quiet);
        if healthy && !quiet && !json {
            outln!();
            outln!("Next: /advisor <path>");
        }
        return if strict && !healthy {
            ExitCode::from(STRICT_NOOP_EXIT)
        } else {
            ExitCode::SUCCESS
        };
    }

    let nudge_path = path.as_deref().map(std::path::Path::new);
    let sp = skill_path.as_deref().map(std::path::Path::new);

    let nudge_result = install(nudge_path, NUDGE_BODY);
    let skill_result = if skip_skill {
        None
    } else {
        Some(install_skill(sp))
    };
    let update_result = if skip_skill {
        None
    } else {
        let up = advisor::install::update_skill_path_for(sp);
        Some(install_update_skill(Some(&up)))
    };
    let codex_result = if !skip_skill && codex_cli_available() {
        let codex_sp = advisor::install::default_codex_skills_root()
            .join("advisor")
            .join("SKILL.md");
        Some(advisor::install::install_skill(Some(&codex_sp)))
    } else {
        None
    };

    let emit =
        |label: &str, res: &Result<advisor::install::InstallResult, std::io::Error>| match res {
            Ok(r) => {
                if !quiet {
                    outln_continue!("  {} {label}", r.action);
                }
            }
            Err(e) => eprintln!("  error {label}: {e}"),
        };

    if !json {
        emit("nudge (CLAUDE.md)", &nudge_result);
        if let Some(ref r) = skill_result {
            emit("skill (SKILL.md)", r);
        }
        if let Some(ref r) = update_result {
            emit("update-skill", r);
        }
        if let Some(ref r) = codex_result {
            emit("codex-skill", r);
        }
        if !quiet {
            outln!();
            outln!("Next: /advisor <path>");
        }
    } else {
        let to_json = |res: &Result<advisor::install::InstallResult, std::io::Error>| match res {
            Ok(r) => serde_json::json!({
                "action": r.action.as_str(),
                "path": r.path.display().to_string(),
            }),
            Err(e) => serde_json::json!({ "error": e.to_string() }),
        };
        let mut payload = serde_json::json!({
            "schema_version": "1.0",
            "nudge": to_json(&nudge_result),
        });
        if let Some(ref r) = skill_result {
            payload["skill"] = to_json(r);
        }
        if let Some(ref r) = update_result {
            payload["update_skill"] = to_json(r);
        }
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
    }

    let failed = nudge_result.is_err()
        || skill_result.as_ref().is_some_and(Result::is_err)
        || update_result.as_ref().is_some_and(Result::is_err)
        || codex_result.as_ref().is_some_and(Result::is_err);
    let is_unchanged = |res: &Result<advisor::install::InstallResult, std::io::Error>| {
        res.as_ref()
            .map(|r| r.action == InstallAction::Unchanged)
            .unwrap_or(false)
    };
    let strict_noop = strict
        && is_unchanged(&nudge_result)
        && skill_result.as_ref().is_none_or(is_unchanged)
        && update_result.as_ref().is_none_or(is_unchanged)
        && codex_result.as_ref().is_none_or(is_unchanged);
    if failed {
        ExitCode::FAILURE
    } else if strict_noop {
        ExitCode::from(STRICT_NOOP_EXIT)
    } else {
        ExitCode::SUCCESS
    }
}

fn cmd_uninstall(
    path: Option<String>,
    skill_path: Option<String>,
    json: bool,
    quiet: bool,
) -> ExitCode {
    let nudge_path = path.as_deref().map(std::path::Path::new);
    let sp = skill_path.as_deref().map(std::path::Path::new);
    let up = advisor::install::update_skill_path_for(sp);

    let nudge_result = uninstall_nudge(nudge_path);
    let skill_result = uninstall_skill(sp);
    let update_result = uninstall_update_skill(Some(&up));
    let failed = nudge_result.is_err() || skill_result.is_err() || update_result.is_err();

    if json {
        let to_json = |res: &Result<advisor::install::InstallResult, std::io::Error>| match res {
            Ok(r) => serde_json::json!({
                "action": r.action.as_str(),
                "path": r.path.display().to_string(),
            }),
            Err(e) => serde_json::json!({ "error": e.to_string() }),
        };
        let payload = serde_json::json!({
            "schema_version": "1.0",
            "nudge": to_json(&nudge_result),
            "skill": to_json(&skill_result),
            "update_skill": to_json(&update_result),
        });
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
        return if failed {
            ExitCode::FAILURE
        } else {
            ExitCode::SUCCESS
        };
    }

    let emit =
        |label: &str, res: &Result<advisor::install::InstallResult, std::io::Error>| match res {
            Ok(r) => {
                if !quiet && r.action != InstallAction::Absent {
                    outln_continue!("  {} {label}", r.action);
                }
            }
            Err(e) => eprintln!("  error {label}: {e}"),
        };
    emit("nudge (CLAUDE.md)", &nudge_result);
    emit("skill (SKILL.md)", &skill_result);
    emit("update-skill", &update_result);
    if failed {
        ExitCode::FAILURE
    } else {
        ExitCode::SUCCESS
    }
}

fn cmd_status(
    path: Option<String>,
    skill_path: Option<String>,
    json: bool,
    quiet: bool,
    strict: bool,
) -> ExitCode {
    let (healthy, _) = print_status(path, skill_path, json, quiet);
    if healthy && !quiet && !json {
        outln!();
        outln!("Next: /advisor <path>");
    }
    if strict && !healthy {
        ExitCode::from(STRICT_NOOP_EXIT)
    } else {
        ExitCode::SUCCESS
    }
}

fn cmd_doctor(
    path: Option<String>,
    skill_path: Option<String>,
    json: bool,
    quiet: bool,
    strict: bool,
) -> ExitCode {
    let nudge_path = path.as_deref().map(std::path::Path::new);
    let sp = skill_path.as_deref().map(std::path::Path::new);
    let report = advisor::doctor::run_doctor(nudge_path, sp);

    if json {
        let mut payload = report.to_dict();
        payload["schema_version"] = serde_json::json!("1.0");
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
        return if strict && !report.healthy {
            ExitCode::from(STRICT_NOOP_EXIT)
        } else {
            ExitCode::SUCCESS
        };
    }

    if !quiet {
        outln!("{}", advisor::doctor::format_report(&report));
        outln!();
        if report.healthy {
            outln!("Next: advisor pipeline .");
        } else {
            outln!("Next: advisor install");
        }
        maybe_print_update_indicator();
    }

    if strict && !report.healthy {
        ExitCode::from(STRICT_NOOP_EXIT)
    } else {
        ExitCode::SUCCESS
    }
}

// ── pipeline ─────────────────────────────────────────────────────────────────

fn render_pipeline_text(cfg: &advisor::config::TeamConfig) -> String {
    advisor::orchestrate::render_pipeline(cfg)
}

fn cmd_pipeline(
    target: &str,
    json: bool,
    quiet: bool,
    advisor_model: Option<String>,
    runner_model: Option<String>,
    team: &str,
) -> ExitCode {
    let mut inp = TeamConfigInput::new(target);
    if let Some(m) = advisor_model {
        inp.advisor_model = m;
    }
    if let Some(m) = runner_model {
        inp.runner_model = m;
    }
    inp.team_name = team.to_string();
    let cfg = default_team_config(inp);
    let text = render_pipeline_text(&cfg);
    if json {
        let payload = serde_json::json!({
            "schema_version": "1.0",
            "text": text,
        });
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
        return ExitCode::SUCCESS;
    }
    outln!("{text}");
    if !quiet {
        outln!();
        outln!("Run the live pipeline: /advisor {target}");
    }
    ExitCode::SUCCESS
}

// ── protocol ─────────────────────────────────────────────────────────────────

const PROTOCOL_TEXT: &str = concat!(
    "# Advisor team lifecycle protocol\n\n",
    "Strict sequence for any Claude Code or Codex session using the /advisor skill.\n",
    "Deviating (e.g. shutting down with broadcast \"*\", forgetting TeamDelete,\n",
    "or spawning runners before the advisor) breaks the pipeline.\n\n",
    "1. Reset and create the team:\n",
    "   TeamDelete()\n",
    "   TeamCreate(name=\"review\")\n\n",
    "2. Spawn advisor FIRST (no runners yet):\n",
    "   Agent(name=\"advisor\", description=\"Investigate, rank, and dispatch runners\",\n",
    "         subagent_type=\"generalPurpose\", team_name=\"review\",\n",
    "         prompt=<build_advisor_prompt(config)>)\n\n",
    "3. Advisor does Glob+Grep discovery, ranks P1-P5, decides runner pool size,\n",
    "   THEN sends a dispatch plan with a per-runner prompt for each runner.\n\n",
    "4. Runners send reports to team-lead; team-lead relays each to the advisor\n",
    "   verbatim. Advisor verifies each output as it lands.\n\n",
    "5. Shut down teammates INDIVIDUALLY:\n",
    "     SendMessage({\"to\": \"advisor\",  \"message\": {\"type\": \"shutdown_request\"}})\n",
    "     SendMessage({\"to\": \"runner-1\", \"message\": {\"type\": \"shutdown_request\"}})\n\n",
    "6. TeamDelete()\n",
);

fn cmd_protocol(json: bool, quiet: bool) -> ExitCode {
    if json {
        let payload = serde_json::json!({
            "schema_version": "1.0",
            "text": PROTOCOL_TEXT,
        });
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
        return ExitCode::SUCCESS;
    }
    outln!("{PROTOCOL_TEXT}");
    if !quiet {
        outln!("Next: advisor pipeline .");
    }
    ExitCode::SUCCESS
}

// ── version ──────────────────────────────────────────────────────────────────

fn cmd_version(json: bool) -> ExitCode {
    let version = advisor::resolve_version();
    if json {
        let payload = serde_json::json!({
            "schema_version": "1.0",
            "advisor_version": version,
        });
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
        return ExitCode::SUCCESS;
    }
    outln!("advisor {version}");
    ExitCode::SUCCESS
}

// ── live ─────────────────────────────────────────────────────────────────────

fn cmd_live(action: LiveAction) -> ExitCode {
    match action {
        LiveAction::Record {
            target,
            kind,
            data,
            json,
            quiet,
        } => {
            let target = PathBuf::from(&target);
            let data_val: Option<serde_json::Value> = match data.as_deref() {
                None => None,
                Some("-") => {
                    let mut buf = Vec::new();
                    if let Err(e) = std::io::stdin()
                        .take((STDIN_LIMIT + 1) as u64)
                        .read_to_end(&mut buf)
                    {
                        eprintln!("error reading stdin: {e}");
                        return ExitCode::from(2);
                    }
                    if buf.len() > STDIN_LIMIT {
                        eprintln!("--data -: input exceeds {STDIN_LIMIT}-byte cap");
                        return ExitCode::from(2);
                    }
                    let buf = String::from_utf8_lossy(&buf).into_owned();
                    if buf.trim().is_empty() {
                        eprintln!("--data -: stdin was empty; expected a JSON object");
                        return ExitCode::from(2);
                    }
                    match serde_json::from_str(&buf) {
                        Ok(v) => Some(v),
                        Err(e) => {
                            eprintln!("invalid --data JSON: {e}");
                            return ExitCode::from(2);
                        }
                    }
                }
                Some(s) => match serde_json::from_str(s) {
                    Ok(v) => Some(v),
                    Err(e) => {
                        eprintln!("invalid --data JSON: {e}");
                        return ExitCode::from(2);
                    }
                },
            };
            if let Some(ref v) = data_val {
                if !v.is_object() {
                    eprintln!("--data must be a JSON object");
                    return ExitCode::from(2);
                }
            }
            match append_event(&target, &kind, data_val, None) {
                Ok((path, seq)) => {
                    if json {
                        let payload = serde_json::json!({
                            "schema_version": "1.0",
                            "path": path.display().to_string(),
                            "seq": seq,
                            "kind": kind,
                        });
                        outln!(
                            "{}",
                            ensure_ascii(
                                &serde_json::to_string_pretty(&payload).unwrap_or_default()
                            )
                        );
                    } else if !quiet {
                        eprintln!("recorded {kind} (seq {seq}) → {}", path.display());
                    }
                    ExitCode::SUCCESS
                }
                Err(e) => {
                    eprintln!("advisor live record failed: {e}");
                    ExitCode::FAILURE
                }
            }
        }
        LiveAction::Tail {
            target,
            limit,
            since,
            json,
        } => {
            let target = PathBuf::from(&target);
            let limit = limit.clamp(1, 1000);
            let events = load_recent_events(&target, since, limit);
            if json {
                let seq = latest_seq(&target);
                let payload = serde_json::json!({
                    "schema_version": "1.0",
                    "target": target.display().to_string(),
                    "count": events.len(),
                    "next_token": seq,
                    "events": events,
                });
                outln!(
                    "{}",
                    ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
                );
                return ExitCode::SUCCESS;
            }
            if events.is_empty() {
                eprintln!(
                    "no live events yet at {}",
                    live_events_path(&target).display()
                );
                return ExitCode::SUCCESS;
            }
            for ev in &events {
                let ts = ev.get("ts").and_then(|v| v.as_str()).unwrap_or("");
                let kind = ev.get("kind").and_then(|v| v.as_str()).unwrap_or("event");
                let data_payload = ev.get("data").cloned().unwrap_or(serde_json::json!({}));
                let data_str = if data_payload
                    .as_object()
                    .map(|o| !o.is_empty())
                    .unwrap_or(false)
                {
                    serde_json::to_string(&data_payload).unwrap_or_default()
                } else {
                    String::new()
                };
                let line = format!("{ts} {kind} {data_str}");
                outln!("{}", line.trim_end());
            }
            ExitCode::SUCCESS
        }
        LiveAction::Clear { target, quiet } => {
            let target = PathBuf::from(&target);
            let path = live_events_path(&target);
            if path.exists() {
                match std::fs::remove_file(&path) {
                    Ok(()) => {
                        if !quiet {
                            eprintln!("removed {}", path.display());
                        }
                    }
                    Err(e) => {
                        eprintln!("advisor live clear failed: {e}");
                        return ExitCode::FAILURE;
                    }
                }
            } else if !quiet {
                eprintln!("no live events file at {}", path.display());
            }
            ExitCode::SUCCESS
        }
    }
}

// ── prompt ───────────────────────────────────────────────────────────────────

struct PromptArgs {
    step: String,
    target: String,
    runner_id: usize,
    file_count: Option<usize>,
    max_runners: usize,
    json: bool,
    quiet: bool,
    advisor_model: Option<String>,
    runner_model: Option<String>,
    team: String,
    no_history: bool,
    preset: Option<String>,
    file_types: String,
    context: Option<String>,
}

fn cmd_prompt(args: PromptArgs) -> ExitCode {
    let target = PathBuf::from(&args.target);
    let mut inp = TeamConfigInput::new(&args.target);
    if let Some(m) = args.advisor_model {
        inp.advisor_model = m;
    }
    if let Some(m) = args.runner_model {
        inp.runner_model = m;
    }
    inp.team_name = args.team.clone();
    if let Some(p) = args.preset.as_deref() {
        inp.preset = Some(p.to_string());
    }
    inp.file_types = args.file_types.clone();
    inp.context = advisor::config::resolve_cli_context(args.context.as_deref());
    let cfg = default_team_config(inp);

    let text = match args.step.as_str() {
        "advisor" => {
            let history_block = if args.no_history {
                String::new()
            } else {
                let entries = advisor::history::load_recent(&target, 20);
                advisor::history::format_history_block(&entries)
            };
            build_advisor_prompt(&cfg, &history_block)
        }
        "runner" => {
            let runner_id = args.runner_id as i64;
            build_runner_pool_prompt(runner_id, &cfg)
        }
        "verify" => {
            let file_count = args.file_count.unwrap_or(args.max_runners) as i64;
            let runner_count = args.max_runners as i64;
            let findings_text =
                advisor::config::read_stdin_if_available("<paste findings here>", STDIN_LIMIT);
            build_verify_dispatch_prompt(&findings_text, file_count, runner_count)
        }
        other => {
            eprintln!("unknown step: {other:?}");
            return ExitCode::from(2);
        }
    };

    if args.json {
        let payload = serde_json::json!({
            "schema_version": "1.0",
            "step": args.step,
            "text": text,
        });
        outln!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
        return ExitCode::SUCCESS;
    }
    outln!("{text}");
    if !args.quiet && std::io::stdout().is_terminal() {
        outln!();
        outln!("Next: paste into Claude Code or Codex");
    }
    ExitCode::SUCCESS
}

// ── codex-plan-csv ───────────────────────────────────────────────────────────

struct CodexPlanCsvArgs {
    target: String,
    file_types: String,
    min_priority: u8,
    batch_size: usize,
    preset: Option<String>,
    out: Option<String>,
    quiet: bool,
    since: Option<String>,
    staged: bool,
    branch: Option<String>,
}

fn collect_files_by_glob_patterns(root: &Path, patterns: &[String]) -> Vec<String> {
    let mut result = Vec::new();
    let Ok(canon) = root.canonicalize() else {
        return result;
    };
    let exts: Vec<&str> = patterns
        .iter()
        .filter_map(|p| {
            // "*.py" → ".py", "*.rs" → ".rs"
            p.strip_prefix('*').map(|s| s.trim_start_matches('.'))
        })
        .collect();
    fn walk(dir: &Path, exts: &[&str], out: &mut Vec<String>) {
        let Ok(rd) = std::fs::read_dir(dir) else {
            return;
        };
        let mut entries: Vec<_> = rd.flatten().collect();
        entries.sort_by_key(|e| e.file_name());
        for entry in entries {
            let path = entry.path();
            if path.is_dir() {
                let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                if !name.starts_with('.') {
                    walk(&path, exts, out);
                }
            } else if exts.is_empty() {
                out.push(path.display().to_string());
            } else {
                let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
                if exts.iter().any(|e| name.ends_with(&format!(".{e}"))) {
                    out.push(path.display().to_string());
                }
            }
        }
    }
    walk(&canon, &exts, &mut result);
    result.dedup();
    result
}

fn cmd_codex_plan_csv(args: CodexPlanCsvArgs) -> ExitCode {
    let target = PathBuf::from(&args.target);
    let ignore_patterns = load_advisorignore(&args.target);
    let preset_extras: Option<Vec<(i64, Vec<&'static str>)>> =
        args.preset.as_deref().and_then(|p| {
            presets::get_preset(p)
                .ok()
                .map(|rp| rp.extra_keywords_by_tier.clone())
        });

    let paths = match advisor::git_scope::resolve_git_scope(
        &target,
        args.since.as_deref(),
        args.staged,
        args.branch.as_deref(),
    ) {
        Ok(Some(p)) => p,
        Ok(None) | Err(_) => {
            let file_types: Vec<String> = args
                .file_types
                .split(',')
                .map(|s| s.trim().to_string())
                .collect();
            collect_files_by_glob_patterns(&target, &file_types)
        }
    };

    // Convert &'static str extras to owned String for rank_files
    let owned_extras: Option<Vec<(i64, Vec<String>)>> = preset_extras.map(|v| {
        v.into_iter()
            .map(|(k, kws)| (k, kws.into_iter().map(|s| s.to_string()).collect()))
            .collect()
    });
    let ranked = rank_files_with_base(
        &paths,
        None,
        &ignore_patterns,
        owned_extras.as_deref(),
        None,
        None,
        30,
        &target,
    );
    let tasks = create_focus_tasks(&ranked, None, args.min_priority, DEFAULT_TASK_PROMPT);

    if tasks.is_empty() {
        if !args.quiet {
            eprintln!(
                "no files matched under {} at min_priority={}; nothing to dispatch",
                target.display(),
                args.min_priority
            );
        }
        return ExitCode::FAILURE;
    }

    let batch_size = if args.batch_size == 0 {
        5
    } else {
        args.batch_size
    };
    let batches = match create_focus_batches(&tasks, batch_size, "auto") {
        Ok(b) => b,
        Err(e) => {
            eprintln!("codex-plan-csv: {e}");
            return ExitCode::FAILURE;
        }
    };

    let out_path = if let Some(ref p) = args.out {
        PathBuf::from(p)
    } else {
        // tempfile: advisor-codex-plan.XXXXXX.csv
        use std::time::{SystemTime, UNIX_EPOCH};
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0);
        std::env::temp_dir().join(format!("advisor-codex-plan.{ts}.csv"))
    };

    if let Some(parent) = out_path.parent() {
        if !parent.as_os_str().is_empty() {
            if let Err(e) = std::fs::create_dir_all(parent) {
                eprintln!("cannot create output directory: {e}");
                return ExitCode::FAILURE;
            }
        }
    }

    let mut csv_rows: Vec<String> = Vec::new();
    // Header — QUOTE_ALL semantics: wrap every field in double-quotes, escape " as ""
    csv_rows.push(csv_quote_row(&[
        "runner_id",
        "batch_id",
        "file_count",
        "max_priority",
        "files",
        "prompt",
    ]));
    for (i, batch) in batches.iter().enumerate() {
        let runner_id = format!("runner-{}", i + 1);
        let file_lines: Vec<String> = batch
            .tasks
            .iter()
            .map(|t| format!("- `{}` (P{})", sanitize_inline(&t.file_path), t.priority))
            .collect();
        let file_line_refs: Vec<&str> = file_lines.iter().map(|s| s.as_str()).collect();
        let prompt = build_codex_runner_prompt(&runner_id, &file_line_refs);
        let files_joined = batch
            .tasks
            .iter()
            .map(|t| t.file_path.clone())
            .collect::<Vec<_>>()
            .join("|");
        csv_rows.push(csv_quote_row(&[
            &runner_id,
            &batch.batch_id.to_string(),
            &batch.tasks.len().to_string(),
            &batch.top_priority().to_string(),
            &files_joined,
            &prompt,
        ]));
    }

    let csv_content = csv_rows.join("\r\n") + "\r\n";
    match std::fs::write(&out_path, csv_content.as_bytes()) {
        Ok(()) => {}
        Err(e) => {
            eprintln!("failed to write CSV: {e}");
            return ExitCode::FAILURE;
        }
    }

    outln!("{}", out_path.display());
    ExitCode::SUCCESS
}

/// Wrap each field in double-quotes; escape interior `"` as `""`.
/// Mirrors Python's `csv.QUOTE_ALL` with `\r\n` row terminators.
fn csv_quote_row(fields: &[&str]) -> String {
    fields
        .iter()
        .map(|f| format!("\"{}\"", f.replace('"', "\"\"")))
        .collect::<Vec<_>>()
        .join(",")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn install_returns_failure_when_component_write_fails() {
        let tmp = tempfile::TempDir::new().unwrap();
        let not_dir = tmp.path().join("not-dir");
        std::fs::write(&not_dir, "").unwrap();

        let rc = cmd_install(
            Some(not_dir.join("CLAUDE.md").to_string_lossy().to_string()),
            None,
            false,
            false,
            true,
            false,
            true,
        );

        assert_eq!(rc, ExitCode::FAILURE);
    }

    #[test]
    fn install_strict_returns_noop_exit_when_nothing_changed() {
        let tmp = tempfile::TempDir::new().unwrap();
        let nudge_path = tmp.path().join("CLAUDE.md").to_string_lossy().to_string();

        let first = cmd_install(
            Some(nudge_path.clone()),
            None,
            false,
            false,
            true,
            false,
            true,
        );
        let second = cmd_install(Some(nudge_path), None, false, false, true, true, true);

        assert_eq!(first, ExitCode::SUCCESS);
        assert_eq!(second, ExitCode::from(STRICT_NOOP_EXIT));
    }

    #[test]
    fn prompt_accepts_context_flag() {
        let mut inp = TeamConfigInput::new(".");
        inp.context = advisor::config::resolve_cli_context(Some("find security bugs"));
        let cfg = default_team_config(inp);
        let text = build_advisor_prompt(&cfg, "");
        assert!(text.contains("find security bugs"));
        assert!(text.contains("user's goal"));
    }

    #[test]
    fn uninstall_json_returns_failure_when_component_remove_fails() {
        let tmp = tempfile::TempDir::new().unwrap();
        let not_dir = tmp.path().join("not-dir");
        std::fs::write(&not_dir, "").unwrap();

        let rc = cmd_uninstall(
            Some(not_dir.join("CLAUDE.md").to_string_lossy().to_string()),
            Some(not_dir.join("SKILL.md").to_string_lossy().to_string()),
            true,
            true,
        );

        assert_eq!(rc, ExitCode::FAILURE);
    }
}
