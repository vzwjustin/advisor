//! Port of `advisor/pr_comment.py` — render findings as a GitHub-flavored
//! Markdown PR comment (collapsible `<details>` per finding + severity table).
//! Pure string-in, string-out.

use once_cell::sync::Lazy;
use regex::Regex;

use crate::models::Finding;
use crate::sarif::strip_controls;

const SEVERITY_ORDER: [&str; 4] = ["CRITICAL", "HIGH", "MEDIUM", "LOW"];
const GITHUB_BODY_LIMIT: usize = 60_000;
const EVIDENCE_BYTE_CAP: usize = 500;

// `<(/?)details\b`, case-insensitive — defense-in-depth for evidence content.
static DETAILS_TAG_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)<(/?)details\b").expect("details regex is valid"));

/// HTML-escape with `quote=True`, matching Python `html.escape`.
fn escape_html(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for c in text.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            other => out.push(other),
        }
    }
    out
}

/// Escape a field for a `<summary>` element (escape, then `|`→`\|`, newline→space).
fn escape_summary(text: &str) -> String {
    escape_html(text).replace('|', "\\|").replace('\n', " ")
}

/// Escape a field for an inline `<code>` element (escape, then backtick→U+2018).
fn escape_inline_code(text: &str) -> String {
    escape_html(text).replace('`', "\u{2018}")
}

/// Cap a single evidence block at `EVIDENCE_BYTE_CAP` UTF-8 bytes. Mirrors
/// `_cap_evidence` (reserves the 3-byte ellipsis, drops partial code points).
fn cap_evidence(evidence: &str) -> String {
    let encoded = evidence.as_bytes();
    if encoded.len() <= EVIDENCE_BYTE_CAP {
        return evidence.to_string();
    }
    let ellipsis_bytes = "\u{2026}".len(); // 3
    let take = EVIDENCE_BYTE_CAP - ellipsis_bytes;
    // Decode lossily-ignoring any trailing partial code point (like Python's
    // errors="ignore" on a mid-character slice).
    let slice = &encoded[..take];
    let valid = match std::str::from_utf8(slice) {
        Ok(s) => s,
        Err(e) => std::str::from_utf8(&slice[..e.valid_up_to()]).unwrap_or(""),
    };
    format!("{}\u{2026}", valid.trim_end())
}

fn sanitize(f: &Finding) -> Finding {
    Finding {
        file_path: strip_controls(&f.file_path, true),
        severity: strip_controls(&f.severity, true),
        description: strip_controls(&f.description, true),
        evidence: strip_controls(&f.evidence, true),
        fix: strip_controls(&f.fix, true),
        rule_id: f.rule_id.as_ref().map(|r| strip_controls(r, true)),
        expected_vs_actual: strip_controls(&f.expected_vs_actual, true),
    }
}

/// Number of UTF-8 bytes a rendered line contributes (`len(line.encode())+1`).
fn line_bytes(line: &str) -> usize {
    line.len() + 1
}

/// Render findings as a Markdown PR-body block. Mirrors `format_pr_comment`.
pub fn format_pr_comment(findings: &[Finding]) -> String {
    if findings.is_empty() {
        return "## Advisor review\n\n_No findings at the current threshold._\n".to_string();
    }

    let mut findings: Vec<Finding> = findings.iter().map(sanitize).collect();

    // Stable sort by severity rank (unknown sorts last).
    let sev_rank = |sev: &str| {
        SEVERITY_ORDER
            .iter()
            .position(|s| *s == sev.to_uppercase())
            .unwrap_or(SEVERITY_ORDER.len())
    };
    findings.sort_by_key(|f| sev_rank(&f.severity));

    // Per-severity counts (unknown clamped to LOW).
    let mut counts: std::collections::HashMap<&str, usize> =
        SEVERITY_ORDER.iter().map(|s| (*s, 0usize)).collect();
    for f in &findings {
        let up = f.severity.to_uppercase();
        let key = SEVERITY_ORDER
            .iter()
            .find(|s| **s == up)
            .copied()
            .unwrap_or("LOW");
        *counts.get_mut(key).unwrap() += 1;
    }

    let finding_word = if findings.len() == 1 {
        "finding"
    } else {
        "findings"
    };
    let mut lines: Vec<String> = vec![
        "## Advisor review".to_string(),
        String::new(),
        format!("**{} {}**", findings.len(), finding_word),
        String::new(),
        "| Severity | Count |".to_string(),
        "| --- | ---: |".to_string(),
    ];
    for sev in SEVERITY_ORDER {
        lines.push(format!("| {} | {} |", sev, counts[sev]));
    }
    lines.push(String::new());
    lines.push("### Details".to_string());
    lines.push(String::new());

    let mut projected: usize = lines.iter().map(|l| line_bytes(l)).sum();
    let mut rendered_count = 0usize;
    let mut truncated = false;

    for f in &findings {
        // Truncate before HTML-escape (avoid slicing a multi-char entity).
        let title_raw: String = {
            let clipped: String = f.description.chars().take(100).collect();
            if clipped.is_empty() {
                "(no description)".to_string()
            } else {
                clipped
            }
        };
        let title = escape_summary(&title_raw);
        let mut block: Vec<String> = vec![
            format!(
                "<details><summary><strong>[{}]</strong> <code>{}</code> — {}</summary>",
                escape_summary(&f.severity),
                escape_inline_code(&f.file_path),
                title
            ),
            String::new(),
            format!("**Description:** {}", escape_html(&f.description)),
            String::new(),
        ];
        if !f.expected_vs_actual.is_empty() {
            block.push(format!(
                "**Expected → Actual:** {}",
                escape_html(&f.expected_vs_actual)
            ));
            block.push(String::new());
        }
        let evidence_neutralized = {
            let capped = cap_evidence(&f.evidence).replace("```", "'''");
            DETAILS_TAG_RE
                .replace_all(&capped, "&lt;${1}details")
                .into_owned()
        };
        block.push("**Evidence:**".to_string());
        block.push(String::new());
        block.push("```".to_string());
        block.push(evidence_neutralized);
        block.push("```".to_string());
        block.push(String::new());
        block.push(format!("**Fix:** {}", escape_html(&f.fix)));
        if let Some(rule_id) = &f.rule_id {
            if !rule_id.is_empty() {
                block.push(String::new());
                block.push(format!("**Rule:** `{}`", escape_inline_code(rule_id)));
            }
        }
        block.push(String::new());
        block.push("</details>".to_string());
        block.push(String::new());

        let block_bytes: usize = block.iter().map(|l| line_bytes(l)).sum();
        if projected + block_bytes > GITHUB_BODY_LIMIT {
            truncated = true;
            break;
        }
        lines.extend(block);
        projected += block_bytes;
        rendered_count += 1;
    }

    if truncated {
        let omitted = findings.len() - rendered_count;
        let word = if omitted == 1 { "finding" } else { "findings" };
        lines.push(format!(
            "_Output truncated to fit GitHub's body length cap — {omitted} {word} omitted. Run `advisor` locally for the full report._"
        ));
        lines.push(String::new());
    }

    format!("{}\n", lines.join("\n").trim_end())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn golden() -> Value {
        serde_json::from_str(include_str!("../tests/parity/pr_comment.json")).unwrap()
    }

    fn f(
        fp: &str,
        sev: &str,
        desc: &str,
        ev: &str,
        fix: &str,
        rule: Option<&str>,
        eva: &str,
    ) -> Finding {
        Finding {
            file_path: fp.into(),
            severity: sev.into(),
            description: desc.into(),
            evidence: ev.into(),
            fix: fix.into(),
            rule_id: rule.map(|s| s.into()),
            expected_vs_actual: eva.into(),
        }
    }

    #[test]
    fn empty_matches_python() {
        assert_eq!(format_pr_comment(&[]), golden()["empty"].as_str().unwrap());
    }

    #[test]
    fn basic_matches_python() {
        let g = golden();
        let findings = vec![
            f(
                "src/auth.py:42",
                "HIGH",
                "SQL injection in `query`",
                "user||input concatenated",
                "use params",
                None,
                "",
            ),
            f(
                "lib/x.py",
                "LOW",
                "weak hash",
                "md5",
                "sha256",
                Some("advisor/custom/1"),
                "expected sha -> got md5",
            ),
            f("a.py", "CRITICAL", "RCE", "eval(x)", "remove", None, ""),
        ];
        assert_eq!(format_pr_comment(&findings), g["basic"].as_str().unwrap());
    }

    #[test]
    fn unknown_severity_clamped() {
        let g = golden();
        let findings = vec![f("z.py", "INFO", "note", "ev", "fix", None, "")];
        assert_eq!(
            format_pr_comment(&findings),
            g["unknown_sev"].as_str().unwrap()
        );
    }

    #[test]
    fn html_escaping_matches_python() {
        let g = golden();
        let findings = vec![f(
            "x.py",
            "HIGH",
            "<script>alert(1)</script>",
            "</details> sneaky\n```\ncode",
            "fix & stuff",
            None,
            "",
        )];
        assert_eq!(
            format_pr_comment(&findings),
            g["html_escape"].as_str().unwrap()
        );
    }
}
