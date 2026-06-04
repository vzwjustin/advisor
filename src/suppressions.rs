//! Port of `advisor/suppressions.py` — targeted per-rule/per-file false-positive
//! suppressions with optional expiry. JSONL file under `.advisor/`.

use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::fs::{normalize_path, read_text_capped, MAX_ADVISOR_FILE_BYTES};
use crate::rank::{fnmatch_match, try_double_star_regex};
use crate::sarif::synthesize_rule_id;
use crate::Finding;

/// advisor's suppressions schema version.
pub const SCHEMA_VERSION: &str = "1.0";
const HEADER_KEY: &str = "__advisor_suppressions__";
const REQUIRES_EXPIRY_ABOVE: i64 = 2; // > MEDIUM requires `until`

fn severity_rank(sev: &str) -> Option<i64> {
    match sev {
        "LOW" => Some(1),
        "MEDIUM" => Some(2),
        "HIGH" => Some(3),
        "CRITICAL" => Some(4),
        _ => None,
    }
}

/// A single suppression entry. Mirrors `Suppression`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Suppression {
    pub rule_id: String,
    pub reason: String,
    pub file: Option<String>,
    pub file_glob: Option<String>,
    pub until: Option<String>,
    pub expired: bool,
}

impl Suppression {
    /// Whether this suppression matches a finding's path + rule id. Mirrors
    /// `Suppression.matches`.
    pub fn matches(&self, finding_path: &str, finding_rule_id: &str) -> bool {
        if finding_rule_id.to_uppercase() != self.rule_id.to_uppercase() {
            return false;
        }
        let np = normalize_path(finding_path);
        if let Some(file) = &self.file {
            if np != normalize_path(file) {
                return false;
            }
        }
        if let Some(glob) = &self.file_glob {
            if !matches_glob(&np, &normalize_glob_pattern(glob)) {
                return false;
            }
        }
        true
    }
}

/// Minimal-transform glob normalization (BOM strip + backslash→slash); does NOT
/// strip `:line` suffixes. Mirrors `_normalize_glob_pattern`.
fn normalize_glob_pattern(pattern: &str) -> String {
    pattern.trim_start_matches('\u{FEFF}').replace('\\', "/")
}

/// Match `file_path` against `pattern` with `**`-aware semantics; falls back to
/// fnmatch on a malformed pattern. Mirrors `_matches_glob`.
fn matches_glob(file_path: &str, pattern: &str) -> bool {
    match try_double_star_regex(pattern) {
        Some(re) => re.is_match(file_path),
        None => fnmatch_match(file_path, pattern),
    }
}

/// Extract severity rank from a rule id. Mirrors `_severity_from_rule_id`
/// (returns `Err` for a malformed advisor-namespace severity segment).
pub fn severity_from_rule_id(rule_id: &str) -> Result<i64, String> {
    let parts: Vec<&str> = rule_id.split('/').collect();
    if !parts.is_empty() && parts[0].to_uppercase() == "ADVISOR" && parts.len() >= 2 {
        let seg = parts[1].to_uppercase();
        return severity_rank(&seg).ok_or_else(|| {
            format!(
                "rule_id {rule_id:?} has malformed advisor-namespace severity segment {:?}; expected one of [\"CRITICAL\", \"HIGH\", \"LOW\", \"MEDIUM\"]",
                parts[1]
            )
        });
    }
    if !parts.is_empty() {
        if let Some(r) = severity_rank(&parts[0].to_uppercase()) {
            return Ok(r);
        }
    }
    Ok(REQUIRES_EXPIRY_ABOVE)
}

// ── date helpers (UTC) ─────────────────────────────────────────────

fn is_leap(y: i64) -> bool {
    (y % 4 == 0 && y % 100 != 0) || y % 400 == 0
}

fn days_in_month(y: i64, m: i64) -> i64 {
    match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if is_leap(y) {
                29
            } else {
                28
            }
        }
        _ => 0,
    }
}

/// Days since 1970-01-01 for a proleptic-Gregorian date (Howard Hinnant's algorithm).
fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

fn today_utc_days() -> i64 {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    (secs / 86400) as i64
}

/// Parse an `until` date (`YYYY-MM-DD`), returning `(normalized, expired)` or an
/// error on bad shape. Mirrors `_parse_until`.
fn parse_until(raw: Option<&str>, context: &str) -> Result<(Option<String>, bool), String> {
    let raw = match raw {
        None | Some("") => return Ok((None, false)),
        Some(s) => s,
    };
    // Date-only shape: ^\d{4}-\d{2}-\d{2}$
    let bytes = raw.as_bytes();
    let shape_ok = bytes.len() == 10
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes[..4].iter().all(|b| b.is_ascii_digit())
        && bytes[5..7].iter().all(|b| b.is_ascii_digit())
        && bytes[8..10].iter().all(|b| b.is_ascii_digit());
    if !shape_ok {
        return Err(format!(
            "{context}: invalid until={raw:?}: expected YYYY-MM-DD (date only, no time/tz)"
        ));
    }
    let y: i64 = raw[..4].parse().unwrap_or(0);
    let m: i64 = raw[5..7].parse().unwrap_or(0);
    let d: i64 = raw[8..10].parse().unwrap_or(0);
    if !(1..=12).contains(&m) || d < 1 || d > days_in_month(y, m) {
        return Err(format!("{context}: invalid until={raw:?}: not a real date"));
    }
    let expired = days_from_civil(y, m, d) < today_utc_days();
    Ok((Some(raw.to_string()), expired))
}

/// Load suppressions from a JSONL file. Missing file → empty. Malformed JSON
/// lines are skipped; structural misuse returns `Err`. Mirrors `load_suppressions`.
pub fn load_suppressions(path: &Path) -> Result<Vec<Suppression>, String> {
    let text = match read_text_capped(path, MAX_ADVISOR_FILE_BYTES) {
        Ok(t) => t,
        Err(crate::fs::ReadCappedError::NotFound) => return Ok(Vec::new()),
        Err(e) => return Err(format!("could not read {}: {e}", path.display())),
    };

    let mut entries = Vec::new();
    for (i, raw_line) in text.lines().enumerate() {
        let line_no = i + 1;
        let stripped = raw_line.trim();
        if stripped.is_empty() || stripped.starts_with('#') {
            continue;
        }
        let obj: serde_json::Value = match serde_json::from_str(stripped) {
            Ok(v) => v,
            Err(err) => return Err(format!("{}:{}: invalid JSON — {err}", path.display(), line_no)),
        };
        let Some(map) = obj.as_object() else { continue };
        if map
            .get(HEADER_KEY)
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
        {
            continue;
        }
        let ctx = format!("{}:{}", path.display(), line_no);
        let rule_id = map.get("rule_id").and_then(|v| v.as_str());
        let reason = map.get("reason").and_then(|v| v.as_str()).unwrap_or("");
        let file_ = map
            .get("file")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty());
        let file_glob = map
            .get("file_glob")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty());
        let until_raw = map.get("until").and_then(|v| v.as_str());

        let rule_id = match rule_id {
            Some(r) if !r.trim().is_empty() => r,
            _ => return Err(format!("{ctx}: missing required 'rule_id'")),
        };
        if file_.is_some() && file_glob.is_some() {
            return Err(format!(
                "{ctx}: 'file' and 'file_glob' are mutually exclusive"
            ));
        }
        if file_.is_none() && file_glob.is_none() {
            return Err(format!("{ctx}: one of 'file' or 'file_glob' is required"));
        }
        if let Some(glob) = file_glob {
            if try_double_star_regex(glob).is_none() {
                return Err(format!("{ctx}: invalid file_glob {glob:?}"));
            }
        }

        let (until_norm, expired) = parse_until(until_raw, &ctx)?;

        let sev_rank = severity_from_rule_id(rule_id).map_err(|e| format!("{ctx}: {e}"))?;
        if sev_rank > REQUIRES_EXPIRY_ABOVE {
            // Python `!r` uses single quotes around the rule id.
            if until_norm.is_none() {
                return Err(format!(
                    "{ctx}: rule '{rule_id}' is above MEDIUM — 'until' date is required"
                ));
            }
            if reason.trim().is_empty() {
                return Err(format!(
                    "{ctx}: rule '{rule_id}' is above MEDIUM — non-empty 'reason' is required"
                ));
            }
        }

        entries.push(Suppression {
            rule_id: rule_id.to_string(),
            reason: reason.to_string(),
            file: file_.map(|s| s.to_string()),
            file_glob: file_glob.map(|s| s.to_string()),
            until: until_norm,
            expired,
        });
    }
    Ok(entries)
}

/// Drop findings matching an active (non-expired) suppression. Returns
/// `(kept, dropped_pairs)`. Mirrors `apply_suppressions`.
pub fn apply_suppressions(
    findings: &[Finding],
    suppressions: &[Suppression],
) -> (Vec<Finding>, Vec<(Finding, Suppression)>) {
    let active: Vec<&Suppression> = suppressions.iter().filter(|s| !s.expired).collect();
    let mut kept = Vec::new();
    let mut dropped = Vec::new();
    for f in findings {
        let rid = match &f.rule_id {
            Some(r) if !r.is_empty() => r.clone(),
            _ => synthesize_rule_id(&f.severity, &f.description, "advisor"),
        };
        match active.iter().find(|s| s.matches(&f.file_path, &rid)) {
            Some(s) => dropped.push((f.clone(), (*s).clone())),
            None => kept.push(f.clone()),
        }
    }
    (kept, dropped)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn golden() -> Value {
        serde_json::from_str(include_str!("../tests/parity/suppressions.json")).unwrap()
    }

    #[test]
    fn severity_from_rule_id_matches_python() {
        let g = golden();
        for (rid, exp) in g["severity_from_rule_id"].as_object().unwrap() {
            assert_eq!(
                severity_from_rule_id(rid).unwrap(),
                exp.as_i64().unwrap(),
                "rid={rid}"
            );
        }
    }

    #[test]
    fn matches_match_python() {
        let g = golden();
        let m = &g["matches"];
        let s_exact = Suppression {
            rule_id: "advisor/low/a".into(),
            reason: "r".into(),
            file: Some("src/auth.py".into()),
            file_glob: None,
            until: None,
            expired: false,
        };
        let s_glob = Suppression {
            rule_id: "advisor/low/b".into(),
            reason: "r".into(),
            file: None,
            file_glob: Some("tests/**/*.py".into()),
            until: None,
            expired: false,
        };
        assert_eq!(
            s_exact.matches("src/auth.py:42", "advisor/low/a"),
            m["exact_hit"].as_bool().unwrap()
        );
        assert_eq!(
            s_exact.matches("src/other.py", "advisor/low/a"),
            m["exact_miss_path"].as_bool().unwrap()
        );
        assert_eq!(
            s_exact.matches("src/auth.py", "ADVISOR/LOW/A"),
            m["rule_case_insensitive"].as_bool().unwrap()
        );
        assert_eq!(
            s_glob.matches("tests/unit/test_x.py", "advisor/low/b"),
            m["glob_hit"].as_bool().unwrap()
        );
        assert_eq!(
            s_glob.matches("tests/test_x.py", "advisor/low/b"),
            m["glob_miss_shallow"].as_bool().unwrap()
        );
        assert_eq!(
            s_glob.matches("src/x.py", "advisor/low/b"),
            m["glob_miss_outside"].as_bool().unwrap()
        );
    }

    #[test]
    fn apply_matches_python() {
        let g = golden();
        let s_exact = Suppression {
            rule_id: "advisor/low/a".into(),
            reason: "r".into(),
            file: Some("src/auth.py".into()),
            file_glob: None,
            until: None,
            expired: false,
        };
        let findings = vec![
            Finding {
                file_path: "src/auth.py:1".into(),
                severity: "LOW".into(),
                description: "weak".into(),
                evidence: "e".into(),
                fix: "x".into(),
                rule_id: Some("advisor/low/a".into()),
                expected_vs_actual: String::new(),
            },
            Finding {
                file_path: "src/keep.py:1".into(),
                severity: "LOW".into(),
                description: "ok".into(),
                evidence: "e".into(),
                fix: "x".into(),
                rule_id: Some("advisor/low/z".into()),
                expected_vs_actual: String::new(),
            },
        ];
        let (kept, dropped) = apply_suppressions(&findings, &[s_exact]);
        let kp: Vec<String> = kept.iter().map(|f| f.file_path.clone()).collect();
        let dp: Vec<String> = dropped.iter().map(|(f, _)| f.file_path.clone()).collect();
        let exp = |k: &str| {
            g[k].as_array()
                .unwrap()
                .iter()
                .map(|v| v.as_str().unwrap().to_string())
                .collect::<Vec<_>>()
        };
        assert_eq!(kp, exp("apply_kept"));
        assert_eq!(dp, exp("apply_dropped"));
    }

    #[test]
    fn load_valid_matches_python() {
        let g = golden();
        let dir = std::env::temp_dir().join(format!("advisor_supp_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("s.jsonl");
        std::fs::write(
            &path,
            "{\"__advisor_suppressions__\": true, \"schema_version\": \"1.0\"}\n\
             {\"rule_id\": \"advisor/low/a\", \"file\": \"src/auth.py\", \"reason\": \"ok\"}\n\
             {\"rule_id\": \"advisor/high/b\", \"file_glob\": \"legacy/**\", \"reason\": \"rewrite\", \"until\": \"2999-01-01\"}\n\
             {\"rule_id\": \"advisor/critical/c\", \"file\": \"x.py\", \"reason\": \"old\", \"until\": \"2000-01-01\"}\n",
        )
        .unwrap();
        let loaded = load_suppressions(&path).unwrap();
        let got: Vec<Value> = loaded
            .iter()
            .map(|s| {
                serde_json::json!({
                    "rule_id": s.rule_id, "file": s.file, "file_glob": s.file_glob,
                    "until": s.until, "expired": s.expired, "reason": s.reason,
                })
            })
            .collect();
        assert_eq!(Value::Array(got), g["load_valid"]);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn load_errors_match_python() {
        let g = golden();
        let dir = std::env::temp_dir().join(format!("advisor_supp_err_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let run = |lines: &str| -> String {
            let path = dir.join("e.jsonl");
            std::fs::write(&path, lines).unwrap();
            let err = load_suppressions(&path).unwrap_err();
            // Strip the "path:line: " context prefix, like the golden generator.
            err.split_once(": ").map(|x| x.1.to_string()).unwrap_or(err)
        };
        assert_eq!(
            run("{\"file\": \"x.py\", \"reason\": \"r\"}\n"),
            g["err_missing_rule"].as_str().unwrap()
        );
        assert_eq!(run("{\"rule_id\":\"advisor/low/a\",\"file\":\"x.py\",\"file_glob\":\"y/**\",\"reason\":\"r\"}\n"), g["err_both"].as_str().unwrap());
        assert_eq!(
            run("{\"rule_id\":\"advisor/low/a\",\"reason\":\"r\"}\n"),
            g["err_no_target"].as_str().unwrap()
        );
        assert_eq!(
            run("{\"rule_id\":\"advisor/high/a\",\"file\":\"x.py\",\"reason\":\"r\"}\n"),
            g["err_high_no_until"].as_str().unwrap()
        );
        let _ = std::fs::remove_dir_all(&dir);
    }
}
