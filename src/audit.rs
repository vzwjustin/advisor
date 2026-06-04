//! Port of `advisor/audit.py` — post-hoc, pure-text analysis of an advisor run
//! transcript against a checkpoint's caps + batch layout.
//!
//! The CLI entry (`advisor audit RUN_ID`) additionally needs `checkpoint.py`'s
//! loader (tracked in PORT_NOTES); this module ports the analysis, which takes
//! the checkpoint fields it reads via [`AuditCheckpoint`].

use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::{json, Map, Value};

use crate::verify::{parse_findings_with_drift, INCOMPLETE_FILE_PATH};
use crate::Finding;

/// Max `PROTOCOL_VIOLATION` lines surfaced (`PROTOCOL_VIOLATION_CAP`).
pub const PROTOCOL_VIOLATION_CAP: usize = 1000;

const RUNNER_ATTRIBUTION_WINDOW: usize = 2000;

static FIX_ASSIGNMENT_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"(?i)##\s+Fix\s+assignment\s*\(\s*(?:\*\*LAST\s+FIX\*\*\s*\()?(?:fix\s+)?(\d+)\s+of\s+(\d+)")
        .expect("fix-assignment regex")
});
static RUNNER_MENTION_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"runner-(\d+)").expect("runner regex"));
static CONTEXT_PRESSURE_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)CONTEXT_PRESSURE").expect("context-pressure regex"));
static PROTOCOL_VIOLATION_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?mi)^\s*PROTOCOL_VIOLATION\s*:\s*[^\n]*").expect("protocol regex"));
static HANDOFF_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"##\s+Handoff\s+from\s+runner-\d+").expect("handoff regex"));
static SENDMESSAGE_TO_RUNNER_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r#"to\s*=\s*['"]runner-(\d+)['"]"#).expect("to-runner regex"));
static SENDMESSAGE_OPEN_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"SendMessage\s*\(").expect("sendmessage regex"));

/// The checkpoint fields the audit reads. Mirrors the subset of `Checkpoint`
/// used by `audit_transcript`.
#[derive(Debug, Clone)]
pub struct AuditCheckpoint {
    pub run_id: String,
    pub max_fixes_per_runner: i64,
    pub large_file_line_threshold: i64,
    pub large_file_max_fixes: i64,
    /// Task dicts (each with a `file_path`).
    pub tasks: Vec<Value>,
    /// Batch dicts (each with `tasks: [{file_path}]`).
    pub batches: Vec<Value>,
}

/// Structured output of [`audit_transcript`]. Mirrors `AuditReport`.
#[derive(Debug, Clone)]
pub struct AuditReport {
    pub run_id: String,
    pub max_fixes_per_runner: i64,
    pub large_file_line_threshold: i64,
    pub large_file_max_fixes: i64,
    /// Runner-id → fix count, in first-appearance order.
    pub fix_counts: Vec<(String, i64)>,
    pub cap_overruns: Vec<String>,
    pub context_pressure_runners: Vec<String>,
    pub context_pressure_count: usize,
    pub rotations: usize,
    pub protocol_violations: Vec<String>,
    pub findings_in_batch: Vec<Finding>,
    pub findings_out_of_batch: Vec<Finding>,
    pub batch_file_count: usize,
    /// Runner-id → fix numbers seen, in first-appearance order.
    pub fix_numbers: Vec<(String, Vec<i64>)>,
    pub protocol_violations_truncated: bool,
}

fn floor_char_boundary(s: &str, mut idx: usize) -> usize {
    if idx >= s.len() {
        return s.len();
    }
    while idx > 0 && !s.is_char_boundary(idx) {
        idx -= 1;
    }
    idx
}

/// Natural-sort key for `runner-N` ids (`runner-?` sentinel last).
fn runner_sort_key(runner_id: &str) -> (u8, i64, String) {
    let suffix = runner_id.strip_prefix("runner-").unwrap_or(runner_id);
    if !suffix.is_empty() && suffix.bytes().all(|b| b.is_ascii_digit()) {
        (0, suffix.parse().unwrap_or(0), runner_id.to_string())
    } else {
        (1, 0, runner_id.to_string())
    }
}

fn collect_batch_files(cp: &AuditCheckpoint) -> Vec<String> {
    // Insertion-ordered, deduped set of batched file paths.
    let mut files: Vec<String> = Vec::new();
    let push = |fp: &str, files: &mut Vec<String>| {
        if !fp.is_empty() && !files.contains(&fp.to_string()) {
            files.push(fp.to_string());
        }
    };
    for batch in &cp.batches {
        if let Some(tasks) = batch.get("tasks").and_then(|v| v.as_array()) {
            for t in tasks {
                if let Some(fp) = t.get("file_path").and_then(|v| v.as_str()) {
                    push(fp, &mut files);
                }
            }
        }
    }
    if files.is_empty() {
        for t in &cp.tasks {
            if let Some(fp) = t.get("file_path").and_then(|v| v.as_str()) {
                push(fp, &mut files);
            }
        }
    }
    files
}

fn attribute_fix_to_runner(transcript: &str, match_start: usize) -> String {
    let window_start = floor_char_boundary(
        transcript,
        match_start.saturating_sub(RUNNER_ATTRIBUTION_WINDOW),
    );
    let window = &transcript[window_start..match_start];
    if let Some(m) = SENDMESSAGE_TO_RUNNER_RE.find_iter(window).last() {
        if let Some(c) = SENDMESSAGE_TO_RUNNER_RE.captures(&window[m.start()..]) {
            return format!("runner-{}", &c[1]);
        }
    }
    if let Some(m) = RUNNER_MENTION_RE.captures_iter(window).last() {
        return format!("runner-{}", &m[1]);
    }
    "runner-?".to_string()
}

fn attribute_context_pressure_to_runner(transcript: &str, match_start: usize) -> String {
    let window_start = floor_char_boundary(
        transcript,
        match_start.saturating_sub(RUNNER_ATTRIBUTION_WINDOW),
    );
    let before = &transcript[window_start..match_start];

    if let Some(opener) = SENDMESSAGE_OPEN_RE.find_iter(before).last() {
        let body_start = window_start + opener.end();
        let body_slice = &transcript[body_start..match_start];
        let body_without_to = SENDMESSAGE_TO_RUNNER_RE.replace_all(body_slice, "");
        if let Some(m) = RUNNER_MENTION_RE.captures_iter(&body_without_to).last() {
            return format!("runner-{}", &m[1]);
        }
    }
    if let Some(m) = SENDMESSAGE_TO_RUNNER_RE.captures_iter(before).last() {
        return format!("runner-{}", &m[1]);
    }
    "runner-?".to_string()
}

/// Remove fenced (``` / ~~~) regions, preserving line count; restores the tail
/// on an unclosed fence. Mirrors `_strip_fenced_blocks`.
fn strip_fenced_blocks(text: &str) -> String {
    let lines: Vec<&str> = text.split('\n').collect();
    let mut out: Vec<String> = Vec::with_capacity(lines.len());
    let mut in_fence = false;
    let mut marker: Option<&str> = None;
    let mut fence_open_line: Option<usize> = None;
    for (idx, ln) in lines.iter().enumerate() {
        let stripped = ln.trim();
        if stripped.starts_with("```") || stripped.starts_with("~~~") {
            if marker.is_none() {
                marker = Some(if stripped.starts_with("```") {
                    "```"
                } else {
                    "~~~"
                });
                in_fence = true;
                fence_open_line = Some(idx);
                out.push(String::new());
                continue;
            }
            marker = None;
            in_fence = false;
            fence_open_line = None;
            out.push(String::new());
            continue;
        }
        if in_fence {
            out.push(String::new());
        } else {
            out.push((*ln).to_string());
        }
    }
    if in_fence {
        if let Some(open) = fence_open_line {
            for (idx, line) in lines.iter().enumerate().skip(open) {
                out[idx] = (*line).to_string();
            }
        }
    }
    out.join("\n")
}

type FixTally = (
    Vec<(String, i64)>,
    Vec<(String, Vec<i64>)>,
    Vec<(String, Vec<i64>)>,
);

fn audit_fix_assignments(transcript: &str) -> FixTally {
    let transcript = &strip_fenced_blocks(transcript);
    let mut fix_counts: Vec<(String, i64)> = Vec::new();
    let mut fix_numbers: Vec<(String, Vec<i64>)> = Vec::new();
    let mut per_message_caps: Vec<(String, Vec<i64>)> = Vec::new();
    for m in FIX_ASSIGNMENT_RE.captures_iter(transcript) {
        let whole = m.get(0).unwrap();
        let fix_num: i64 = m[1].parse().unwrap_or(0);
        let msg_cap: i64 = m[2].parse().unwrap_or(0);
        let runner = attribute_fix_to_runner(transcript, whole.start());
        upsert_count(&mut fix_counts, &runner);
        push_num(&mut fix_numbers, &runner, fix_num);
        push_num(&mut per_message_caps, &runner, msg_cap);
    }
    (fix_counts, fix_numbers, per_message_caps)
}

fn upsert_count(v: &mut Vec<(String, i64)>, key: &str) {
    if let Some(e) = v.iter_mut().find(|(k, _)| k == key) {
        e.1 += 1;
    } else {
        v.push((key.to_string(), 1));
    }
}

fn push_num(v: &mut Vec<(String, Vec<i64>)>, key: &str, n: i64) {
    if let Some(e) = v.iter_mut().find(|(k, _)| k == key) {
        e.1.push(n);
    } else {
        v.push((key.to_string(), vec![n]));
    }
}

fn audit_cap_overruns(
    fix_counts: &[(String, i64)],
    cap: i64,
    per_message_caps: &[(String, Vec<i64>)],
) -> Vec<String> {
    let mut sorted: Vec<&(String, i64)> = fix_counts.iter().collect();
    sorted.sort_by_key(|a| runner_sort_key(&a.0));
    let mut out = Vec::new();
    for (runner, count) in sorted {
        let observed = per_message_caps
            .iter()
            .find(|(k, _)| k == runner)
            .map(|(_, v)| v.as_slice())
            .unwrap_or(&[]);
        let effective_cap = observed
            .iter()
            .copied()
            .chain(std::iter::once(cap))
            .min()
            .unwrap_or(cap);
        if *count > effective_cap {
            if runner == "runner-?" {
                out.push(format!(
                    "runner-? (unattributed): {count} fix assignments detected (cap={effective_cap}) — attribution failed; check transcript for dense dispatch blocks"
                ));
            } else {
                out.push(format!(
                    "{runner}: observed {count} fix assignments (cap={effective_cap}) — rotation was late or missed"
                ));
            }
        }
    }
    out
}

fn audit_context_pressure(transcript: &str) -> (Vec<String>, usize) {
    let unfenced = strip_fenced_blocks(transcript);
    let matches: Vec<usize> = CONTEXT_PRESSURE_RE
        .find_iter(&unfenced)
        .map(|m| m.start())
        .collect();
    let mut ordered: Vec<String> = Vec::new();
    for start in &matches {
        let runner = attribute_context_pressure_to_runner(&unfenced, *start);
        if !ordered.contains(&runner) {
            ordered.push(runner);
        }
    }
    (ordered, matches.len())
}

fn audit_protocol_violations(transcript: &str) -> (Vec<String>, bool) {
    let unfenced = strip_fenced_blocks(transcript);
    let mut order: Vec<String> = Vec::new();
    let mut counts: std::collections::HashMap<String, i64> = std::collections::HashMap::new();
    let mut truncated = false;
    let mut total = 0usize;
    for m in PROTOCOL_VIOLATION_RE.find_iter(&unfenced) {
        if total >= PROTOCOL_VIOLATION_CAP {
            truncated = true;
            break;
        }
        total += 1;
        let text = m.as_str().to_string();
        let entry = counts.entry(text.clone()).or_insert(0);
        if *entry == 0 {
            order.push(text);
        }
        *entry += 1;
    }
    let violations = order
        .iter()
        .map(|text| {
            let n = counts.get(text).copied().unwrap_or(1);
            if n == 1 {
                text.clone()
            } else {
                format!("{text} (×{n})")
            }
        })
        .collect();
    (violations, truncated)
}

fn audit_scope_drift(transcript: &str, batch_files: &[String]) -> (Vec<Finding>, Vec<Finding>) {
    let (mut kept, mut dropped) = if !batch_files.is_empty() {
        parse_findings_with_drift(transcript, Some(batch_files))
    } else {
        (parse_findings_with_drift(transcript, None).0, Vec::new())
    };
    kept.retain(|f| f.file_path != INCOMPLETE_FILE_PATH);
    dropped.retain(|f| f.file_path != INCOMPLETE_FILE_PATH);
    (kept, dropped)
}

/// Produce an [`AuditReport`] for a transcript / checkpoint pair. Mirrors
/// `audit_transcript`.
pub fn audit_transcript(transcript: &str, cp: &AuditCheckpoint) -> AuditReport {
    let (fix_counts, fix_numbers, per_message_caps) = audit_fix_assignments(transcript);
    let cap_overruns = audit_cap_overruns(&fix_counts, cp.max_fixes_per_runner, &per_message_caps);
    let (context_pressure_runners, context_pressure_count) = audit_context_pressure(transcript);
    let rotations = HANDOFF_RE.find_iter(transcript).count();
    let (protocol_violations, protocol_violations_truncated) =
        audit_protocol_violations(transcript);
    let batch_files = collect_batch_files(cp);
    let (findings_in_batch, findings_out_of_batch) = audit_scope_drift(transcript, &batch_files);

    AuditReport {
        run_id: cp.run_id.clone(),
        max_fixes_per_runner: cp.max_fixes_per_runner,
        large_file_line_threshold: cp.large_file_line_threshold,
        large_file_max_fixes: cp.large_file_max_fixes,
        fix_counts,
        cap_overruns,
        context_pressure_runners,
        context_pressure_count,
        rotations,
        protocol_violations,
        findings_in_batch,
        findings_out_of_batch,
        batch_file_count: batch_files.len(),
        fix_numbers,
        protocol_violations_truncated,
    }
}

fn finding_value(f: &Finding) -> Value {
    json!({
        "file_path": f.file_path,
        "severity": f.severity,
        "description": f.description,
        "evidence": f.evidence,
        "fix": f.fix,
        "rule_id": f.rule_id,
        "expected_vs_actual": f.expected_vs_actual,
    })
}

/// Serialize an [`AuditReport`] to the JSON-friendly dict shape. Mirrors
/// `audit_to_dict`.
pub fn audit_to_dict(report: &AuditReport) -> Value {
    let mut fix_counts = Map::new();
    for (k, v) in &report.fix_counts {
        fix_counts.insert(k.clone(), json!(v));
    }
    let mut fix_numbers = Map::new();
    for (k, v) in &report.fix_numbers {
        fix_numbers.insert(k.clone(), json!(v));
    }
    json!({
        "run_id": report.run_id,
        "caps": {
            "max_fixes_per_runner": report.max_fixes_per_runner,
            "large_file_line_threshold": report.large_file_line_threshold,
            "large_file_max_fixes": report.large_file_max_fixes,
        },
        "fix_counts": Value::Object(fix_counts),
        "fix_numbers": Value::Object(fix_numbers),
        "cap_overruns": report.cap_overruns,
        "context_pressure": {
            "runners": report.context_pressure_runners,
            "total_count": report.context_pressure_count,
        },
        "rotations": report.rotations,
        "protocol_violations": report.protocol_violations,
        "protocol_violations_truncated": report.protocol_violations_truncated,
        "batch_file_count": report.batch_file_count,
        "findings_in_batch": report.findings_in_batch.iter().map(finding_value).collect::<Vec<_>>(),
        "findings_out_of_batch": report.findings_out_of_batch.iter().map(finding_value).collect::<Vec<_>>(),
    })
}

fn single_line(value: &str) -> String {
    value
        .replace("\r\n", " ")
        .replace(['\n', '\r'], " ")
        .trim()
        .to_string()
}

fn sorted_runner_keys(counts: &[(String, i64)]) -> Vec<&(String, i64)> {
    let mut v: Vec<&(String, i64)> = counts.iter().collect();
    v.sort_by_key(|a| runner_sort_key(&a.0));
    v
}

/// Render an [`AuditReport`] as a human-readable markdown block. Mirrors
/// `format_audit_report`.
pub fn format_audit_report(report: &AuditReport) -> String {
    let mut lines: Vec<String> = vec![
        format!("# Audit — run {}", report.run_id),
        String::new(),
        format!(
            "Caps: max_fixes_per_runner={}, large_file_line_threshold={}, large_file_max_fixes={}",
            report.max_fixes_per_runner,
            report.large_file_line_threshold,
            report.large_file_max_fixes
        ),
        format!("Batch file universe: {} files", report.batch_file_count),
        String::new(),
    ];

    // Fix counts
    lines.push("## Fix counts per runner".to_string());
    if !report.fix_counts.is_empty() {
        for (runner, count) in sorted_runner_keys(&report.fix_counts) {
            let nums = report
                .fix_numbers
                .iter()
                .find(|(k, _)| k == runner)
                .map(|(_, v)| v.as_slice())
                .unwrap_or(&[]);
            let nums_str = if nums.is_empty() {
                String::new()
            } else {
                format!(
                    " (fix numbers seen: {})",
                    nums.iter()
                        .map(|n| n.to_string())
                        .collect::<Vec<_>>()
                        .join(", ")
                )
            };
            lines.push(format!("- {runner}: {count}{nums_str}"));
        }
    } else {
        lines.push("- (none)".to_string());
    }
    lines.push(String::new());

    // Cap overruns
    lines.push("## Cap overruns".to_string());
    if !report.cap_overruns.is_empty() {
        for o in &report.cap_overruns {
            lines.push(format!("- {o}"));
        }
        lines.push(format!(
            "- _Tip: rotate sooner by lowering `--max-fixes-per-runner` (or `--large-file-max-fixes` for ≥{}-line files), or raise `--max-runners` so the advisor has fresh capacity to hand off to._",
            report.large_file_line_threshold
        ));
    } else {
        lines.push("- (none — every runner stayed within cap)".to_string());
    }
    lines.push(String::new());

    // Context pressure
    lines.push("## CONTEXT_PRESSURE pings".to_string());
    if !report.context_pressure_runners.is_empty() {
        let runner_word = if report.context_pressure_runners.len() == 1 {
            "runner"
        } else {
            "runners"
        };
        lines.push(format!(
            "- total occurrences: {} (from {} {runner_word})",
            report.context_pressure_count,
            report.context_pressure_runners.len()
        ));
        for r in &report.context_pressure_runners {
            lines.push(format!("  - {r}"));
        }
        lines.push("- _Tip: pings are expected and healthy — they trigger rotation. Investigate only if a ping landed but no rotation followed (see the Rotations section below)._".to_string());
    } else {
        lines.push("- (none — no runner self-reported saturation)".to_string());
    }
    lines.push(String::new());

    // Rotations
    lines.push("## Rotations (handoffs)".to_string());
    lines.push(format!("- count: {}", report.rotations));
    lines.push(String::new());

    // Protocol violations
    lines.push("## PROTOCOL_VIOLATION strings".to_string());
    if !report.protocol_violations.is_empty() {
        for v in &report.protocol_violations {
            lines.push(format!("- {v}"));
        }
        if report.protocol_violations_truncated {
            lines.push(format!(
                "- … (truncated at {PROTOCOL_VIOLATION_CAP}; transcript contains additional matches)"
            ));
        }
        lines.push("- _Tip: every PROTOCOL_VIOLATION is a near-miss the advisor self-flagged — read the surrounding transcript to see what the advisor was about to do and why it stopped. Recurring violations of the same shape suggest a prompt change._".to_string());
    } else {
        lines.push("- (none)".to_string());
    }
    lines.push(String::new());

    // Scope drift
    lines.push("## Out-of-batch findings (scope drift)".to_string());
    if !report.findings_out_of_batch.is_empty() {
        for f in &report.findings_out_of_batch {
            lines.push(format!(
                "- `{}` [{}] — {}",
                f.file_path,
                f.severity,
                single_line(&f.description)
            ));
        }
        lines.push("- _Tip: drift means a runner reported on a file outside its batch. Triage each one as either (a) a real finding worth promoting to a follow-up run, or (b) signal the runner's scope anchors weren't being enforced — tighten the per-runner REDIRECT cadence._".to_string());
    } else {
        lines.push("- (none — no runner reported on a file outside its batch)".to_string());
    }
    lines.push(String::new());

    lines.push(format!(
        "## In-batch findings: {}",
        report.findings_in_batch.len()
    ));
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    const TRANSCRIPT: &str = "\nSendMessage(to='runner-2', message='dispatch')\n## Fix assignment (fix 1 of 2)\nSendMessage(to='runner-2', message='dispatch')\n## Fix assignment (fix 2 of 2)\nSendMessage(to='runner-2', message='dispatch')\n## Fix assignment (fix 3 of 2)\nrunner-2 says CONTEXT_PRESSURE now\n## Handoff from runner-2\nSendMessage(to='runner-3', message='dispatch')\n## Fix assignment (fix 1 of 3)\nPROTOCOL_VIOLATION: almost violated scope rule\n\n### Finding 1\n- **File**: `src/auth.py:10`\n- **Severity**: HIGH\n- **Description**: real in-batch issue\n- **Evidence**: ev\n- **Fix**: fx\n\n### Finding 2\n- **File**: `other/drift.py:5`\n- **Severity**: LOW\n- **Description**: out of batch\n- **Evidence**: ev2\n- **Fix**: fx2\n\n```\nPROTOCOL_VIOLATION: this is quoted inside a fence and must be ignored\n```\n";

    fn checkpoint() -> AuditCheckpoint {
        AuditCheckpoint {
            run_id: "20260604T000000Z-abcd1234".into(),
            max_fixes_per_runner: 2,
            large_file_line_threshold: 800,
            large_file_max_fixes: 3,
            tasks: vec![json!({"file_path": "src/auth.py", "priority": 5, "prompt": "p"})],
            batches: vec![
                json!({"batch_id": 1, "complexity": "high", "top_priority": 5, "tasks": [{"file_path": "src/auth.py", "priority": 5}]}),
            ],
        }
    }

    fn golden() -> Value {
        serde_json::from_str(include_str!("../tests/parity/audit.json")).unwrap()
    }

    #[test]
    fn audit_to_dict_matches_python() {
        let report = audit_transcript(TRANSCRIPT, &checkpoint());
        assert_eq!(audit_to_dict(&report), golden()["to_dict"]);
    }

    #[test]
    fn format_matches_python() {
        let report = audit_transcript(TRANSCRIPT, &checkpoint());
        assert_eq!(
            format_audit_report(&report),
            golden()["format"].as_str().unwrap()
        );
    }
}
