//! Port of `advisor/install.py` — nudge + skill file install/uninstall/status.

use std::path::{Path, PathBuf};

use once_cell::sync::Lazy;
use regex::Regex;

use crate::skill_asset::{skill_md, skill_md_update};

// ── constants ──────────────────────────────────────────────────────────────

pub const OPT_OUT_ENV: &str = "ADVISOR_NO_NUDGE";
pub const START_MARKER: &str = "<!-- advisor:nudge:start -->";
pub const END_MARKER: &str = "<!-- advisor:nudge:end -->";

pub const SKILL_DIR_NAME: &str = "advisor";
pub const SKILL_FILE_NAME: &str = "SKILL.md";
pub const UPDATE_SKILL_DIR_NAME: &str = "advisor-update";
pub const CODEX_SKILLS_DIR_NAME: &str = ".agents";
pub const CODEX_SKILLS_SUBDIR: &str = "skills";

const CLAUDE_MD_MAX_BYTES: usize = 1_048_576;
const SKILL_MD_MAX_BYTES: usize = 262_144;

static BADGE_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"<!--\s*advisor:([^\s>]+)\s*-->").unwrap());
static PYPI_VERSION_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^[A-Za-z0-9._+!\-]{1,64}$").unwrap());
#[allow(dead_code)]
static BLOCK_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(&format!(
        "{}.*?{}",
        regex::escape(START_MARKER),
        regex::escape(END_MARKER)
    ))
    .unwrap()
});

pub static NUDGE_BODY: &str = include_str!("../advisor/nudge_body.txt");

// ── types ─────────────────────────────────────────────────────────────────

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum InstallAction {
    Installed,
    Updated,
    Unchanged,
    Removed,
    Absent,
    Skipped,
}

impl InstallAction {
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Installed => "installed",
            Self::Updated => "updated",
            Self::Unchanged => "unchanged",
            Self::Removed => "removed",
            Self::Absent => "absent",
            Self::Skipped => "skipped",
        }
    }
}

impl std::fmt::Display for InstallAction {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

#[derive(Debug)]
pub struct InstallResult {
    pub path: PathBuf,
    pub action: InstallAction,
    pub error: Option<String>,
}

#[derive(Debug)]
pub struct ComponentStatus {
    pub name: String,
    pub path: PathBuf,
    pub present: bool,
    pub current: bool,
}

#[derive(Debug)]
pub struct Status {
    pub nudge: ComponentStatus,
    pub skill: ComponentStatus,
    pub opt_out: bool,
    pub update_skill: Option<ComponentStatus>,
}

// ── badge / semver helpers ────────────────────────────────────────────────

pub fn parse_badge(text: &str) -> Option<String> {
    BADGE_RE
        .captures(text)
        .and_then(|c| c.get(1))
        .map(|m| m.as_str().to_string())
}

pub fn is_valid_pypi_version(v: &str) -> bool {
    PYPI_VERSION_RE.is_match(v)
}

/// Parse a semver-ish string to `(major, minor, patch)` ints. Ignores pre/dev/local.
fn semver_tuple(v: &str) -> Option<(u64, u64, u64)> {
    // strip epoch (e.g. "1!2.3.4")
    let v = v.splitn(2, '!').last().unwrap_or(v);
    // strip pre/dev/local
    let v = v
        .split(['-', '+'])
        .next()
        .unwrap_or(v)
        .split(|c: char| c.is_alphabetic())
        .next()
        .unwrap_or(v)
        .trim_end_matches('.');
    let parts: Vec<&str> = v.splitn(3, '.').collect();
    if parts.is_empty() {
        return None;
    }
    let major: u64 = parts.first().and_then(|s| s.parse().ok()).unwrap_or(0);
    let minor: u64 = parts.get(1).and_then(|s| s.parse().ok()).unwrap_or(0);
    let patch: u64 = parts.get(2).and_then(|s| s.parse().ok()).unwrap_or(0);
    Some((major, minor, patch))
}

fn semver_gt(a: (u64, u64, u64), b: (u64, u64, u64)) -> bool {
    a > b
}

pub fn is_semver_newer(installed: &str, bundled: &str) -> bool {
    match (semver_tuple(installed), semver_tuple(bundled)) {
        (Some(a), Some(b)) => semver_gt(a, b),
        _ => false,
    }
}

// ── file helpers ──────────────────────────────────────────────────────────

fn read_text_capped(path: &Path, max_bytes: usize) -> Result<String, std::io::Error> {
    let data = std::fs::read(path)?;
    let data = if data.len() > max_bytes {
        &data[..max_bytes]
    } else {
        &data
    };
    let s = String::from_utf8_lossy(data).into_owned();
    Ok(s.replace("\r\n", "\n").replace('\r', "\n"))
}

fn atomic_write(target: &Path, text: &str) -> Result<(), std::io::Error> {
    // Refuse to follow a symlink at the final path component.
    if target.is_symlink() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::Other,
            format!("refusing to write through symlink: {}", target.display()),
        ));
    }
    let dir = target.parent().unwrap_or(Path::new("."));
    let tmp = dir.join(format!(
        ".advisor_tmp_{}.tmp",
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::write(&tmp, text.as_bytes())?;
    std::fs::rename(&tmp, target)?;
    Ok(())
}

// ── nudge helpers ─────────────────────────────────────────────────────────

pub fn render_block(body: &str) -> String {
    format!("{}\n{}\n{}\n", START_MARKER, body.trim_end(), END_MARKER)
}

fn strip_all_blocks(text: &str) -> String {
    // Use a non-greedy dot-all regex to remove all sentinel blocks.
    // BLOCK_RE is DOTALL via (?s) flag — we rebuild with (?s) here.
    static BLOCK_DOTALL: Lazy<Regex> = Lazy::new(|| {
        Regex::new(&format!(
            "(?s){}.*?{}",
            regex::escape(START_MARKER),
            regex::escape(END_MARKER)
        ))
        .unwrap()
    });
    BLOCK_DOTALL.replace_all(text, "").into_owned()
}

pub fn apply_nudge(existing: &str, body: &str) -> (String, InstallAction) {
    let block = render_block(body);
    let has_block = existing.contains(START_MARKER) && existing.contains(END_MARKER);
    if has_block {
        let stripped = strip_all_blocks(existing).trim().to_string();
        let updated = if stripped.is_empty() {
            block.clone()
        } else {
            format!("{stripped}\n\n{block}").trim_start_matches('\n').to_string()
        };
        if updated.trim() == existing.trim() {
            return (existing.to_string(), InstallAction::Unchanged);
        }
        return (updated, InstallAction::Updated);
    }
    if existing.trim().is_empty() {
        return (block, InstallAction::Installed);
    }
    let sep = if existing.ends_with("\n\n") {
        ""
    } else if existing.ends_with('\n') {
        "\n"
    } else {
        "\n\n"
    };
    (format!("{existing}{sep}{block}"), InstallAction::Installed)
}

pub fn remove_nudge(existing: &str) -> (String, InstallAction) {
    if !existing.contains(START_MARKER) && !existing.contains(END_MARKER) {
        return (existing.to_string(), InstallAction::Absent);
    }
    let stripped = strip_all_blocks(existing).trim().to_string();
    let cleaned = if stripped.is_empty() {
        String::new()
    } else {
        format!("{stripped}\n")
    };
    (cleaned, InstallAction::Removed)
}

// ── path defaults ─────────────────────────────────────────────────────────

pub fn default_claude_md() -> PathBuf {
    dirs_home().join(".claude").join("CLAUDE.md")
}

pub fn default_skills_root() -> PathBuf {
    dirs_home().join(".claude").join("skills")
}

pub fn default_skill_path() -> PathBuf {
    default_skills_root()
        .join(SKILL_DIR_NAME)
        .join(SKILL_FILE_NAME)
}

pub fn default_update_skill_path() -> PathBuf {
    default_skills_root()
        .join(UPDATE_SKILL_DIR_NAME)
        .join(SKILL_FILE_NAME)
}

pub fn update_skill_path_for(skill_path: Option<&Path>) -> PathBuf {
    match skill_path {
        None => default_update_skill_path(),
        Some(p) => {
            if p.file_name().map(|n| n == SKILL_FILE_NAME).unwrap_or(false)
                && p.parent()
                    .and_then(|d| d.file_name())
                    .map(|n| n == SKILL_DIR_NAME)
                    .unwrap_or(false)
            {
                p.parent()
                    .unwrap()
                    .parent()
                    .unwrap_or(Path::new("."))
                    .join(UPDATE_SKILL_DIR_NAME)
                    .join(SKILL_FILE_NAME)
            } else {
                p.parent().unwrap_or(Path::new(".")).join(UPDATE_SKILL_DIR_NAME).join(SKILL_FILE_NAME)
            }
        }
    }
}

pub fn default_codex_skills_root() -> PathBuf {
    dirs_home()
        .join(CODEX_SKILLS_DIR_NAME)
        .join(CODEX_SKILLS_SUBDIR)
}

fn dirs_home() -> PathBuf {
    std::env::var("HOME")
        .or_else(|_| std::env::var("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."))
}

// ── install / uninstall ───────────────────────────────────────────────────

pub fn install(path: Option<&Path>, body: &str) -> Result<InstallResult, std::io::Error> {
    let target = match path {
        Some(p) => p.to_path_buf(),
        None => {
            let t = default_claude_md();
            if t.is_symlink() {
                return Err(std::io::Error::new(
                    std::io::ErrorKind::Other,
                    format!("refusing to install nudge through symlink: {}", t.display()),
                ));
            }
            t
        }
    };
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let current = match read_text_capped(&target, CLAUDE_MD_MAX_BYTES) {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(e) => return Err(e),
    };
    let (new_contents, action) = apply_nudge(&current, body);
    if action != InstallAction::Unchanged {
        atomic_write(&target, &new_contents)?;
    }
    Ok(InstallResult { path: target, action, error: None })
}

pub fn uninstall_nudge(path: Option<&Path>) -> Result<InstallResult, std::io::Error> {
    let target = path.map(|p| p.to_path_buf()).unwrap_or_else(default_claude_md);
    let current = match read_text_capped(&target, CLAUDE_MD_MAX_BYTES) {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Ok(InstallResult {
                path: target,
                action: InstallAction::Absent,
                error: None,
            });
        }
        Err(e) => return Err(e),
    };
    let (new_contents, action) = remove_nudge(&current);
    if action != InstallAction::Absent {
        atomic_write(&target, &new_contents)?;
    }
    Ok(InstallResult { path: target, action, error: None })
}

fn install_file(
    target: &Path,
    body: &str,
    component_name: &str,
) -> Result<InstallResult, std::io::Error> {
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent)?;
    }
    if target.exists() {
        let current = read_text_capped(target, SKILL_MD_MAX_BYTES).unwrap_or_default();
        if current.trim() == body.trim() {
            return Ok(InstallResult {
                path: target.to_path_buf(),
                action: InstallAction::Unchanged,
                error: None,
            });
        }
        let installed_v = parse_badge(&current);
        let bundled_v = parse_badge(body);
        if let (Some(iv), Some(bv)) = (installed_v.as_deref(), bundled_v.as_deref()) {
            if is_semver_newer(iv, bv) {
                eprintln!(
                    "warning: overwriting {component_name} v{iv} with bundled v{bv} (downgrade)"
                );
            }
        }
        atomic_write(target, body)?;
        return Ok(InstallResult {
            path: target.to_path_buf(),
            action: InstallAction::Updated,
            error: None,
        });
    }
    atomic_write(target, body)?;
    Ok(InstallResult {
        path: target.to_path_buf(),
        action: InstallAction::Installed,
        error: None,
    })
}

pub fn install_skill(path: Option<&Path>) -> Result<InstallResult, std::io::Error> {
    let target = path.map(|p| p.to_path_buf()).unwrap_or_else(default_skill_path);
    install_file(&target, &skill_md(), "SKILL.md")
}

pub fn install_update_skill(path: Option<&Path>) -> Result<InstallResult, std::io::Error> {
    let target = path
        .map(|p| p.to_path_buf())
        .unwrap_or_else(default_update_skill_path);
    install_file(&target, &skill_md_update(), "advisor-update SKILL.md")
}

pub fn uninstall_skill(path: Option<&Path>) -> Result<InstallResult, std::io::Error> {
    let target = path.map(|p| p.to_path_buf()).unwrap_or_else(default_skill_path);
    if !target.exists() {
        return Ok(InstallResult {
            path: target,
            action: InstallAction::Absent,
            error: None,
        });
    }
    std::fs::remove_file(&target)?;
    Ok(InstallResult { path: target, action: InstallAction::Removed, error: None })
}

pub fn uninstall_update_skill(path: Option<&Path>) -> Result<InstallResult, std::io::Error> {
    let target = path
        .map(|p| p.to_path_buf())
        .unwrap_or_else(default_update_skill_path);
    if !target.exists() {
        return Ok(InstallResult {
            path: target,
            action: InstallAction::Absent,
            error: None,
        });
    }
    std::fs::remove_file(&target)?;
    Ok(InstallResult { path: target, action: InstallAction::Removed, error: None })
}

// ── status ────────────────────────────────────────────────────────────────

pub fn get_installed_skill_version(path: Option<&Path>) -> Option<String> {
    let target = path.map(|p| p.to_path_buf()).unwrap_or_else(default_skill_path);
    let text = read_text_capped(&target, SKILL_MD_MAX_BYTES).ok()?;
    parse_badge(&text)
}

pub fn get_status(
    nudge_path: Option<&Path>,
    skill_path: Option<&Path>,
    update_skill_path: Option<&Path>,
) -> Status {
    let nudge_target = nudge_path
        .map(|p| p.to_path_buf())
        .unwrap_or_else(default_claude_md);
    let skill_target = skill_path
        .map(|p| p.to_path_buf())
        .unwrap_or_else(default_skill_path);
    let update_skill_target = update_skill_path
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| update_skill_path_for(skill_path));

    let expected_block = render_block(NUDGE_BODY);

    let (nudge_present, nudge_current) = if nudge_target.exists() {
        match read_text_capped(&nudge_target, CLAUDE_MD_MAX_BYTES) {
            Ok(text) => {
                let present = text.contains(START_MARKER) && text.contains(END_MARKER);
                let current = present && text.contains(expected_block.trim());
                (present, current)
            }
            Err(_) => (false, false),
        }
    } else {
        (false, false)
    };

    let bundled_skill = skill_md();
    let (skill_present, skill_current) = if skill_target.exists() {
        match read_text_capped(&skill_target, SKILL_MD_MAX_BYTES) {
            Ok(text) => {
                let present = true;
                let current = text.trim() == bundled_skill.trim();
                (present, current)
            }
            Err(_) => (true, false),
        }
    } else {
        (false, false)
    };

    let bundled_update = skill_md_update();
    let update_status = if update_skill_target.exists() {
        match read_text_capped(&update_skill_target, SKILL_MD_MAX_BYTES) {
            Ok(text) => Some(ComponentStatus {
                name: "update_skill".to_string(),
                path: update_skill_target,
                present: true,
                current: text.trim() == bundled_update.trim(),
            }),
            Err(_) => Some(ComponentStatus {
                name: "update_skill".to_string(),
                path: update_skill_target,
                present: true,
                current: false,
            }),
        }
    } else {
        None
    };

    let opt_out = std::env::var(OPT_OUT_ENV)
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false);

    Status {
        nudge: ComponentStatus {
            name: "nudge".to_string(),
            path: nudge_target,
            present: nudge_present,
            current: nudge_current,
        },
        skill: ComponentStatus {
            name: "skill".to_string(),
            path: skill_target,
            present: skill_present,
            current: skill_current,
        },
        opt_out,
        update_skill: update_status,
    }
}

// ── changelog ─────────────────────────────────────────────────────────────

const CHANGELOG_MAX_BYTES: usize = 1_048_576;

static CHANGELOG_PATHS: &[&str] = &["CHANGELOG.md"];

static SECTION_START_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?m)^## \[([^\]]+)\]([^\n]*)").unwrap());

fn read_changelog() -> Option<String> {
    // look relative to the executable, then CWD
    let exe_dir = std::env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(|d| d.to_path_buf()));
    let search_roots: Vec<PathBuf> = std::iter::once(std::env::current_dir().ok())
        .chain(exe_dir.map(|d| {
            // walk up to find CHANGELOG.md
            let mut dirs = Vec::new();
            let mut cur = d.as_path().to_path_buf();
            for _ in 0..4 {
                dirs.push(cur.clone());
                if let Some(p) = cur.parent() {
                    cur = p.to_path_buf();
                } else {
                    break;
                }
            }
            dirs.into_iter().map(Some)
        }).into_iter().flatten())
        .flatten()
        .collect();
    for root in &search_roots {
        for name in CHANGELOG_PATHS {
            let candidate = root.join(name);
            if let Ok(text) = read_text_capped(&candidate, CHANGELOG_MAX_BYTES) {
                return Some(text);
            }
        }
    }
    None
}

/// Parse a CHANGELOG body into `(version, heading_rest, body)` tuples newest-first.
/// Skips `[Unreleased]`. When `since` is provided, only sections strictly newer.
pub fn parse_changelog_sections(
    text: &str,
    since: Option<&str>,
) -> Vec<(String, String, String)> {
    // Collect (start_byte, version, heading_rest) for every ## [X] header.
    let mut headers: Vec<(usize, String, String)> = Vec::new();
    for cap in SECTION_START_RE.captures_iter(text) {
        let m = cap.get(0).unwrap();
        let version = cap.get(1).unwrap().as_str().trim().to_string();
        let rest = cap.get(2).unwrap().as_str().trim_end().to_string();
        headers.push((m.end(), version, rest));
    }

    let mut sections = Vec::new();
    for (i, (end_of_header, version, rest)) in headers.iter().enumerate() {
        if version.to_lowercase() == "unreleased" {
            continue;
        }
        if let Some(s) = since {
            let cur = semver_tuple(version);
            let floor = semver_tuple(s);
            if let (Some(c), Some(f)) = (cur, floor) {
                if c <= f {
                    continue;
                }
            }
        }
        // body is from end_of_header to the start of the next header (or end of text)
        let body_end = headers.get(i + 1).map(|(start, _, _)| {
            // find the ## back from the start of the match
            let before = &text[..*start];
            // the ## [line starts at the last newline before start
            before.rfind('\n').map(|p| p + 1).unwrap_or(0)
        }).unwrap_or(text.len());
        let body_start = if *end_of_header < text.len() && text.as_bytes()[*end_of_header] == b'\n' {
            end_of_header + 1
        } else {
            *end_of_header
        };
        let body = text.get(body_start..body_end).unwrap_or("").trim().to_string();
        sections.push((version.clone(), rest.clone(), body));
    }
    sections
}

pub fn load_changelog_sections(since: Option<&str>) -> Vec<(String, String, String)> {
    match read_changelog() {
        Some(text) => parse_changelog_sections(&text, since),
        None => Vec::new(),
    }
}

pub fn load_release_notes(version: &str) -> Option<String> {
    let text = read_changelog()?;
    let sections = parse_changelog_sections(&text, None);
    sections.into_iter().find(|(v, _, _)| v == version).map(|(_, _, body)| body)
}

pub fn should_auto_nudge() -> bool {
    !std::env::var(OPT_OUT_ENV)
        .map(|v| !v.trim().is_empty())
        .unwrap_or(false)
}

pub fn codex_cli_available() -> bool {
    std::process::Command::new("codex")
        .arg("--version")
        .output()
        .map(|o| o.status.success())
        .unwrap_or(false)
}

// ── parity tests ──────────────────────────────────────────────────────────

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_badge_basic() {
        assert_eq!(parse_badge("<!-- advisor:0.8.4 -->"), Some("0.8.4".into()));
        assert_eq!(parse_badge("no badge here"), None);
    }

    #[test]
    fn semver_newer() {
        assert!(is_semver_newer("1.0.0", "0.9.9"));
        assert!(!is_semver_newer("0.8.0", "0.8.4"));
        assert!(!is_semver_newer("0.8.4", "0.8.4"));
    }

    #[test]
    fn apply_nudge_roundtrip() {
        let (contents, action) = apply_nudge("", NUDGE_BODY);
        assert_eq!(action, InstallAction::Installed);
        assert!(contents.contains(START_MARKER));
        assert!(contents.contains(END_MARKER));

        let (contents2, action2) = apply_nudge(&contents, NUDGE_BODY);
        assert_eq!(action2, InstallAction::Unchanged);
        assert_eq!(contents2, contents);
    }

    #[test]
    fn remove_nudge_absent() {
        let (out, action) = remove_nudge("nothing here");
        assert_eq!(action, InstallAction::Absent);
        assert_eq!(out, "nothing here");
    }

    #[test]
    fn remove_nudge_present() {
        let (with_block, _) = apply_nudge("preamble\n", NUDGE_BODY);
        let (removed, action) = remove_nudge(&with_block);
        assert_eq!(action, InstallAction::Removed);
        assert!(!removed.contains(START_MARKER));
        assert!(removed.contains("preamble"));
    }

    #[test]
    fn install_parity_json() {
        use std::collections::HashMap;
        let raw = std::fs::read_to_string("tests/parity/install.json").unwrap();
        let v: HashMap<String, serde_json::Value> = serde_json::from_str(&raw).unwrap();
        assert_eq!(v["OPT_OUT_ENV"], OPT_OUT_ENV);
        assert_eq!(v["START_MARKER"], START_MARKER);
        assert_eq!(v["END_MARKER"], END_MARKER);

        // badge present
        let badge_text = format!("<!-- advisor:{} -->", v["badge_present"].as_str().unwrap());
        let got = parse_badge(&badge_text);
        assert_eq!(got.as_deref(), v["badge_present"].as_str());

        // badge absent
        assert_eq!(parse_badge("no badge"), None);

        // semver_gt true/false
        let gt_true = v["semver_gt_true"].as_bool().unwrap();
        let gt_false = v["semver_gt_false"].as_bool().unwrap();
        assert_eq!(
            semver_gt(
                semver_tuple("1.0.0").unwrap(),
                semver_tuple("0.9.9").unwrap()
            ),
            gt_true
        );
        assert_eq!(
            semver_gt(
                semver_tuple("0.8.0").unwrap(),
                semver_tuple("1.0.0").unwrap()
            ),
            gt_false
        );

        // newer_true / newer_false
        assert_eq!(is_semver_newer("1.0.0", "0.9.9"), v["newer_true"].as_bool().unwrap());
        assert_eq!(is_semver_newer("0.8.0", "1.0.0"), v["newer_false"].as_bool().unwrap());

        // valid / invalid version strings
        assert_eq!(
            is_valid_pypi_version("0.8.4"),
            v["valid_ver"].as_bool().unwrap()
        );
        assert_eq!(
            is_valid_pypi_version("\x1b[31m0.8.4\x1b[0m"),
            v["invalid_ver_escape"].as_bool().unwrap()
        );

        // render_block markers
        let block = render_block(NUDGE_BODY);
        assert_eq!(
            block.starts_with(START_MARKER),
            v["render_block_starts"].as_bool().unwrap()
        );
        assert!(block.contains(END_MARKER));

        // apply_nudge: install on empty
        let (contents, action) = apply_nudge("", NUDGE_BODY);
        assert_eq!(action.as_str(), v["apply_empty_action"].as_str().unwrap());
        assert_eq!(
            contents.contains(START_MARKER) && contents.contains(END_MARKER),
            v["apply_empty_has_markers"].as_bool().unwrap()
        );
    }
}
