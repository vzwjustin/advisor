//! Port of `advisor/baseline.py` — accept-current / flag-new findings lifecycle.
//!
//! A baseline is a JSONL file (header record + one identity per line). Identity
//! key is `(normalized_path, rule_id_or_synthesized, description_hash)`.

use std::collections::HashMap;
use std::path::Path;

use sha1::{Digest, Sha1};

use crate::fs::{atomic_write_text, posix_normpath, read_text_capped, MAX_ADVISOR_FILE_BYTES};
use crate::jsonutil::ensure_ascii;
use crate::models::Finding;
use crate::sarif::synthesize_rule_id;

/// advisor's baseline emitter schema version.
pub const SCHEMA_VERSION: &str = "1.0";
const HEADER_KEY: &str = "__advisor_baseline__";

/// A single captured finding identity. Mirrors `BaselineEntry`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BaselineEntry {
    pub file_path: String,
    pub rule_id: String,
    pub description_hash: String,
    pub severity: String,
    pub description: String,
}

impl BaselineEntry {
    /// Identity key `(normalized_path, rule_id, description_hash)`.
    pub fn key(&self) -> (String, String, String) {
        (
            normalize_identity_path(&self.file_path),
            self.rule_id.clone(),
            self.description_hash.clone(),
        )
    }
}

/// Normalize superficial path spelling while preserving the `:line` suffix.
/// Mirrors `_normalize_identity_path`.
pub fn normalize_identity_path(path: &str) -> String {
    let mut n = path.trim().trim_matches('`').trim().replace('\\', "/");
    if !n.is_empty() && n != "." {
        // Collapse a leading run of 2+ slashes to a single slash (re.sub ^/{2,}).
        let leading = n.len() - n.trim_start_matches('/').len();
        if leading >= 2 {
            n = format!("/{}", &n[leading..]);
        }
        let collapsed = posix_normpath(&n);
        n = if collapsed == "." {
            String::new()
        } else {
            collapsed
        };
    }
    while let Some(rest) = n.strip_prefix("./") {
        n = rest.to_string();
    }
    n
}

fn rule_id_for(f: &Finding) -> String {
    match &f.rule_id {
        Some(r) if !r.is_empty() => r.clone(),
        _ => synthesize_rule_id(&f.severity, &f.description, "advisor"),
    }
}

/// Stable short hash of the whitespace-normalized first 120 chars of a
/// description (first 16 hex of SHA-1). Mirrors `_description_hash`.
pub fn description_hash(description: &str) -> String {
    let first120: String = description.chars().take(120).collect();
    let normalized = first120.split_whitespace().collect::<Vec<_>>().join(" ");
    let mut hasher = Sha1::new();
    hasher.update(normalized.as_bytes());
    let digest = hasher.finalize();
    let mut head = [0u8; 8];
    head.copy_from_slice(&digest[..8]);
    format!("{:016x}", u64::from_be_bytes(head))
}

fn finding_key(f: &Finding) -> (String, String, String) {
    (
        normalize_identity_path(&f.file_path),
        rule_id_for(f),
        description_hash(&f.description),
    )
}

/// Convert confirmed findings into baseline identity entries. Mirrors
/// `findings_to_entries`.
pub fn findings_to_entries(findings: &[Finding]) -> Vec<BaselineEntry> {
    findings
        .iter()
        .map(|f| BaselineEntry {
            file_path: f.file_path.clone(),
            rule_id: rule_id_for(f),
            description_hash: description_hash(&f.description),
            severity: f.severity.clone(),
            description: f.description.chars().take(120).collect(),
        })
        .collect()
}

/// Minimal JSON string encoder matching CPython `json.dumps` (`ensure_ascii`).
fn json_str(s: &str) -> String {
    let mut escaped = String::with_capacity(s.len() + 2);
    escaped.push('"');
    for ch in s.chars() {
        match ch {
            '"' => escaped.push_str("\\\""),
            '\\' => escaped.push_str("\\\\"),
            '\n' => escaped.push_str("\\n"),
            '\r' => escaped.push_str("\\r"),
            '\t' => escaped.push_str("\\t"),
            c if (c as u32) < 0x20 => escaped.push_str(&format!("\\u{:04x}", c as u32)),
            c => escaped.push(c),
        }
    }
    escaped.push('"');
    ensure_ascii(&escaped)
}

/// Write entries to a baseline JSONL file, sorted by identity key for stable
/// byte output. Mirrors `write_baseline` (Python `json.dumps` default `, `/`: `
/// separators).
pub fn write_baseline(path: &Path, entries: &[BaselineEntry]) -> std::io::Result<()> {
    let mut lines: Vec<String> = vec![format!(
        "{{\"{HEADER_KEY}\": true, \"schema_version\": \"{SCHEMA_VERSION}\", \"count\": {}}}",
        entries.len()
    )];
    let mut sorted: Vec<&BaselineEntry> = entries.iter().collect();
    sorted.sort_by_key(|e| e.key());
    for e in sorted {
        lines.push(format!(
            "{{\"file_path\": {}, \"rule_id\": {}, \"description_hash\": {}, \"severity\": {}, \"description\": {}}}",
            json_str(&e.file_path),
            json_str(&e.rule_id),
            json_str(&e.description_hash),
            json_str(&e.severity),
            json_str(&e.description)
        ));
    }
    atomic_write_text(path, &(lines.join("\n") + "\n"))
}

/// Read entries from a baseline JSONL file. Missing file → empty list; malformed
/// lines skipped. Mirrors `read_baseline`.
pub fn read_baseline(path: &Path) -> Vec<BaselineEntry> {
    let text = match read_text_capped(path, MAX_ADVISOR_FILE_BYTES) {
        Ok(t) => t,
        Err(_) => return Vec::new(),
    };
    let mut entries = Vec::new();
    for line in text.lines() {
        if line.trim().is_empty() {
            continue;
        }
        let obj: serde_json::Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let Some(map) = obj.as_object() else { continue };
        if map
            .get(HEADER_KEY)
            .and_then(|v| v.as_bool())
            .unwrap_or(false)
        {
            continue; // header record
        }
        let get_str = |k: &str| map.get(k).and_then(|v| v.as_str()).map(|s| s.to_string());
        match (
            get_str("file_path"),
            get_str("rule_id"),
            get_str("description_hash"),
        ) {
            (Some(file_path), Some(rule_id), Some(description_hash)) => {
                entries.push(BaselineEntry {
                    file_path,
                    rule_id,
                    description_hash,
                    severity: get_str("severity").unwrap_or_default(),
                    description: get_str("description").unwrap_or_default(),
                })
            }
            _ => continue,
        }
    }
    entries
}

/// `(rule_id, description_hash)` → normalized baseline paths, for abs/rel suffix
/// aliasing. Mirrors `_baseline_path_index`.
fn baseline_path_index(baseline: &[BaselineEntry]) -> HashMap<(&str, &str), Vec<String>> {
    let mut idx: HashMap<(&str, &str), Vec<String>> = HashMap::new();
    for e in baseline {
        idx.entry((e.rule_id.as_str(), e.description_hash.as_str()))
            .or_default()
            .push(normalize_identity_path(&e.file_path));
    }
    idx
}

/// Match absolute vs repo-relative spellings of the same path. Bare filenames
/// (no `/`) never alias — `auth.py` must not suppress `src/auth.py`.
fn suffix_alias_match(path_norm: &str, baseline_paths: &[String]) -> bool {
    baseline_paths.iter().any(|bp| {
        if path_norm == bp {
            return true;
        }
        if path_norm.contains('/') && bp.ends_with(&format!("/{path_norm}")) {
            return true;
        }
        if bp.contains('/') && path_norm.ends_with(&format!("/{bp}")) {
            return true;
        }
        false
    })
}

/// Partition `findings` into `(new, suppressed_by_baseline)`. Mirrors
/// `filter_against_baseline`.
pub fn filter_against_baseline(
    findings: &[Finding],
    baseline: &[BaselineEntry],
) -> (Vec<Finding>, Vec<Finding>) {
    let baseline_keys: std::collections::HashSet<(String, String, String)> =
        baseline.iter().map(|e| e.key()).collect();
    let path_idx = baseline_path_index(baseline);
    let mut new_findings = Vec::new();
    let mut suppressed = Vec::new();
    for f in findings {
        let key = finding_key(f);
        let empty = Vec::new();
        let candidates = path_idx
            .get(&(key.1.as_str(), key.2.as_str()))
            .unwrap_or(&empty);
        if baseline_keys.contains(&key) || suffix_alias_match(&key.0, candidates) {
            suppressed.push(f.clone());
        } else {
            new_findings.push(f.clone());
        }
    }
    (new_findings, suppressed)
}

/// Partitioning of current findings against a baseline. Mirrors `BaselineDiff`
/// (`baseline_only` is an alias of `fixed`).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct BaselineDiff {
    pub new: Vec<Finding>,
    pub persisting: Vec<Finding>,
    pub fixed: Vec<BaselineEntry>,
}

/// Compare current `findings` to a saved `baseline`. Mirrors `diff_against_baseline`.
pub fn diff_against_baseline(findings: &[Finding], baseline: &[BaselineEntry]) -> BaselineDiff {
    let baseline_by_key: std::collections::HashSet<(String, String, String)> =
        baseline.iter().map(|e| e.key()).collect();
    let path_idx = baseline_path_index(baseline);
    let mut matched: std::collections::HashSet<(String, String, String)> =
        std::collections::HashSet::new();
    let mut new_findings = Vec::new();
    let mut persisting = Vec::new();
    for f in findings {
        let key = finding_key(f);
        if baseline_by_key.contains(&key) {
            matched.insert(key.clone());
            persisting.push(f.clone());
        } else {
            let empty = Vec::new();
            let candidates = path_idx
                .get(&(key.1.as_str(), key.2.as_str()))
                .unwrap_or(&empty);
            if suffix_alias_match(&key.0, candidates) {
                for e in baseline {
                    if e.rule_id == key.1 && e.description_hash == key.2 {
                        let bp = normalize_identity_path(&e.file_path);
                        if bp.ends_with(&format!("/{}", key.0))
                            || key.0.ends_with(&format!("/{bp}"))
                        {
                            matched.insert(e.key());
                        }
                    }
                }
                persisting.push(f.clone());
            } else {
                new_findings.push(f.clone());
            }
        }
    }
    let fixed: Vec<BaselineEntry> = baseline
        .iter()
        .filter(|e| !matched.contains(&e.key()))
        .cloned()
        .collect();
    BaselineDiff {
        new: new_findings,
        persisting,
        fixed,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn golden() -> Value {
        serde_json::from_str(include_str!("../tests/parity/baseline.json")).unwrap()
    }

    fn f(fp: &str, sev: &str, desc: &str, rule: Option<&str>) -> Finding {
        Finding {
            file_path: fp.into(),
            severity: sev.into(),
            description: desc.into(),
            evidence: "e".into(),
            fix: "x".into(),
            rule_id: rule.map(|s| s.into()),
            expected_vs_actual: String::new(),
        }
    }

    fn base_findings() -> Vec<Finding> {
        vec![
            f("src/auth.py:42", "HIGH", "SQL injection in login", None),
            f(
                "lib/x.py",
                "LOW",
                "weak md5 hash used",
                Some("advisor/custom/1"),
            ),
        ]
    }

    #[test]
    fn entries_and_hashes_match_python() {
        let g = golden();
        let entries = findings_to_entries(&base_findings());
        let got: Vec<Value> = entries
            .iter()
            .map(|e| {
                serde_json::json!({
                    "file_path": e.file_path, "rule_id": e.rule_id,
                    "description_hash": e.description_hash, "severity": e.severity,
                    "description": e.description,
                })
            })
            .collect();
        assert_eq!(Value::Array(got), g["entries"]);

        let dh = &g["description_hash"];
        assert_eq!(
            description_hash("SQL injection in login"),
            dh["a"].as_str().unwrap()
        );
        assert_eq!(
            description_hash("  SQL   injection\n in login  "),
            dh["ws"].as_str().unwrap()
        );

        for (p, exp) in g["normalize_identity_path"].as_object().unwrap() {
            assert_eq!(&normalize_identity_path(p), exp.as_str().unwrap(), "p={p}");
        }
    }

    #[test]
    fn write_read_roundtrip_matches_python() {
        let g = golden();
        let dir =
            std::env::temp_dir().join(format!("advisor_baseline_test_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("baseline.jsonl");
        write_baseline(&path, &findings_to_entries(&base_findings())).unwrap();
        let written = std::fs::read_to_string(&path).unwrap();
        assert_eq!(written, g["written"].as_str().unwrap());
        assert_eq!(
            read_baseline(&path).len() as u64,
            g["roundtrip_count"].as_u64().unwrap()
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn bare_filename_does_not_alias_directory_path() {
        let baseline = findings_to_entries(&[f("src/auth.py:42", "HIGH", "SQL injection", None)]);
        let findings = vec![f("auth.py:42", "HIGH", "SQL injection", None)];
        let (new, suppressed) = filter_against_baseline(&findings, &baseline);
        assert_eq!(new.len(), 1);
        assert!(suppressed.is_empty());
        let diff = diff_against_baseline(&findings, &baseline);
        assert_eq!(diff.new.len(), 1);
        assert!(diff.persisting.is_empty());
    }

    #[test]
    fn absolute_path_aliases_relative_baseline() {
        let baseline = findings_to_entries(&[f("src/auth.py:10", "HIGH", "issue", None)]);
        let findings = vec![f(
            "/home/runner/work/repo/src/auth.py:10",
            "HIGH",
            "issue",
            None,
        )];
        let (new, suppressed) = filter_against_baseline(&findings, &baseline);
        assert!(new.is_empty());
        assert_eq!(suppressed.len(), 1);
    }

    #[test]
    fn filter_and_diff_match_python() {
        let g = golden();
        let entries = findings_to_entries(&base_findings());
        let mut findings2 = base_findings();
        findings2.push(f("new.py:1", "MEDIUM", "brand new finding", None));
        let (new, suppressed) = filter_against_baseline(&findings2, &entries);
        let paths = |v: &[Finding]| v.iter().map(|x| x.file_path.clone()).collect::<Vec<_>>();
        let exp = |k: &str| {
            g[k].as_array()
                .unwrap()
                .iter()
                .map(|x| x.as_str().unwrap().to_string())
                .collect::<Vec<_>>()
        };
        assert_eq!(paths(&new), exp("filter_new"));
        assert_eq!(paths(&suppressed), exp("filter_suppressed"));

        let diff = diff_against_baseline(&findings2, &entries);
        assert_eq!(paths(&diff.new), exp("diff_new"));
        assert_eq!(paths(&diff.persisting), exp("diff_persisting"));
        assert_eq!(
            diff.fixed
                .iter()
                .map(|e| e.file_path.clone())
                .collect::<Vec<_>>(),
            exp("diff_fixed")
        );
    }
}
