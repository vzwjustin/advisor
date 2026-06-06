//! Port of `advisor/verify.py` — formats findings into the verification prompt
//! and parses agent output back into [`Finding`]s.
//!
//! The parser is a faithful translation of the Python line-by-line state
//! machine: `### Finding` / second-`file_path` block boundaries, fenced-code
//! tracking with auto-recovery, list vs plain field styles, continuation
//! accumulation, partial-drop sentinels, and optional batch scope filtering.

use std::collections::HashMap;

use crate::fence::fence;
use crate::fs::normalize_path;
use crate::models::{Finding, Severity};

/// Sentinel `file_path` for a block missing required fields ("partial drop").
pub const INCOMPLETE_FILE_PATH: &str = "<incomplete>";

const REQUIRED_FIELDS: [&str; 5] = ["file_path", "severity", "description", "evidence", "fix"];

const VERIFY_PROMPT_HEAD: &str = "You are the verification agent. Your job is to review findings from other agents and determine which are real, significant issues versus false positives or low-value noise.\n\n## Findings to Verify\n\n";

const VERIFY_PROMPT_TAIL: &str = "\n\n## Instructions\n\nFor each finding:\n1. Read the cited file and line to confirm the issue exists.\n2. Check whether the issue is exploitable or impactful in practice.\n3. Reject findings that are:\n   - False positives (the code is actually safe)\n   - Theoretical only (requires unrealistic conditions)\n   - Duplicates of another finding\n   - Too minor to act on (style nits, unlikely edge cases)\n\n4. For each finding, output:\n   - **CONFIRMED** or **REJECTED**\n   - **Reason**: why you confirmed or rejected it\n\n5. End with a summary: how many confirmed, how many rejected, and the top 3 most critical issues to fix first.\n\nBe strict. Only confirm issues that are real and worth fixing.";

/// Field key → recognized line prefixes, in the order Python iterates the
/// `_KEY_PREFIXES` dict (list markers before the unadorned form).
fn key_prefixes() -> [(&'static str, &'static [&'static str]); 7] {
    [
        ("file_path", &["- **File**:", "* **File**:", "**File**:"]),
        (
            "severity",
            &["- **Severity**:", "* **Severity**:", "**Severity**:"],
        ),
        (
            "description",
            &[
                "- **Description**:",
                "* **Description**:",
                "**Description**:",
            ],
        ),
        (
            "evidence",
            &["- **Evidence**:", "* **Evidence**:", "**Evidence**:"],
        ),
        (
            "expected_vs_actual",
            &[
                "- **Expected → Actual**:",
                "* **Expected → Actual**:",
                "**Expected → Actual**:",
                "- **Expected -> Actual**:",
                "* **Expected -> Actual**:",
                "**Expected -> Actual**:",
            ],
        ),
        ("fix", &["- **Fix**:", "* **Fix**:", "**Fix**:"]),
        ("rule_id", &["- **Rule**:", "* **Rule**:", "**Rule**:"]),
    ]
}

// Control-stripping sets for `safe_inline`, mirroring the Python frozensets.
const SAFE_INLINE_STRIP_EXTRA: [u32; 3] = [0x85, 0x2028, 0x2029];
const SAFE_INLINE_BIDI: [u32; 12] = [
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2060, 0x200E, 0x200F, 0x2066, 0x2067, 0x2068, 0x2069,
];

/// Canonicalize a severity string to the allowlist (or `UNKNOWN`). Mirrors
/// `_canonical_severity`.
fn canonical_severity(raw: &str) -> String {
    Severity::canonical(raw).as_str().to_string()
}

/// Sanitize a runner-authored field for inline embedding. Strips C0/DEL, NEL,
/// LS/PS, and bidi controls; replaces backticks with an ASCII apostrophe; drops
/// zero-width code points. Mirrors `_safe_inline`.
pub fn safe_inline(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    for c in s.chars() {
        let o = c as u32;
        if o < 0x20
            || o == 0x7F
            || SAFE_INLINE_STRIP_EXTRA.contains(&o)
            || SAFE_INLINE_BIDI.contains(&o)
        {
            continue;
        }
        out.push(c);
    }
    out.chars()
        .filter_map(|c| match c {
            '`' => Some('\''),
            '\u{200B}' | '\u{200C}' | '\u{200D}' | '\u{FEFF}' | '\u{00AD}' => None,
            other => Some(other),
        })
        .collect()
}

/// Format findings into a markdown block for the verification prompt. Mirrors
/// `format_findings_block`.
pub fn format_findings_block(findings: &[Finding]) -> String {
    if findings.is_empty() {
        return "_No findings to verify._".to_string();
    }
    let mut lines: Vec<String> = Vec::new();
    for (i, f) in findings.iter().enumerate() {
        lines.push(format!("### Finding {}", i + 1));
        lines.push(format!("- **File**: `{}`", safe_inline(&f.file_path)));
        lines.push(format!("- **Severity**: {}", safe_inline(&f.severity)));
        lines.push(format!(
            "- **Description**: {}",
            safe_inline(&f.description)
        ));
        lines.push(format!("- **Evidence**: {}", safe_inline(&f.evidence)));
        if !f.expected_vs_actual.is_empty() {
            lines.push(format!(
                "- **Expected → Actual**: {}",
                safe_inline(&f.expected_vs_actual)
            ));
        }
        lines.push(format!("- **Fix**: {}", safe_inline(&f.fix)));
        if let Some(rule_id) = &f.rule_id {
            if !rule_id.is_empty() {
                lines.push(format!("- **Rule**: {}", safe_inline(rule_id)));
            }
        }
        lines.push(String::new());
    }
    lines.join("\n")
}

/// Build the complete verification agent prompt. Mirrors `build_verify_prompt`.
pub fn build_verify_prompt(findings: &[Finding]) -> String {
    let block = format_findings_block(findings);
    format!(
        "{VERIFY_PROMPT_HEAD}{}{VERIFY_PROMPT_TAIL}",
        fence(&block, "")
    )
}

/// Best-effort parse of agent output into findings. Mirrors
/// `parse_findings_from_text`.
pub fn parse_findings_from_text(text: &str, batch_files: Option<&[String]>) -> Vec<Finding> {
    parse_findings_with_drift(text, batch_files).0
}

/// Parse findings, returning `(kept, dropped_out_of_batch)`. Mirrors
/// `parse_findings_with_drift`.
pub fn parse_findings_with_drift(
    text: &str,
    batch_files: Option<&[String]>,
) -> (Vec<Finding>, Vec<Finding>) {
    let normalized_batch: Option<Vec<String>> =
        batch_files.map(|b| b.iter().map(|p| normalize_path(p)).collect());

    let raw = parse_blocks(text);
    match normalized_batch {
        None => (
            raw.into_iter()
                .filter(|f| f.file_path != INCOMPLETE_FILE_PATH)
                .collect(),
            Vec::new(),
        ),
        Some(batch) => {
            let mut kept = Vec::new();
            let mut dropped = Vec::new();
            for f in raw {
                if batch.contains(&normalize_path(&f.file_path)) {
                    kept.push(f);
                } else {
                    dropped.push(f);
                }
            }
            (kept, dropped)
        }
    }
}

fn extract_value(line: &str, prefix: &str) -> String {
    line[prefix.len()..]
        .trim()
        .trim_matches('`')
        .trim()
        .to_string()
}

fn match_key(stripped: &str) -> Option<(String, String)> {
    for (key, prefixes) in key_prefixes() {
        for prefix in prefixes {
            if stripped.starts_with(prefix) {
                return Some((key.to_string(), extract_value(stripped, prefix)));
            }
        }
    }
    None
}

fn merge_parts(current: &mut HashMap<String, String>, parts: &mut HashMap<String, Vec<String>>) {
    for (key, pieces) in parts.drain() {
        if pieces.is_empty() {
            continue;
        }
        let joined = pieces.join(" ");
        let existing = current.get(&key).cloned().unwrap_or_default();
        let merged = if !existing.is_empty() {
            format!("{existing} {joined}").trim().to_string()
        } else {
            joined.trim().to_string()
        };
        current.insert(key, merged);
    }
}

fn flush(
    current: &mut HashMap<String, String>,
    parts: &mut HashMap<String, Vec<String>>,
    findings: &mut Vec<Finding>,
) {
    merge_parts(current, parts);
    if let Some(f) = dict_to_finding(current) {
        findings.push(f);
    }
}

/// Python list repr for the partial-drop description (`['a', 'b']`).
fn py_list(v: &[&str]) -> String {
    let inner: Vec<String> = v.iter().map(|s| format!("'{s}'")).collect();
    format!("[{}]", inner.join(", "))
}

fn dict_to_finding(d: &HashMap<String, String>) -> Option<Finding> {
    let absent: Vec<&str> = REQUIRED_FIELDS
        .iter()
        .filter(|k| !d.contains_key(**k))
        .copied()
        .collect();
    let empty: Vec<&str> = REQUIRED_FIELDS
        .iter()
        .filter(|k| d.get(**k).is_some_and(|v| v.is_empty()))
        .copied()
        .collect();

    if !absent.is_empty() || !empty.is_empty() {
        let populated: Vec<&str> = REQUIRED_FIELDS
            .iter()
            .filter(|k| d.get(**k).is_some_and(|v| !v.is_empty()))
            .copied()
            .collect();
        if populated.is_empty() {
            return None;
        }
        let description = d
            .get("description")
            .filter(|s| !s.is_empty())
            .cloned()
            .unwrap_or_else(|| {
                format!(
                    "<partial: empty={} absent={}>",
                    py_list(&empty),
                    py_list(&absent)
                )
            });
        return Some(Finding {
            file_path: INCOMPLETE_FILE_PATH.to_string(),
            severity: canonical_severity(d.get("severity").map_or("", |s| s.as_str())),
            description,
            evidence: d.get("evidence").cloned().unwrap_or_default(),
            fix: d.get("fix").cloned().unwrap_or_default(),
            rule_id: d.get("rule_id").filter(|s| !s.is_empty()).cloned(),
            expected_vs_actual: d.get("expected_vs_actual").cloned().unwrap_or_default(),
        });
    }

    Some(Finding {
        file_path: d.get("file_path").cloned().unwrap_or_default(),
        severity: canonical_severity(d.get("severity").map_or("", |s| s.as_str())),
        description: d.get("description").cloned().unwrap_or_default(),
        evidence: d.get("evidence").cloned().unwrap_or_default(),
        fix: d.get("fix").cloned().unwrap_or_default(),
        rule_id: d.get("rule_id").filter(|s| !s.is_empty()).cloned(),
        expected_vs_actual: d.get("expected_vs_actual").cloned().unwrap_or_default(),
    })
}

fn parse_blocks(text: &str) -> Vec<Finding> {
    let mut findings: Vec<Finding> = Vec::new();
    let mut current: HashMap<String, String> = HashMap::new();
    let mut parts: HashMap<String, Vec<String>> = HashMap::new();
    let mut active_key: Option<String> = None;
    let mut field_style: Option<&'static str> = None;
    let mut in_header_block = false;
    let mut in_fence = false;
    let mut fence_marker: Option<&'static str> = None;

    let append = |parts: &mut HashMap<String, Vec<String>>, key: &str, value: &str| {
        parts
            .entry(key.to_string())
            .or_default()
            .push(value.to_string());
    };

    for line in text.split('\n') {
        let stripped = line.trim();

        // Fence tracking for the whole file.
        if stripped.starts_with("```") || stripped.starts_with("~~~") {
            let marker = if stripped.starts_with("```") {
                "```"
            } else {
                "~~~"
            };
            if fence_marker.is_none() {
                fence_marker = Some(marker);
                in_fence = true;
            } else {
                fence_marker = None;
                in_fence = false;
            }
            continue;
        }

        // H2 boundary (with fence auto-recovery).
        if line.starts_with("## ") && !line.starts_with("### ") && in_fence {
            in_fence = false;
            fence_marker = None;
        }
        if !in_fence && stripped.starts_with("## ") && !stripped.starts_with("### ") {
            let block_is_complete = REQUIRED_FIELDS.iter().all(|k| current.contains_key(*k));
            if active_key.is_none() || block_is_complete {
                if !current.is_empty() {
                    flush(&mut current, &mut parts, &mut findings);
                }
                current.clear();
                parts.clear();
                active_key = None;
                field_style = None;
                in_header_block = false;
                continue;
            }
            if let Some(ak) = &active_key {
                append(&mut parts, ak, stripped);
            }
            continue;
        }

        // `### Finding` boundary (with fence auto-recovery).
        if line.starts_with("### Finding") && in_fence {
            in_fence = false;
            fence_marker = None;
        }
        if !in_fence && stripped.starts_with("### Finding") {
            let block_is_complete = REQUIRED_FIELDS.iter().all(|k| current.contains_key(*k));
            if active_key.is_none() || block_is_complete {
                if !current.is_empty() {
                    flush(&mut current, &mut parts, &mut findings);
                }
                current.clear();
                parts.clear();
                active_key = None;
                field_style = None;
                in_header_block = true;
                continue;
            }
            if let Some(ak) = &active_key {
                append(&mut parts, ak, stripped);
            }
            continue;
        }

        let matched = match_key(stripped);
        let is_list_item = stripped.starts_with("- ") || stripped.starts_with("* ");
        let is_plain_item = stripped.starts_with("**");
        let opens_plain_block = matched.is_some()
            && is_plain_item
            && (field_style == Some("plain") || (field_style.is_none() && current.is_empty()));

        let mut handled = false;
        if let Some((key, value)) = matched {
            if (is_list_item || opens_plain_block) && !in_fence {
                handled = true;
                if !current.contains_key(&key) {
                    current.insert(key.clone(), value);
                    active_key = Some(key);
                    if field_style.is_none() {
                        field_style = Some(if is_list_item { "list" } else { "plain" });
                    }
                } else if key == "file_path" && !in_header_block {
                    flush(&mut current, &mut parts, &mut findings);
                    current.clear();
                    parts.clear();
                    current.insert(key.clone(), value);
                    active_key = Some(key);
                    field_style = Some(if is_list_item { "list" } else { "plain" });
                } else if let Some(ak) = active_key.clone() {
                    append(&mut parts, &ak, stripped);
                }
            }
        }
        if !handled && !stripped.is_empty() && !stripped.starts_with("### Finding") {
            if let Some(ak) = active_key.clone() {
                append(&mut parts, &ak, stripped);
            }
        }
    }

    if !current.is_empty() {
        flush(&mut current, &mut parts, &mut findings);
    }

    findings
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn golden() -> Value {
        serde_json::from_str(include_str!("../tests/parity/verify.json")).unwrap()
    }

    fn f(
        file_path: &str,
        severity: &str,
        description: &str,
        evidence: &str,
        fix: &str,
        rule_id: Option<&str>,
        eva: &str,
    ) -> Finding {
        Finding {
            file_path: file_path.into(),
            severity: severity.into(),
            description: description.into(),
            evidence: evidence.into(),
            fix: fix.into(),
            rule_id: rule_id.map(|s| s.into()),
            expected_vs_actual: eva.into(),
        }
    }

    fn findings() -> Vec<Finding> {
        vec![
            f(
                "src/auth.py:42",
                "HIGH",
                "SQL injection",
                "user input to query",
                "parameterize",
                None,
                "",
            ),
            f(
                "api/x.py:1",
                "critical",
                "RCE via eval",
                "eval(req.body)",
                "remove eval",
                Some("advisor/high/abc"),
                "expected safe -> got eval",
            ),
        ]
    }

    fn parsed_to_value(fs: &[Finding]) -> Value {
        Value::Array(
            fs.iter()
                .map(|f| {
                    serde_json::json!({
                        "file_path": f.file_path,
                        "severity": f.severity,
                        "description": f.description,
                        "evidence": f.evidence,
                        "fix": f.fix,
                        "rule_id": f.rule_id,
                        "expected_vs_actual": f.expected_vs_actual,
                    })
                })
                .collect(),
        )
    }

    #[test]
    fn format_and_prompt_match_python() {
        let g = golden();
        assert_eq!(
            format_findings_block(&findings()),
            g["format_block"].as_str().unwrap()
        );
        assert_eq!(
            format_findings_block(&[]),
            g["format_block_empty"].as_str().unwrap()
        );
        assert_eq!(
            build_verify_prompt(&findings()[..1]),
            g["verify_prompt"].as_str().unwrap()
        );
    }

    #[test]
    fn parse_variants_match_python() {
        let g = golden();
        let roundtrip = parse_findings_from_text(&format_findings_block(&findings()), None);
        assert_eq!(parsed_to_value(&roundtrip), g["roundtrip_parsed"]);

        let plain = "**File**: `lib/a.py:5`\n**Severity**: medium\n**Description**: weak hash\n**Evidence**: md5 used\n**Fix**: use sha256\n";
        assert_eq!(
            parsed_to_value(&parse_findings_from_text(plain, None)),
            g["plain_parsed"]
        );

        let ascii = "### Finding 1\n- **File**: `a.py:1`\n- **Severity**: LOW\n- **Description**: d\n- **Evidence**: e\n- **Expected -> Actual**: foo -> bar\n- **Fix**: f\n";
        assert_eq!(
            parsed_to_value(&parse_findings_from_text(ascii, None)),
            g["ascii_arrow_parsed"]
        );

        let fenced = "### Finding 1\n- **File**: `a.py:1`\n- **Severity**: HIGH\n- **Description**: d\n- **Evidence**:\n```\n### Finding 99 (not real)\ncode here\n```\n- **Fix**: f\n";
        assert_eq!(
            parsed_to_value(&parse_findings_from_text(fenced, None)),
            g["fenced_parsed"]
        );

        let sev = "### Finding 1\n- **File**: `a.py:1`\n- **Severity**: Bogus\n- **Description**: d\n- **Evidence**: e\n- **Fix**: f\n";
        assert_eq!(
            parsed_to_value(&parse_findings_from_text(sev, None)),
            g["invented_severity_parsed"]
        );

        let headerless = "- **File**: `a.py:1`\n- **Severity**: LOW\n- **Description**: d1\n- **Evidence**: e1\n- **Fix**: f1\n- **File**: `b.py:2`\n- **Severity**: HIGH\n- **Description**: d2\n- **Evidence**: e2\n- **Fix**: f2\n";
        assert_eq!(
            parsed_to_value(&parse_findings_from_text(headerless, None)),
            g["headerless_parsed"]
        );
    }

    #[test]
    fn scope_filter_matches_python() {
        let g = golden();
        let block = format_findings_block(&findings());
        let (kept, dropped) = parse_findings_with_drift(&block, Some(&["src/auth.py".to_string()]));
        let kept_paths: Vec<&str> = kept.iter().map(|f| f.file_path.as_str()).collect();
        let dropped_paths: Vec<&str> = dropped.iter().map(|f| f.file_path.as_str()).collect();
        let exp_kept: Vec<&str> = g["scope_kept"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        let exp_dropped: Vec<&str> = g["scope_dropped"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert_eq!(kept_paths, exp_kept);
        assert_eq!(dropped_paths, exp_dropped);
    }

    #[test]
    fn safe_inline_matches_python() {
        let g = golden();
        let si = &g["safe_inline"];
        assert_eq!(safe_inline("a`b"), si["backtick"].as_str().unwrap());
        assert_eq!(safe_inline("a\x07b\x00c"), si["control"].as_str().unwrap());
        assert_eq!(safe_inline("a\u{202e}b"), si["bidi"].as_str().unwrap());
        assert_eq!(
            safe_inline("a\u{200b}b\u{feff}c"),
            si["zw"].as_str().unwrap()
        );
    }
}
