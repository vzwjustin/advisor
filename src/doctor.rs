//! Port of `advisor/doctor.py` — extended diagnostic checks.

use std::path::{Path, PathBuf};

use crate::install::{
    codex_cli_available, default_codex_skills_root, get_installed_skill_version, get_status,
    update_skill_path_for, OPT_OUT_ENV,
};
use crate::version::resolve_version;

// ── known env vars ────────────────────────────────────────────────────────

const KNOWN_ENV_VARS: &[&str] = &[
    "ADVISOR_MODEL",
    "ADVISOR_RUNNER_MODEL",
    "ADVISOR_MAX_RUNNERS",
    "ADVISOR_FILE_TYPES",
    "ADVISOR_MIN_PRIORITY",
    "ADVISOR_TEST_COMMAND",
    "ADVISOR_QUIET",
    "ADVISOR_RUNNER_OUTPUT_CHAR_CEILING",
    "ADVISOR_RUNNER_FILE_READ_CEILING",
    "ADVISOR_EXPLORER_MODEL",
    "ADVISOR_MAX_EXPLORERS",
    "ADVISOR_EXPLORER_OUTPUT_CHAR_CEILING",
    "ADVISOR_EXPLORER_FILE_READ_CEILING",
    OPT_OUT_ENV,
];

/// Values for these keys are safe to surface verbatim; others are redacted.
const KNOWN_SAFE_ENV_VARS: &[&str] = &[
    "ADVISOR_MODEL",
    "ADVISOR_RUNNER_MODEL",
    "ADVISOR_MAX_RUNNERS",
    "ADVISOR_FILE_TYPES",
    "ADVISOR_MIN_PRIORITY",
    "ADVISOR_QUIET",
    "ADVISOR_RUNNER_OUTPUT_CHAR_CEILING",
    "ADVISOR_RUNNER_FILE_READ_CEILING",
    "ADVISOR_EXPLORER_MODEL",
    "ADVISOR_MAX_EXPLORERS",
    "ADVISOR_EXPLORER_OUTPUT_CHAR_CEILING",
    "ADVISOR_EXPLORER_FILE_READ_CEILING",
    OPT_OUT_ENV,
];

const REDACTED_VALUE: &str = "<set>";

// ── types ─────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum HealthLevel {
    Ok,
    Warn,
    Fail,
}

impl HealthLevel {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Warn => "warn",
            Self::Fail => "fail",
        }
    }
}

impl std::fmt::Display for HealthLevel {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug, Clone)]
pub struct Check {
    pub name: String,
    pub level: HealthLevel,
    pub message: String,
}

impl Check {
    fn ok(name: &str, msg: &str) -> Self {
        Self {
            name: name.to_string(),
            level: HealthLevel::Ok,
            message: msg.to_string(),
        }
    }
    fn warn(name: &str, msg: &str) -> Self {
        Self {
            name: name.to_string(),
            level: HealthLevel::Warn,
            message: msg.to_string(),
        }
    }
    fn fail(name: &str, msg: &str) -> Self {
        Self {
            name: name.to_string(),
            level: HealthLevel::Fail,
            message: msg.to_string(),
        }
    }

    pub fn to_json(&self) -> serde_json::Value {
        serde_json::json!({
            "name": self.name,
            "level": self.level.as_str(),
            "message": self.message,
        })
    }
}

#[derive(Debug)]
pub struct DoctorReport {
    pub healthy: bool,
    pub checks: Vec<Check>,
    pub env_overrides: std::collections::HashMap<String, String>,
    pub advisor_version: String,
    pub platform: String,
}

impl DoctorReport {
    pub fn to_dict(&self) -> serde_json::Value {
        let checks: Vec<serde_json::Value> = self.checks.iter().map(|c| c.to_json()).collect();
        let env: serde_json::Map<String, serde_json::Value> = self
            .env_overrides
            .iter()
            .map(|(k, v)| (k.clone(), serde_json::json!(v)))
            .collect();
        serde_json::json!({
            "healthy": self.healthy,
            "checks": checks,
            "env_overrides": env,
            "advisor_version": self.advisor_version,
            "platform": self.platform,
        })
    }
}

// ── individual checks ─────────────────────────────────────────────────────

fn check_git() -> Check {
    if which("git") {
        Check::ok("git", "git available on PATH")
    } else {
        Check::warn(
            "git",
            "git not on PATH; --since/--staged/--branch will be unavailable",
        )
    }
}

fn check_claude_cli() -> Check {
    if which("claude") {
        Check::ok("claude-cli", "`claude` CLI available on PATH")
    } else {
        Check::warn(
            "claude-cli",
            "`claude` CLI not on PATH; Claude Code pipeline runs will be unavailable \
             from this shell — install Claude Code from https://claude.ai/code",
        )
    }
}

fn check_codex_cli() -> Check {
    if codex_cli_available() {
        Check::ok("codex-cli", "`codex` CLI available on PATH")
    } else {
        Check::warn(
            "codex-cli",
            "`codex` CLI not on PATH — Codex variant unavailable \
             (Claude Code `/advisor` is unaffected)",
        )
    }
}

fn check_home_dir(check_name: &str, dir_path: &Path) -> Check {
    if dir_path.is_symlink() {
        match std::fs::canonicalize(dir_path) {
            Ok(resolved) => {
                let home = home_dir();
                let home_resolved = std::fs::canonicalize(&home).unwrap_or(home);
                if !resolved.starts_with(&home_resolved) {
                    return Check::warn(
                        check_name,
                        &format!(
                            "{} resolves to {} (outside $HOME); \
                             advisor install refuses to write outside $HOME",
                            dir_path.display(),
                            resolved.display()
                        ),
                    );
                }
            }
            Err(e) => {
                return Check::warn(
                    check_name,
                    &format!(
                        "{} is a symlink that could not be resolved: {e}",
                        dir_path.display()
                    ),
                );
            }
        }
        if !dir_path.exists() {
            let target = std::fs::read_link(dir_path)
                .map(|p| p.display().to_string())
                .unwrap_or_else(|e| format!("<unreadable: {e}>"));
            return Check::warn(
                check_name,
                &format!(
                    "{} is a broken symlink (target: {target})",
                    dir_path.display()
                ),
            );
        }
    }
    if !dir_path.exists() {
        return Check::warn(
            check_name,
            &format!(
                "{} does not exist (will be created on first `advisor install`)",
                dir_path.display()
            ),
        );
    }
    if !dir_path.is_dir() {
        return Check::fail(
            check_name,
            &format!("{} exists but is not a directory", dir_path.display()),
        );
    }
    Check::ok(
        check_name,
        &format!("{} is a regular directory", dir_path.display()),
    )
}

fn check_claude_home() -> Check {
    check_home_dir("claude-home", &home_dir().join(".claude"))
}

fn check_codex_home() -> Check {
    check_home_dir("codex-home", &home_dir().join(".codex"))
}

fn check_codex_skill_install() -> Option<Check> {
    if !codex_cli_available() {
        return None;
    }
    let skill_path = default_codex_skills_root().join("advisor").join("SKILL.md");
    if !skill_path.exists() {
        Some(Check::warn(
            "install-codex-skill",
            &format!(
                "codex skill not installed at {} (run: advisor install)",
                skill_path.display()
            ),
        ))
    } else {
        Some(Check::ok(
            "install-codex-skill",
            &format!("codex skill installed at {}", skill_path.display()),
        ))
    }
}

fn check_harness_agent_types(status: &crate::install::Status) -> Vec<Check> {
    if status.harness_types.ok {
        vec![Check::ok(
            "harness-agent-types",
            "skill uses built-in subagent types (generalPurpose, explore)",
        )]
    } else {
        status
            .harness_types
            .issues
            .iter()
            .map(|issue| Check::warn("harness-agent-types", issue))
            .collect()
    }
}

fn check_install(
    status: &crate::install::Status,
    installed_version: Option<&str>,
    current_version: &str,
) -> Vec<Check> {
    let mut checks = Vec::new();
    let mut components: Vec<&crate::install::ComponentStatus> = vec![&status.nudge, &status.skill];
    if let Some(u) = &status.update_skill {
        components.push(u);
    }
    for component in components {
        let name = &component.name;
        if !component.present {
            checks.push(Check::warn(
                &format!("install-{name}"),
                &format!("{name} not installed (run: advisor install)"),
            ));
        } else if !component.current {
            if name == "skill" {
                if let Some(iv) = installed_version {
                    if iv != current_version {
                        checks.push(Check::warn(
                            &format!("install-{name}"),
                            &format!(
                                "{name} is outdated (installed: {iv}, available: {current_version}) \
                                 — run: advisor install"
                            ),
                        ));
                        continue;
                    }
                }
            }
            checks.push(Check::warn(
                &format!("install-{name}"),
                &format!("{name} is outdated (run: advisor install)"),
            ));
        } else {
            checks.push(Check::ok(
                &format!("install-{name}"),
                &format!("{name} installed and current"),
            ));
        }
    }
    if status.opt_out {
        checks.push(Check::warn(
            "opt-out",
            &format!("auto-install disabled via {OPT_OUT_ENV}"),
        ));
    }
    checks
}

fn collect_env_overrides() -> std::collections::HashMap<String, String> {
    let mut overrides = std::collections::HashMap::new();
    for k in KNOWN_ENV_VARS {
        if let Ok(val) = std::env::var(k) {
            let display = if KNOWN_SAFE_ENV_VARS.contains(k) {
                val
            } else {
                REDACTED_VALUE.to_string()
            };
            overrides.insert(k.to_string(), display);
        }
    }
    overrides
}

// ── public entry point ────────────────────────────────────────────────────

pub fn run_doctor(nudge_path: Option<&Path>, skill_path: Option<&Path>) -> DoctorReport {
    let version = resolve_version();
    let update_sp = update_skill_path_for(skill_path);
    let status = get_status(nudge_path, skill_path, Some(&update_sp));
    let installed = get_installed_skill_version(skill_path);

    let mut checks = vec![
        check_git(),
        check_claude_cli(),
        check_codex_cli(),
        check_claude_home(),
    ];

    let codex_present = codex_cli_available();
    if codex_present {
        checks.push(check_codex_home());
    }
    checks.extend(check_install(&status, installed.as_deref(), version));
    checks.extend(check_harness_agent_types(&status));
    if codex_present {
        if let Some(c) = check_codex_skill_install() {
            checks.push(c);
        }
    }

    let healthy = !checks.iter().any(|c| c.level == HealthLevel::Fail);
    let platform = {
        let os = std::env::consts::OS;
        os.to_string()
    };

    DoctorReport {
        healthy,
        checks,
        env_overrides: collect_env_overrides(),
        advisor_version: version.to_string(),
        platform,
    }
}

pub fn format_report(report: &DoctorReport) -> String {
    let mut lines = vec![
        format!("advisor doctor — {}", report.advisor_version),
        format!("platform: {}", report.platform),
        String::new(),
    ];
    for check in &report.checks {
        let mark = match check.level {
            HealthLevel::Ok => "✓",
            HealthLevel::Warn => "~",
            HealthLevel::Fail => "✗",
        };
        lines.push(format!("  {mark} {:<20} {}", check.name, check.message));
    }
    if !report.env_overrides.is_empty() {
        lines.push(String::new());
        lines.push("  env overrides in effect:".to_string());
        let mut pairs: Vec<_> = report.env_overrides.iter().collect();
        pairs.sort_by_key(|(k, _)| k.as_str());
        for (k, v) in pairs {
            lines.push(format!("    {k}={v:?}"));
        }
    }
    lines.push(String::new());
    let footer = if report.healthy {
        "healthy"
    } else {
        "unhealthy — fix the ✗ items above"
    };
    lines.push(format!("  {footer}"));
    lines.join("\n")
}

// ── helpers ───────────────────────────────────────────────────────────────

fn which(cmd: &str) -> bool {
    std::process::Command::new("which")
        .arg(cmd)
        .output()
        .map(|o| o.status.success())
        .unwrap_or_else(|_| {
            // Windows fallback
            std::process::Command::new("where")
                .arg(cmd)
                .output()
                .map(|o| o.status.success())
                .unwrap_or(false)
        })
}

fn home_dir() -> PathBuf {
    std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."))
}
