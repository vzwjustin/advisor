//! Port of constants and model-validation from `advisor/orchestrate/config.py`,
//! plus the [`TeamConfig`] dataclass and the [`default_team_config`] assembler
//! (env-var fallbacks, range clamping with stderr warnings, preset merge).

use once_cell::sync::Lazy;
use regex::Regex;
use serde::{Deserialize, Serialize};

use crate::presets::get_preset;

/// Bare-family model aliases accepted by Claude Code / Codex (`KNOWN_MODEL_SHORTCUTS`).
pub const KNOWN_MODEL_SHORTCUTS: [&str; 3] = ["opus", "sonnet", "haiku"];

/// Hard ceiling on the runner pool size (`POOL_SIZE_CEILING`).
pub const POOL_SIZE_CEILING: i64 = 20;

/// Default advisor (Opus) model id.
pub const DEFAULT_ADVISOR_MODEL: &str = "claude-opus-4-7";

/// Default runner (Sonnet) model id.
pub const DEFAULT_RUNNER_MODEL: &str = "claude-sonnet-4-6";

// Long-form model id matcher, identical to `_LONG_FORM_MODEL_RE`.
static LONG_FORM_MODEL_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^(?:Codex|claude)-(opus|sonnet|haiku)-\d+(?:[.-]\d+){0,3}(?:-\d{8})?$")
        .expect("model-id regex is a valid compile-time constant")
});

/// Return true if `name` looks like a valid Claude Code / Codex model string —
/// either a bare alias or a long-form `claude-`/`Codex-` family id. Mirrors
/// Python `is_known_model`.
pub fn is_known_model(name: &str) -> bool {
    if KNOWN_MODEL_SHORTCUTS.contains(&name) {
        return true;
    }
    LONG_FORM_MODEL_RE.is_match(name)
}

/// Configuration for the advisor review team. Mirrors the `TeamConfig`
/// dataclass; field order matches the Python declaration so serde output mirrors
/// `dataclasses.asdict`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TeamConfig {
    pub team_name: String,
    pub target_dir: String,
    pub file_types: String,
    pub max_runners: i64,
    pub min_priority: i64,
    pub context: String,
    pub advisor_model: String,
    pub runner_model: String,
    pub max_fixes_per_runner: i64,
    pub large_file_line_threshold: i64,
    pub large_file_max_fixes: i64,
    pub test_command: String,
    pub preset: Option<String>,
    pub runner_output_char_ceiling: i64,
    pub runner_file_read_ceiling: i64,
}

/// Inputs to [`default_team_config`], mirroring the Python keyword arguments and
/// their default sentinels. Construct with [`TeamConfigInput::new`] then override
/// fields as needed — env vars are consulted only for fields left at their
/// documented default sentinel (and `max_runners == None`).
#[derive(Debug, Clone)]
pub struct TeamConfigInput {
    pub target_dir: String,
    pub team_name: String,
    pub file_types: String,
    pub max_runners: Option<i64>,
    pub min_priority: i64,
    pub context: String,
    pub advisor_model: String,
    pub runner_model: String,
    pub max_fixes_per_runner: i64,
    pub large_file_line_threshold: i64,
    pub large_file_max_fixes: i64,
    pub test_command: String,
    pub warn_unknown_model: bool,
    pub preset: Option<String>,
    pub runner_output_char_ceiling: i64,
    pub runner_file_read_ceiling: i64,
}

impl TeamConfigInput {
    /// Default inputs (matching the Python signature defaults) for `target_dir`.
    pub fn new(target_dir: impl Into<String>) -> Self {
        TeamConfigInput {
            target_dir: target_dir.into(),
            team_name: "review".to_string(),
            file_types: "*.py".to_string(),
            max_runners: None,
            min_priority: 3,
            context: String::new(),
            advisor_model: DEFAULT_ADVISOR_MODEL.to_string(),
            runner_model: DEFAULT_RUNNER_MODEL.to_string(),
            max_fixes_per_runner: 5,
            large_file_line_threshold: 800,
            large_file_max_fixes: 3,
            test_command: String::new(),
            warn_unknown_model: true,
            preset: None,
            runner_output_char_ceiling: 80_000,
            runner_file_read_ceiling: 20,
        }
    }
}

/// Return the env var if set and non-empty, else `default`. Mirrors `_env_or`.
fn env_or(env_key: &str, default: &str) -> String {
    match std::env::var(env_key) {
        Ok(v) if !v.is_empty() => v,
        _ => default.to_string(),
    }
}

/// Parse the env var as an int, falling back (with a stderr warning on a
/// non-empty invalid value). Mirrors `_env_int_or`.
fn env_int_or(env_key: &str, default: i64) -> i64 {
    let raw = match std::env::var(env_key) {
        Ok(v) => v,
        Err(_) => return default,
    };
    if raw.trim().is_empty() {
        return default;
    }
    match raw.trim().parse::<i64>() {
        Ok(n) => n,
        Err(_) => {
            warn(&format!(
                "{env_key}={raw:?} is not an integer; using default {default}"
            ));
            default
        }
    }
}

/// Emit an advisory warning to stderr (mirrors `_style.warning_box` to stderr;
/// the box decoration is omitted — the message text is what matters).
fn warn(msg: &str) {
    eprintln!("⚠ {msg}");
}

/// Create a default team configuration with env-var fallbacks, range clamping
/// (with stderr warnings), and optional preset merge. Mirrors
/// `default_team_config`.
pub fn default_team_config(input: TeamConfigInput) -> TeamConfig {
    let TeamConfigInput {
        target_dir,
        team_name,
        mut file_types,
        max_runners,
        mut min_priority,
        context,
        mut advisor_model,
        mut runner_model,
        mut max_fixes_per_runner,
        mut large_file_line_threshold,
        mut large_file_max_fixes,
        mut test_command,
        warn_unknown_model,
        preset,
        mut runner_output_char_ceiling,
        mut runner_file_read_ceiling,
    } = input;

    // Capture "left at default sentinel" BEFORE any env mutation.
    let file_types_is_default = file_types == "*.py";
    let min_priority_is_default = min_priority == 3;
    let test_command_is_default = test_command.is_empty();
    let runner_output_char_ceiling_is_default = runner_output_char_ceiling == 80_000;
    let runner_file_read_ceiling_is_default = runner_file_read_ceiling == 20;

    if advisor_model == DEFAULT_ADVISOR_MODEL {
        advisor_model = env_or("ADVISOR_MODEL", &advisor_model);
    }
    if runner_model == DEFAULT_RUNNER_MODEL {
        runner_model = env_or("ADVISOR_RUNNER_MODEL", &runner_model);
    }

    let mut max_runners = match max_runners {
        None => {
            let raw = env_int_or("ADVISOR_MAX_RUNNERS", 5);
            if raw < 1 {
                warn(&format!("ADVISOR_MAX_RUNNERS={raw} is < 1; using 5"));
                5
            } else {
                raw
            }
        }
        Some(m) if m < 1 => {
            warn(&format!("max_runners={m} is < 1; using 1"));
            1
        }
        Some(m) => m,
    };
    if max_runners > POOL_SIZE_CEILING {
        warn(&format!(
            "max_runners={max_runners} exceeds ceiling of {POOL_SIZE_CEILING}; using {POOL_SIZE_CEILING}"
        ));
        max_runners = POOL_SIZE_CEILING;
    }

    if file_types_is_default {
        file_types = env_or("ADVISOR_FILE_TYPES", &file_types);
    }
    if min_priority_is_default {
        min_priority = env_int_or("ADVISOR_MIN_PRIORITY", min_priority);
    }
    if !(1..=5).contains(&min_priority) {
        let clamped = min_priority.clamp(1, 5);
        warn(&format!(
            "min_priority={min_priority} outside P1–P5; using {clamped}"
        ));
        min_priority = clamped;
    }
    if test_command_is_default {
        test_command = env_or("ADVISOR_TEST_COMMAND", &test_command);
    }
    if runner_output_char_ceiling_is_default {
        runner_output_char_ceiling = env_int_or(
            "ADVISOR_RUNNER_OUTPUT_CHAR_CEILING",
            runner_output_char_ceiling,
        );
    }
    if runner_file_read_ceiling_is_default {
        runner_file_read_ceiling =
            env_int_or("ADVISOR_RUNNER_FILE_READ_CEILING", runner_file_read_ceiling);
    }

    // Preset merge — fills only fields the caller left at the documented default
    // sentinel (using the pre-env snapshots so env values aren't clobbered).
    if let Some(name) = &preset {
        if let Ok(pack) = get_preset(name) {
            if file_types_is_default && file_types == "*.py" {
                file_types = pack.file_types.to_string();
            }
            if min_priority_is_default && min_priority == 3 {
                min_priority = pack.min_priority;
            }
            if test_command_is_default && test_command.is_empty() {
                if let Some(tc) = pack.test_command {
                    test_command = tc.to_string();
                }
            }
            min_priority = min_priority.clamp(1, 5);
        }
    }

    if warn_unknown_model {
        for (label, model) in [
            ("advisor_model", &advisor_model),
            ("runner_model", &runner_model),
        ] {
            if !is_known_model(model) {
                warn(&format!(
                    "{label}={model:?} does not look like a known native agent model shortcut or long-form ID; the agent tool may reject it"
                ));
            }
        }
    }

    // Floor the runner-budget integers at 1.
    max_fixes_per_runner = max_fixes_per_runner.max(1);
    large_file_max_fixes = large_file_max_fixes.max(1);
    large_file_line_threshold = large_file_line_threshold.max(1);
    runner_output_char_ceiling = runner_output_char_ceiling.max(1);
    runner_file_read_ceiling = runner_file_read_ceiling.max(1);

    TeamConfig {
        team_name,
        target_dir,
        file_types,
        max_runners,
        min_priority,
        context,
        advisor_model,
        runner_model,
        max_fixes_per_runner,
        large_file_line_threshold,
        large_file_max_fixes,
        test_command,
        preset,
        runner_output_char_ceiling,
        runner_file_read_ceiling,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Reference values captured from the Python implementation.
    #[test]
    fn known_model_matrix() {
        assert!(is_known_model("opus"));
        assert!(is_known_model("claude-opus-4-7"));
        assert!(!is_known_model("opus-4-5"));
        assert!(is_known_model("claude-sonnet-4-6-20231015"));
        assert!(!is_known_model("gpt-4"));
        assert!(is_known_model("Codex-haiku-4-5"));
    }

    #[test]
    fn date_stamp_must_be_eight_digits() {
        // Bounded version segment must not swallow a bogus date stamp.
        assert!(!is_known_model("claude-opus-4-99999999-extra"));
    }

    #[test]
    fn default_team_config_matches_python() {
        // The Python golden was captured with ADVISOR_* env cleared; clear them
        // here too so the no-env path is exercised deterministically. These vars
        // are not read by any other test in this crate.
        for k in [
            "ADVISOR_MODEL",
            "ADVISOR_RUNNER_MODEL",
            "ADVISOR_MAX_RUNNERS",
            "ADVISOR_FILE_TYPES",
            "ADVISOR_MIN_PRIORITY",
            "ADVISOR_TEST_COMMAND",
            "ADVISOR_RUNNER_OUTPUT_CHAR_CEILING",
            "ADVISOR_RUNNER_FILE_READ_CEILING",
        ] {
            std::env::remove_var(k);
        }

        let golden: serde_json::Value =
            serde_json::from_str(include_str!("../tests/parity/config.json")).unwrap();

        let check = |name: &str, input: TeamConfigInput| {
            let cfg = default_team_config(input);
            let got = serde_json::to_value(&cfg).unwrap();
            assert_eq!(got, golden[name], "scenario={name}");
        };

        let base = |dir: &str| {
            let mut i = TeamConfigInput::new(dir);
            i.warn_unknown_model = false;
            i
        };

        check("minimal", base("/t"));

        let mut pw = base("/t");
        pw.preset = Some("python-web".to_string());
        check("preset_python_web", pw);

        let mut na = base("/t");
        na.preset = Some("node-api".to_string());
        check("preset_node_api", na);

        let mut ch = base("/t");
        ch.max_runners = Some(99);
        ch.min_priority = 9;
        ch.max_fixes_per_runner = 0;
        ch.large_file_max_fixes = 0;
        ch.large_file_line_threshold = 0;
        ch.runner_output_char_ceiling = 0;
        ch.runner_file_read_ceiling = 0;
        check("clamp_high", ch);

        let mut cl = base("/t");
        cl.max_runners = Some(0);
        cl.min_priority = 0;
        check("clamp_low", cl);

        let mut ex = base("/proj");
        ex.team_name = "rev".to_string();
        ex.file_types = "*.rs".to_string();
        ex.max_runners = Some(4);
        ex.min_priority = 4;
        ex.context = "auth flow".to_string();
        ex.advisor_model = "opus".to_string();
        ex.runner_model = "sonnet".to_string();
        ex.test_command = "cargo test".to_string();
        check("explicit", ex);

        let mut pe = base("/t");
        pe.preset = Some("python-web".to_string());
        pe.min_priority = 5;
        check("preset_explicit_min", pe);
    }
}
