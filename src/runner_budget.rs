use std::collections::HashSet;

use once_cell::sync::Lazy;
use regex::Regex;
use serde::{Deserialize, Serialize};

use crate::fs::normalize_path;

pub const DEFAULT_CHAR_CEILING: usize = 80_000;
pub const DEFAULT_FILE_READ_CEILING: usize = 20;
pub const SOFT_WARN_FRACTION: f64 = 0.60;
pub const ROTATE_FRACTION: f64 = 0.80;

pub const SCOPE_STAGES: &[&str] = &["reading", "hypothesizing", "confirming", "fixing", "done"];

static SCOPE_RE: Lazy<Regex> = Lazy::new(|| {
    // Mirrors Python: ^\s*SCOPE\s*:\s*(?P<file>\S[^\n]*?)\s+[·|\-]\s+(?P<stage>\w+)\s*$
    // Regex crate uses (?m) for multiline; no (?i) needed — \w matches ASCII word chars
    Regex::new(r"(?mi)^\s*SCOPE\s*:\s*(\S[^\n]*?)\s+[·|\-]\s+(\w+)\s*$").unwrap()
});

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ScopeAnchor {
    pub file_path: String,
    pub stage: String,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunnerBudget {
    pub runner_id: String,
    pub char_ceiling: usize,
    pub file_read_ceiling: usize,
    pub fix_ceiling: usize,
    pub output_chars: usize,
    pub files_read: Vec<String>,
    pub fixes_done: usize,
    pub soft_nudge_sent: bool,
    pub rotate_nudge_sent: bool,
    pub last_stage: Option<String>,
    pub last_file: Option<String>,
}

pub fn new_budget(
    runner_id: &str,
    char_ceiling: Option<usize>,
    file_read_ceiling: Option<usize>,
    fix_ceiling: Option<usize>,
) -> Result<RunnerBudget, String> {
    let cc = char_ceiling.unwrap_or(DEFAULT_CHAR_CEILING);
    let frc = file_read_ceiling.unwrap_or(DEFAULT_FILE_READ_CEILING);
    let fc = fix_ceiling.unwrap_or(5);
    if cc == 0 {
        return Err("char_ceiling must be > 0".to_string());
    }
    if frc == 0 {
        return Err("file_read_ceiling must be > 0".to_string());
    }
    // fix_ceiling == 0 is explore-only (valid); usize can't be negative
    Ok(RunnerBudget {
        runner_id: runner_id.to_string(),
        char_ceiling: cc,
        file_read_ceiling: frc,
        fix_ceiling: fc,
        output_chars: 0,
        files_read: vec![],
        fixes_done: 0,
        soft_nudge_sent: false,
        rotate_nudge_sent: false,
        last_stage: None,
        last_file: None,
    })
}

pub fn parse_scope_anchor(text: &str) -> Vec<ScopeAnchor> {
    SCOPE_RE
        .captures_iter(text)
        .map(|c| {
            let file_path = c
                .get(1)
                .map(|m| m.as_str().trim().trim_matches('`').to_string())
                .unwrap_or_default();
            let stage = c
                .get(2)
                .map(|m| m.as_str().trim().to_lowercase())
                .unwrap_or_default();
            ScopeAnchor { file_path, stage }
        })
        .collect()
}

pub fn update_budget(
    budget: &RunnerBudget,
    message_text: &str,
    fix_completed: bool,
    file_read: Option<&str>,
) -> RunnerBudget {
    let anchors = parse_scope_anchor(message_text);
    let mut new_files: Vec<String> = budget.files_read.clone();
    let mut last_anchor: Option<&ScopeAnchor> = None;

    for a in &anchors {
        if !a.file_path.is_empty() {
            let ap = normalize_path(&a.file_path);
            if !new_files.contains(&ap) {
                new_files.push(ap);
            }
        }
        last_anchor = Some(a);
    }

    if let Some(fr) = file_read {
        let fp = normalize_path(fr);
        if !new_files.contains(&fp) {
            new_files.push(fp);
        }
    }

    let last_anchor_path = last_anchor
        .filter(|a| !a.file_path.is_empty())
        .map(|a| normalize_path(&a.file_path));

    let last_stage = last_anchor
        .map(|a| a.stage.clone())
        .or_else(|| budget.last_stage.clone());

    let last_file = last_anchor_path.or_else(|| budget.last_file.clone());

    RunnerBudget {
        output_chars: budget.output_chars + message_text.chars().count(),
        files_read: new_files,
        fixes_done: budget.fixes_done + if fix_completed { 1 } else { 0 },
        last_stage,
        last_file,
        ..budget.clone()
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum BudgetStatus {
    Ok,
    SoftWarn,
    Rotate,
}

pub fn budget_status(budget: &RunnerBudget) -> BudgetStatus {
    let rotate_chars = (budget.char_ceiling as f64 * ROTATE_FRACTION) as usize;
    if budget.output_chars >= rotate_chars
        || budget.files_read.len() >= budget.file_read_ceiling
        || (budget.fix_ceiling > 0 && budget.fixes_done >= budget.fix_ceiling)
    {
        return BudgetStatus::Rotate;
    }
    let soft_chars = (budget.char_ceiling as f64 * SOFT_WARN_FRACTION) as usize;
    if budget.output_chars >= soft_chars {
        return BudgetStatus::SoftWarn;
    }
    BudgetStatus::Ok
}

pub fn stage_regressed(prev: Option<&str>, current: Option<&str>) -> bool {
    let (p, c) = match (prev, current) {
        (Some(p), Some(c)) => (p, c),
        _ => return false,
    };
    let pi = SCOPE_STAGES.iter().position(|&s| s == p);
    let ci = SCOPE_STAGES.iter().position(|&s| s == c);
    match (pi, ci) {
        (Some(pi), Some(ci)) => ci < pi,
        _ => false,
    }
}

pub fn normalize_batch_files<'a, I: IntoIterator<Item = &'a str>>(paths: I) -> HashSet<String> {
    paths.into_iter().map(normalize_path).collect()
}

pub fn out_of_batch(anchor: Option<&ScopeAnchor>, batch_files: &HashSet<String>) -> bool {
    let anchor = match anchor {
        Some(a) if !a.file_path.is_empty() => a,
        _ => return false,
    };
    if batch_files.is_empty() {
        return false;
    }
    // filter empty-string entries — degenerate input behaves like empty batch
    if !batch_files.iter().any(|f| !f.is_empty()) {
        return false;
    }
    let key = normalize_path(&anchor.file_path);
    !batch_files.contains(&key)
}

/// Returns `(nudge_message, new_budget)`.
///
/// A nudge fires exactly once per threshold crossing. Caller must adopt
/// the returned budget to prevent re-firing.
pub fn format_budget_nudge(budget: &RunnerBudget) -> (Option<String>, RunnerBudget) {
    let status = budget_status(budget);
    match status {
        BudgetStatus::Ok => (None, budget.clone()),
        BudgetStatus::SoftWarn => {
            if budget.soft_nudge_sent {
                return (None, budget.clone());
            }
            let remaining = budget.char_ceiling.saturating_sub(budget.output_chars);
            let msg = format!(
                "BUDGET SOFT \u{2014} {}/{} chars used (~{} remaining). Compact your next reply: one primary finding, skip recaps, then confirm you are still under budget.",
                budget.output_chars,
                budget.char_ceiling,
                format_with_commas(remaining),
            );
            let mut new_b = budget.clone();
            new_b.soft_nudge_sent = true;
            (Some(msg), new_b)
        }
        BudgetStatus::Rotate => {
            if budget.rotate_nudge_sent {
                return (None, budget.clone());
            }
            let remaining = budget.char_ceiling.saturating_sub(budget.output_chars);
            let msg = format!(
                "BUDGET ROTATE \u{2014} {} has crossed the hard ceiling (chars {}/{}; ~{} chars left, files {}/{}, fixes {}/{}). Finish the current tool call, emit a one-paragraph handoff brief, then wait for shutdown_request.",
                budget.runner_id,
                budget.output_chars,
                budget.char_ceiling,
                format_with_commas(remaining),
                budget.files_read.len(),
                budget.file_read_ceiling,
                budget.fixes_done,
                budget.fix_ceiling,
            );
            let mut new_b = budget.clone();
            new_b.rotate_nudge_sent = true;
            (Some(msg), new_b)
        }
    }
}

fn format_with_commas(n: usize) -> String {
    let s = n.to_string();
    let mut result = String::new();
    for (i, c) in s.chars().rev().enumerate() {
        if i > 0 && i % 3 == 0 {
            result.push(',');
        }
        result.push(c);
    }
    result.chars().rev().collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn load_golden() -> Value {
        let s = include_str!("../tests/parity/runner_budget.json");
        serde_json::from_str(s).unwrap()
    }

    fn budget_to_value(b: &RunnerBudget) -> Value {
        serde_json::json!({
            "runner_id": b.runner_id,
            "char_ceiling": b.char_ceiling,
            "file_read_ceiling": b.file_read_ceiling,
            "fix_ceiling": b.fix_ceiling,
            "output_chars": b.output_chars,
            "files_read": b.files_read,
            "fixes_done": b.fixes_done,
            "soft_nudge_sent": b.soft_nudge_sent,
            "rotate_nudge_sent": b.rotate_nudge_sent,
            "last_stage": b.last_stage,
            "last_file": b.last_file,
        })
    }

    fn status_str(s: &BudgetStatus) -> &'static str {
        match s {
            BudgetStatus::Ok => "OK",
            BudgetStatus::SoftWarn => "SOFT_WARN",
            BudgetStatus::Rotate => "ROTATE",
        }
    }

    #[test]
    fn parity_new_budget_default() {
        let g = load_golden();
        let b = new_budget("runner-1", None, None, None).unwrap();
        assert_eq!(budget_to_value(&b), g["new_budget_default"]);
    }

    #[test]
    fn parity_new_budget_custom() {
        let g = load_golden();
        let b = new_budget("runner-2", Some(50000), Some(10), Some(3)).unwrap();
        assert_eq!(budget_to_value(&b), g["new_budget_custom"]);
    }

    #[test]
    fn parity_parse_scope_two() {
        let g = load_golden();
        let anchors = parse_scope_anchor(
            "SCOPE: src/foo.py · reading\nsome text\nSCOPE: src/bar.py · fixing",
        );
        let v: Vec<Value> = anchors
            .iter()
            .map(|a| serde_json::json!({"file_path": a.file_path, "stage": a.stage}))
            .collect();
        assert_eq!(serde_json::json!(v), g["parse_scope_two"]);
    }

    #[test]
    fn parity_parse_scope_none() {
        let g = load_golden();
        let anchors = parse_scope_anchor("no anchors here");
        let empty: Vec<Value> = vec![];
        assert_eq!(
            serde_json::json!(anchors.len()),
            g["parse_scope_none"]
                .as_array()
                .map(|a| serde_json::json!(a.len()))
                .unwrap_or(serde_json::json!(0))
        );
        assert!(anchors.is_empty());
        let _ = g; // suppress unused
        assert_eq!(
            serde_json::json!(empty),
            serde_json::json!(g["parse_scope_none"])
        );
    }

    #[test]
    fn parity_update_budget() {
        let g = load_golden();
        let b = new_budget("runner-1", Some(100), Some(5), Some(3)).unwrap();
        let b2 = update_budget(&b, "SCOPE: src/foo.py · reading\nhello world", false, None);
        assert_eq!(budget_to_value(&b2), g["update_budget_basic"]);

        let b3 = update_budget(&b2, "SCOPE: src/bar.py · fixing\nfix done", true, None);
        assert_eq!(budget_to_value(&b3), g["update_budget_fix"]);

        let b4 = update_budget(&b3, "plain reply no scope", false, Some("src/baz.py"));
        assert_eq!(budget_to_value(&b4), g["update_budget_file_read"]);
    }

    #[test]
    fn parity_budget_status() {
        let g = load_golden();
        let b_ok = new_budget("r", Some(100), Some(5), Some(3)).unwrap();
        assert_eq!(
            status_str(&budget_status(&b_ok)),
            g["status_ok"].as_str().unwrap()
        );

        let b_soft = update_budget(&b_ok, &"x".repeat(62), false, None);
        assert_eq!(
            status_str(&budget_status(&b_soft)),
            g["status_soft"].as_str().unwrap()
        );

        let b_rotate = update_budget(&b_soft, &"x".repeat(20), false, None);
        assert_eq!(
            status_str(&budget_status(&b_rotate)),
            g["status_rotate"].as_str().unwrap()
        );

        // fix ceiling
        let b_fix = new_budget("r", None, None, Some(2)).unwrap();
        let b_fix2 = update_budget(&b_fix, "a", true, None);
        let b_fix3 = update_budget(&b_fix2, "b", true, None);
        assert_eq!(
            status_str(&budget_status(&b_fix3)),
            g["status_fix_rotate"].as_str().unwrap()
        );

        // explore-only
        let b_exp = new_budget("r", None, None, Some(0)).unwrap();
        let b_exp2 = update_budget(&b_exp, "a", true, None);
        assert_eq!(
            status_str(&budget_status(&b_exp2)),
            g["status_explore_ok"].as_str().unwrap()
        );
    }

    #[test]
    fn parity_stage_regressed() {
        let g = load_golden();
        assert_eq!(
            stage_regressed(Some("reading"), Some("hypothesizing")),
            g["stage_ok"].as_bool().unwrap()
        );
        assert_eq!(
            stage_regressed(Some("fixing"), Some("reading")),
            g["stage_regressed"].as_bool().unwrap()
        );
        assert_eq!(
            stage_regressed(None, Some("reading")),
            g["stage_none"].as_bool().unwrap()
        );
        assert_eq!(
            stage_regressed(Some("reading"), Some("blorp")),
            g["stage_unknown"].as_bool().unwrap()
        );
    }

    #[test]
    fn parity_normalize_batch() {
        let g = load_golden();
        let result = normalize_batch_files(["src/foo.py", "./src/foo.py", "src/bar.py"]);
        let mut sorted: Vec<String> = result.into_iter().collect();
        sorted.sort();
        let expected: Vec<String> = g["normalize_batch"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        assert_eq!(sorted, expected);
    }

    #[test]
    fn parity_out_of_batch() {
        let g = load_golden();
        let batch = normalize_batch_files(["src/foo.py"]);
        let a_in = ScopeAnchor {
            file_path: "src/foo.py".to_string(),
            stage: "reading".to_string(),
        };
        let a_out = ScopeAnchor {
            file_path: "src/other.py".to_string(),
            stage: "reading".to_string(),
        };
        assert_eq!(
            out_of_batch(Some(&a_in), &batch),
            g["out_of_batch_in"].as_bool().unwrap()
        );
        assert_eq!(
            out_of_batch(Some(&a_out), &batch),
            g["out_of_batch_out"].as_bool().unwrap()
        );
        assert_eq!(
            out_of_batch(None, &batch),
            g["out_of_batch_none"].as_bool().unwrap()
        );
        assert_eq!(
            out_of_batch(Some(&a_out), &HashSet::new()),
            g["out_of_batch_empty"].as_bool().unwrap()
        );
    }

    #[test]
    fn parity_format_budget_nudge() {
        let g = load_golden();
        let b = new_budget("runner-1", Some(1000), Some(20), Some(5)).unwrap();
        let b2 = update_budget(&b, &"x".repeat(620), false, None);
        let (msg, b_after) = format_budget_nudge(&b2);
        assert_eq!(msg.as_deref(), g["nudge_soft_msg"].as_str());
        assert_eq!(
            b_after.soft_nudge_sent,
            g["nudge_soft_flag"].as_bool().unwrap()
        );

        let (msg2, _) = format_budget_nudge(&b_after);
        assert!(msg2.is_none());
        assert!(g["nudge_soft_no_double"].is_null());

        let b3 = update_budget(&b_after, &"x".repeat(200), false, None);
        let (rot_msg, b_rot) = format_budget_nudge(&b3);
        assert_eq!(rot_msg.as_deref(), g["nudge_rotate_msg"].as_str());
        assert_eq!(
            b_rot.rotate_nudge_sent,
            g["nudge_rotate_flag"].as_bool().unwrap()
        );

        let (rot_msg2, _) = format_budget_nudge(&b_rot);
        assert!(rot_msg2.is_none());
        assert!(g["nudge_rotate_no_double"].is_null());
    }
}
