//! `advisor` CLI binary (Rust port — in progress).
//!
//! Only the subcommands whose behavior has been ported and parity-verified are
//! wired up here. The full argparse surface (see RUST_PORT_PLAN.md §2) is being
//! migrated incrementally; until then the Python CLI remains the reference
//! implementation and ships alongside this binary.

use std::collections::HashSet;
use std::io::{IsTerminal, Read};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::{Parser, Subcommand};

use advisor::audit::AuditCheckpoint;
use advisor::baseline::{
    diff_against_baseline, filter_against_baseline, findings_to_entries, read_baseline,
    write_baseline,
};
use advisor::config::{default_team_config, TeamConfigInput};
use advisor::focus::{
    self, create_focus_batches, create_focus_tasks, FocusBatch, FocusTask, DEFAULT_TASK_PROMPT,
};
use advisor::jsonutil::ensure_ascii;
use advisor::models::Severity;
use advisor::presets;
use advisor::rank::{self, fnmatch_match, load_advisorignore, rank_files};
use advisor::verify::{parse_findings_from_text, INCOMPLETE_FILE_PATH};
use advisor::Finding;

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
        /// Ignore `.advisor/history.jsonl` (currently always-on in the Rust port).
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
        Command::Suppressions {
            target,
            list,
            expired,
            json,
        } => {
            let _ = list; // default-on flag, no behavior change
            cmd_suppressions(&target, expired, json)
        }
    }
}

/// `advisor suppressions` — list active/expired suppressions. Mirrors
/// `cmd_suppressions`.
fn cmd_suppressions(target: &str, expired_only: bool, json: bool) -> ExitCode {
    let path = Path::new(target)
        .join(".advisor")
        .join("suppressions.jsonl");
    if !path.exists() {
        println!("no suppressions file at {}", path.display());
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
        println!(
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
        println!("no {label} in {}", path.display());
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
        println!("{}", presets::presets_json(&packs));
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
/// for the core (non-git-scope, non-resume, non-checkpoint, non-estimate) path.
///
/// History-informed ranking is not yet wired (history.py is not ported), so the
/// Rust port behaves as `--no-history`; on a target with no `.advisor/history.jsonl`
/// this is identical to the Python default. See PORT_NOTES.md.
fn cmd_plan(args: PlanArgs) -> ExitCode {
    let _ = args.no_history; // history not yet ported; plan is always no-history

    // `--dump-pricing-template` short-circuits all discovery.
    if args.dump_pricing_template {
        println!("{}", dump_pricing_template());
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

    let paths = match discover(target, &cfg.file_types) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("✗ {e}");
            return ExitCode::from(2);
        }
    };

    let ignore = load_advisorignore(&args.target);
    let read = |p: &str| read_head(p);
    let ranked = rank_files(
        &paths,
        Some(&read),
        &ignore,
        preset_extras.as_deref(),
        None,
        None,
        90,
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
        println!(
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
            Err(_) => continue, // skip unreadable dir, keep scanning
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
                if pats.iter().any(|pat| fnmatch_match(&name, pat)) {
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
                println!(
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
                println!(
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
        _ => 0,
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
        println!(
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
        println!(
            "{}",
            ensure_ascii(&serde_json::to_string_pretty(&payload).unwrap_or_default())
        );
        return exit(fail_rc);
    }

    // Pretty (color disabled in the Rust port — colorize_markdown is a no-op here).
    println!("{}", advisor::audit::format_audit_report(&report));
    exit(fail_rc)
}
