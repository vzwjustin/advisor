//! Port of `advisor/history.py` — persistent JSONL log of confirmed findings,
//! plus repeat-offender decay scoring that boosts the ranker.
//!
//! Time-dependent functions take `now` as epoch seconds (`None` = real now) so
//! tests are deterministic. The advisory file lock and oversized-line/doubling
//! reader optimizations are simplified here (documented in PORT_NOTES); the
//! observable results match for realistic append-only history files.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

use crate::fence::fence;
use crate::fs::normalize_path;

pub const HISTORY_DIR_NAME: &str = ".advisor";
pub const HISTORY_FILE_NAME: &str = "history.jsonl";
pub const HISTORY_SCHEMA_VERSION: &str = "1.0";

const MAX_FILE_SCORE: f64 = 10.0;

fn severity_weight(sev: &str) -> Option<f64> {
    match sev {
        "CRITICAL" => Some(4.0),
        "HIGH" => Some(2.5),
        "MEDIUM" => Some(1.5),
        "LOW" => Some(1.0),
        _ => None,
    }
}

fn is_allowed_severity(s: &str) -> bool {
    matches!(s, "CRITICAL" | "HIGH" | "MEDIUM" | "LOW")
}
fn is_allowed_status(s: &str) -> bool {
    matches!(s, "CONFIRMED" | "FIXED" | "REJECTED")
}

/// A single recorded finding from a past run. Mirrors `HistoryEntry`; field
/// order matches the dataclass so `to_json_line` mirrors `asdict`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct HistoryEntry {
    pub timestamp: String,
    pub file_path: String,
    pub severity: String,
    pub description: String,
    pub status: String,
    pub run_id: String,
    pub schema_version: String,
}

impl HistoryEntry {
    /// `json.dumps(asdict(self), ensure_ascii=False)` — raw UTF-8, declaration
    /// order, and Python's *default* `, `/`: ` separators (with spaces).
    pub fn to_json_line(&self) -> String {
        // Value::String(..).to_string() yields a properly-escaped JSON string
        // with non-ASCII left raw (matching ensure_ascii=False).
        let q = |s: &str| Value::String(s.to_string()).to_string();
        format!(
            "{{\"timestamp\": {}, \"file_path\": {}, \"severity\": {}, \"description\": {}, \"status\": {}, \"run_id\": {}, \"schema_version\": {}}}",
            q(&self.timestamp),
            q(&self.file_path),
            q(&self.severity),
            q(&self.description),
            q(&self.status),
            q(&self.run_id),
            q(&self.schema_version),
        )
    }
}

/// Absolute path to `<target>/.advisor/history.jsonl`. Mirrors `history_path`.
pub fn history_path(target: &Path) -> PathBuf {
    target.join(HISTORY_DIR_NAME).join(HISTORY_FILE_NAME)
}

/// Append entries to the history file (best-effort; no fsync, lock simplified).
/// Mirrors `append_entries`.
pub fn append_entries(target: &Path, entries: &[HistoryEntry]) -> std::io::Result<PathBuf> {
    let path = history_path(target);
    if entries.is_empty() {
        return Ok(path);
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let payload: String = entries
        .iter()
        .map(|e| format!("{}\n", e.to_json_line()))
        .collect();
    use std::io::Write;
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)?;
    f.write_all(payload.as_bytes())?;
    Ok(path)
}

/// Read the last `limit` entries newest-first. Mirrors `load_recent_findings`
/// (reads all lines; for append-only history the newest-by-timestamp set
/// matches Python's tail-window behavior).
pub fn load_recent_findings(history_file: &Path, limit: usize) -> Vec<HistoryEntry> {
    if limit == 0 || !history_file.exists() {
        return Vec::new();
    }
    let text = match std::fs::read(history_file) {
        Ok(b) => String::from_utf8_lossy(&b).into_owned(),
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => {
            eprintln!("warning: history unreadable: {e}");
            return Vec::new();
        }
        Err(_) => return Vec::new(),
    };
    let mut entries: Vec<HistoryEntry> = Vec::new();
    for line in text.lines() {
        let stripped = line.trim();
        if stripped.is_empty() || stripped.len() > 65536 {
            continue;
        }
        let obj: Value = match serde_json::from_str(stripped) {
            Ok(v) => v,
            Err(_) => continue,
        };
        let Some(map) = obj.as_object() else { continue };
        let req = |k: &str| map.get(k).and_then(|v| v.as_str());
        let (
            Some(timestamp),
            Some(file_path),
            Some(severity),
            Some(description),
            Some(status),
            Some(run_id),
        ) = (
            req("timestamp"),
            req("file_path"),
            req("severity"),
            req("description"),
            req("status"),
            req("run_id"),
        )
        else {
            continue;
        };
        let sev_up = severity.to_uppercase();
        let st_up = status.to_uppercase();
        let sev = if is_allowed_severity(&sev_up) {
            sev_up
        } else {
            "UNKNOWN".to_string()
        };
        let st = if is_allowed_status(&st_up) {
            st_up
        } else {
            "UNKNOWN".to_string()
        };
        entries.push(HistoryEntry {
            timestamp: timestamp.to_string(),
            file_path: file_path.to_string(),
            severity: sev,
            description: description.to_string(),
            status: st,
            run_id: run_id.to_string(),
            schema_version: map
                .get("schema_version")
                .and_then(|v| v.as_str())
                .unwrap_or(HISTORY_SCHEMA_VERSION)
                .to_string(),
        });
    }
    // Newest-first: reverse (to make appended-last first on ties) then stable
    // sort by timestamp descending.
    entries.reverse();
    entries.sort_by(|a, b| b.timestamp.cmp(&a.timestamp));
    entries.truncate(limit);
    entries
}

/// Thin wrapper resolving `<target>/.advisor/history.jsonl`. Mirrors `load_recent`.
pub fn load_recent(target: &Path, limit: usize) -> Vec<HistoryEntry> {
    load_recent_findings(&history_path(target), limit)
}

fn now_epoch() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Parse an ISO-8601 timestamp to epoch seconds. Handles `YYYY-MM-DDTHH:MM:SS`
/// with optional fractional seconds and optional offset (`+HH:MM`/`-HH:MM`/`Z`/
/// none → UTC). Returns `None` if unparseable.
fn parse_iso_epoch(ts: &str) -> Option<i64> {
    let (date, rest) = ts.split_once('T')?;
    let dparts: Vec<&str> = date.split('-').collect();
    if dparts.len() != 3 {
        return None;
    }
    let y: i64 = dparts[0].parse().ok()?;
    let mo: i64 = dparts[1].parse().ok()?;
    let d: i64 = dparts[2].parse().ok()?;

    // Split off an offset suffix.
    let (time_part, offset_secs): (&str, i64) = if let Some(t) = rest.strip_suffix('Z') {
        (t, 0)
    } else if let Some(idx) = rest.rfind(['+', '-']) {
        // Only treat as offset if it looks like +HH:MM / -HH:MM and is after the seconds.
        let (t, off) = rest.split_at(idx);
        let sign = if off.starts_with('-') { -1 } else { 1 };
        let off = &off[1..];
        let op: Vec<&str> = off.split(':').collect();
        if op.len() == 2 {
            let oh: i64 = op[0].parse().ok()?;
            let om: i64 = op[1].parse().ok()?;
            (t, sign * (oh * 3600 + om * 60))
        } else {
            (rest, 0)
        }
    } else {
        (rest, 0)
    };

    // time_part = HH:MM:SS[.fff]
    let time_main = time_part.split('.').next().unwrap_or(time_part);
    let tparts: Vec<&str> = time_main.split(':').collect();
    if tparts.len() != 3 {
        return None;
    }
    let h: i64 = tparts[0].parse().ok()?;
    let mi: i64 = tparts[1].parse().ok()?;
    let s: i64 = tparts[2].parse().ok()?;

    let days = days_from_civil(y, mo, d);
    Some(days * 86400 + h * 3600 + mi * 60 + s - offset_secs)
}

fn days_from_civil(y: i64, m: i64, d: i64) -> i64 {
    let y = if m <= 2 { y - 1 } else { y };
    let era = if y >= 0 { y } else { y - 399 } / 400;
    let yoe = y - era * 400;
    let doy = (153 * (if m > 2 { m - 3 } else { m + 9 }) + 2) / 5 + d - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146097 + doe - 719468
}

/// Age of an entry in days. Unparseable / far-future → +inf. Mirrors `_age_days`.
fn age_days(entry: &HistoryEntry, now: i64) -> f64 {
    match parse_iso_epoch(&entry.timestamp) {
        None => f64::INFINITY,
        Some(ts) => {
            let delta = (now - ts) as f64;
            if delta < -60.0 {
                f64::INFINITY
            } else {
                (delta / 86400.0).max(0.0)
            }
        }
    }
}

fn key_for(file_path: &str) -> String {
    let n = normalize_path(file_path);
    if n.is_empty() {
        file_path.to_string()
    } else {
        n
    }
}

/// Per-file CONFIRMED count within `window_days`. Mirrors `file_repeat_counts`.
pub fn file_repeat_counts(
    findings: &[HistoryEntry],
    window_days: f64,
    now: Option<i64>,
) -> HashMap<String, i64> {
    let now = now.unwrap_or_else(now_epoch);
    let mut counts: HashMap<String, i64> = HashMap::new();
    for e in findings {
        if e.status.to_uppercase() != "CONFIRMED" {
            continue;
        }
        if age_days(e, now) > window_days {
            continue;
        }
        *counts.entry(key_for(&e.file_path)).or_insert(0) += 1;
    }
    counts
}

/// Per-file exponential-decay repeat-offender score. Mirrors `file_repeat_scores`.
pub fn file_repeat_scores(
    findings: &[HistoryEntry],
    half_life_days: f64,
    now: Option<i64>,
) -> HashMap<String, f64> {
    let now = now.unwrap_or_else(now_epoch);
    let decay_lambda = std::f64::consts::LN_2 / half_life_days;
    let mut scores: HashMap<String, f64> = HashMap::new();
    for e in findings {
        if e.status.to_uppercase() != "CONFIRMED" {
            continue;
        }
        let Some(weight) = severity_weight(&e.severity.to_uppercase()) else {
            continue;
        };
        let age = age_days(e, now);
        let contribution = weight * (-decay_lambda * age).exp();
        if contribution <= 0.0 {
            continue;
        }
        let entry = scores.entry(key_for(&e.file_path)).or_insert(0.0);
        *entry = (*entry + contribution).min(MAX_FILE_SCORE);
    }
    scores
}

/// Aggregate stats. Mirrors `summarize` (now-independent: `top_files` uses an
/// infinite window).
pub fn summarize(entries: &[HistoryEntry], top_n: usize) -> Value {
    let total = entries.len();
    let mut by_status: Vec<(String, i64)> = Vec::new();
    let mut by_severity: Vec<(String, i64)> = Vec::new();
    let mut run_ids: Vec<String> = Vec::new();
    let bump = |v: &mut Vec<(String, i64)>, k: String| {
        if let Some(e) = v.iter_mut().find(|(x, _)| *x == k) {
            e.1 += 1;
        } else {
            v.push((k, 1));
        }
    };
    for e in entries {
        bump(&mut by_status, e.status.to_uppercase());
        bump(&mut by_severity, e.severity.to_uppercase());
        if !e.run_id.is_empty() && !run_ids.contains(&e.run_id) {
            run_ids.push(e.run_id.clone());
        }
    }
    let confirmed = by_status
        .iter()
        .find(|(s, _)| s == "CONFIRMED")
        .map(|(_, c)| *c)
        .unwrap_or(0);
    let counts = file_repeat_counts(entries, f64::INFINITY, Some(0));
    let mut ranked: Vec<(String, i64)> = counts.into_iter().collect();
    ranked.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(&b.0)));
    ranked.truncate(top_n);

    let mut status_map = Map::new();
    for (k, v) in &by_status {
        status_map.insert(k.clone(), json!(v));
    }
    let mut sev_map = Map::new();
    for (k, v) in &by_severity {
        sev_map.insert(k.clone(), json!(v));
    }
    json!({
        "total": total,
        "by_status": Value::Object(status_map),
        "by_severity": Value::Object(sev_map),
        "confirm_rate": if total > 0 { confirmed as f64 / total as f64 } else { 0.0 },
        "run_count": run_ids.len(),
        "top_files": ranked.iter().map(|(p, c)| json!({"file_path": p, "count": c})).collect::<Vec<_>>(),
    })
}

/// Format recent history as a Markdown block for the advisor prompt. Mirrors
/// `format_history_block`.
pub fn format_history_block(entries: &[HistoryEntry]) -> String {
    if entries.is_empty() {
        return String::new();
    }
    // Group by normalized path, preserving first-seen order.
    let mut order: Vec<String> = Vec::new();
    let mut grouped: HashMap<String, Vec<&HistoryEntry>> = HashMap::new();
    let mut display: HashMap<String, String> = HashMap::new();
    for e in entries {
        let key = key_for(&e.file_path);
        if !grouped.contains_key(&key) {
            order.push(key.clone());
            display.insert(key.clone(), e.file_path.clone());
        }
        grouped.get_mut(&key).map(|v| v.push(e)).unwrap_or_else(|| {
            grouped.insert(key.clone(), vec![e]);
        });
    }
    let mut lines = vec![
        "## Recent findings from prior runs".to_string(),
        String::new(),
    ];
    for key in &order {
        let file_entries = &grouped[key];
        let count_note = if file_entries.len() > 1 {
            format!(" — {} prior findings", file_entries.len())
        } else {
            String::new()
        };
        lines.push(format!("### File{count_note}"));
        lines.push(fence(&display[key], ""));
        for e in file_entries {
            lines.push(format!("- [{}] ({}):", e.severity, e.status));
            lines.push(fence(&e.description, ""));
        }
        lines.push(String::new());
    }
    format!("{}\n", lines.join("\n").trim_end())
}

/// Generate a collision-resistant run id: `YYYYMMDDTHHMMSSZ-XXXXXXXX`.
/// Mirrors `new_run_id` (random suffix via system entropy proxy).
pub fn new_run_id() -> String {
    let secs = now_epoch();
    let days = secs.div_euclid(86400);
    let tod = secs.rem_euclid(86400);
    let (y, mo, d) = civil_from_days(days);
    let h = tod / 3600;
    let mi = (tod % 3600) / 60;
    let s = tod % 60;
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|x| x.subsec_nanos())
        .unwrap_or(0);
    let rand = (nanos as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
    format!(
        "{y:04}{mo:02}{d:02}T{h:02}{mi:02}{s:02}Z-{:08x}",
        (rand >> 32) as u32
    )
}

fn civil_from_days(z: i64) -> (i64, i64, i64) {
    let z = z + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// Build a history entry timestamped now. Mirrors `entry_now`.
pub fn entry_now(
    file_path: &str,
    severity: &str,
    description: &str,
    status: &str,
    run_id: &str,
) -> HistoryEntry {
    let secs = now_epoch();
    let days = secs.div_euclid(86400);
    let tod = secs.rem_euclid(86400);
    let (y, mo, d) = civil_from_days(days);
    let timestamp = format!(
        "{y:04}-{mo:02}-{d:02}T{:02}:{:02}:{:02}+00:00",
        tod / 3600,
        (tod % 3600) / 60,
        tod % 60
    );
    HistoryEntry {
        timestamp,
        file_path: file_path.to_string(),
        severity: severity.to_string(),
        description: description.to_string(),
        status: status.to_string(),
        run_id: run_id.to_string(),
        schema_version: HISTORY_SCHEMA_VERSION.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn golden() -> Value {
        serde_json::from_str(include_str!("../tests/parity/history.json")).unwrap()
    }

    fn entries() -> Vec<HistoryEntry> {
        let g = golden();
        g["entries_json"]
            .as_array()
            .unwrap()
            .iter()
            .map(|o| HistoryEntry {
                timestamp: o["timestamp"].as_str().unwrap().into(),
                file_path: o["file_path"].as_str().unwrap().into(),
                severity: o["severity"].as_str().unwrap().into(),
                description: o["description"].as_str().unwrap().into(),
                status: o["status"].as_str().unwrap().into(),
                run_id: o["run_id"].as_str().unwrap().into(),
                schema_version: o["schema_version"].as_str().unwrap_or("1.0").into(),
            })
            .collect()
    }

    #[test]
    fn scores_and_counts_match_python() {
        let g = golden();
        let now = g["now_epoch"].as_i64().unwrap();
        let scores = file_repeat_scores(&entries(), 30.0, Some(now));
        for (k, v) in g["scores"].as_object().unwrap() {
            let got = scores.get(k).copied().unwrap_or(0.0);
            assert!(
                (got - v.as_f64().unwrap()).abs() < 1e-9,
                "score {k}: {got} vs {v}"
            );
        }
        assert_eq!(scores.len(), g["scores"].as_object().unwrap().len());

        let counts = file_repeat_counts(&entries(), 90.0, Some(now));
        for (k, v) in g["counts"].as_object().unwrap() {
            assert_eq!(
                counts.get(k).copied().unwrap_or(0),
                v.as_i64().unwrap(),
                "count {k}"
            );
        }
        assert_eq!(counts.len(), g["counts"].as_object().unwrap().len());
    }

    #[test]
    fn summarize_matches_python() {
        assert_eq!(summarize(&entries(), 10), golden()["summarize"]);
    }

    #[test]
    fn format_block_matches_python() {
        assert_eq!(
            format_history_block(&entries()[..3]),
            golden()["format_block"].as_str().unwrap()
        );
    }

    #[test]
    fn new_run_id_shape() {
        let id = new_run_id();
        // YYYYMMDD(8) + T(1) + HHMMSS(6) + Z(1) + -(1) + 8 hex = 25.
        assert_eq!(id.len(), 25);
        assert!(id.contains("Z-"));
    }
}
