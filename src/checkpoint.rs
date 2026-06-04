//! Port of `advisor/checkpoint.py` — load/list saved run checkpoints
//! (`<target>/.advisor/run-<id>.json`). The save path is exercised by
//! `plan --checkpoint` (not yet wired); `load`/`list` back `advisor audit`.

use std::path::{Path, PathBuf};

use once_cell::sync::Lazy;
use regex::Regex;
use serde_json::Value;

use crate::fs::{read_text_capped, ReadCappedError, MAX_ADVISOR_FILE_BYTES};

pub const CHECKPOINT_SCHEMA_VERSION: &str = "1.0";
const CHECKPOINT_PREFIX: &str = "run-";
const CHECKPOINT_SUFFIX: &str = ".json";

static RUN_ID_RE: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$").expect("run-id regex"));

/// A persisted snapshot of a planned advisor run. Mirrors `Checkpoint`.
#[derive(Debug, Clone)]
pub struct Checkpoint {
    pub run_id: String,
    pub created_at: String,
    pub target: String,
    pub team_name: String,
    pub file_types: String,
    pub min_priority: i64,
    pub max_runners: i64,
    pub advisor_model: String,
    pub runner_model: String,
    pub max_fixes_per_runner: i64,
    pub large_file_line_threshold: i64,
    pub large_file_max_fixes: i64,
    pub test_command: String,
    pub context: String,
    pub tasks: Vec<Value>,
    pub batches: Vec<Value>,
    pub schema_version: String,
}

fn validate_run_id(run_id: &str) -> Result<(), String> {
    if !RUN_ID_RE.is_match(run_id) {
        return Err(
            "invalid run_id: use only letters, digits, underscore, dot, and hyphen".to_string(),
        );
    }
    if run_id.len() > 128 {
        return Err("invalid run_id: must be 128 characters or fewer".to_string());
    }
    Ok(())
}

/// Absolute path for a checkpoint file. Mirrors `checkpoint_path`.
pub fn checkpoint_path(target: &Path, run_id: &str) -> Result<PathBuf, String> {
    validate_run_id(run_id)?;
    Ok(target
        .join(".advisor")
        .join(format!("{CHECKPOINT_PREFIX}{run_id}{CHECKPOINT_SUFFIX}")))
}

fn as_i64(v: Option<&Value>) -> Option<i64> {
    match v {
        Some(Value::Number(n)) => n.as_i64(),
        Some(Value::String(s)) => s.trim().parse().ok(),
        _ => None,
    }
}

fn as_str(v: Option<&Value>) -> Option<String> {
    v.map(|x| match x {
        Value::String(s) => s.clone(),
        other => other.to_string(),
    })
}

/// Load a checkpoint by run_id. Mirrors `load_checkpoint` (missing → `Err`,
/// malformed → `Err`, non-list tasks/batches → `Err`).
pub fn load_checkpoint(target: &Path, run_id: &str) -> Result<Checkpoint, String> {
    let path = checkpoint_path(target, run_id)?;
    let text = match read_text_capped(&path, MAX_ADVISOR_FILE_BYTES) {
        Ok(t) => t,
        Err(ReadCappedError::NotFound) => {
            return Err(format!("no checkpoint at {}", path.display()))
        }
        Err(e) => return Err(format!("could not read checkpoint {}: {e}", path.display())),
    };
    let obj: Value = serde_json::from_str(&text)
        .map_err(|e| format!("could not read checkpoint {}: {e}", path.display()))?;
    let Some(map) = obj.as_object() else {
        return Err(format!(
            "could not read checkpoint {}: expected a JSON object",
            path.display()
        ));
    };

    let raw_tasks = map.get("tasks").cloned().unwrap_or(Value::Array(vec![]));
    let raw_batches = map.get("batches").cloned().unwrap_or(Value::Array(vec![]));
    let tasks = match raw_tasks {
        Value::Array(a) => a,
        other => {
            return Err(format!(
                "checkpoint {}: 'tasks' must be a JSON array, got {}",
                path.display(),
                json_type(&other)
            ))
        }
    };
    let batches_arr = match raw_batches {
        Value::Array(a) => a,
        other => {
            return Err(format!(
                "checkpoint {}: 'batches' must be a JSON array, got {}",
                path.display(),
                json_type(&other)
            ))
        }
    };
    // Keep only dict batch entries (mirror the skip-non-dict behavior).
    let batches: Vec<Value> = batches_arr.into_iter().filter(|b| b.is_object()).collect();

    let version = as_str(map.get("schema_version")).unwrap_or_default();

    let req_str = |k: &str| -> Result<String, String> {
        as_str(map.get(k)).ok_or_else(|| {
            format!(
                "checkpoint {} is missing required fields: '{k}'",
                path.display()
            )
        })
    };
    let req_int = |k: &str| -> Result<i64, String> {
        as_i64(map.get(k)).ok_or_else(|| {
            format!(
                "checkpoint {} is missing required fields: '{k}'",
                path.display()
            )
        })
    };

    Ok(Checkpoint {
        run_id: req_str("run_id")?,
        created_at: req_str("created_at")?,
        target: req_str("target")?,
        team_name: req_str("team_name")?,
        file_types: req_str("file_types")?,
        min_priority: req_int("min_priority")?,
        max_runners: req_int("max_runners")?,
        advisor_model: req_str("advisor_model")?,
        runner_model: req_str("runner_model")?,
        max_fixes_per_runner: as_i64(map.get("max_fixes_per_runner")).unwrap_or(5),
        large_file_line_threshold: as_i64(map.get("large_file_line_threshold")).unwrap_or(800),
        large_file_max_fixes: as_i64(map.get("large_file_max_fixes")).unwrap_or(3),
        test_command: as_str(map.get("test_command")).unwrap_or_default(),
        context: as_str(map.get("context")).unwrap_or_default(),
        tasks,
        batches,
        schema_version: if version.is_empty() {
            CHECKPOINT_SCHEMA_VERSION.to_string()
        } else {
            version
        },
    })
}

fn json_type(v: &Value) -> &'static str {
    match v {
        Value::Null => "NoneType",
        Value::Bool(_) => "bool",
        Value::Number(_) => "int",
        Value::String(_) => "str",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
    }
}

/// Return all run_ids with saved checkpoints, newest (lexical) first. Mirrors
/// `list_checkpoints`.
pub fn list_checkpoints(target: &Path) -> Vec<String> {
    let dir = target.join(".advisor");
    let Ok(entries) = std::fs::read_dir(&dir) else {
        return Vec::new();
    };
    let mut ids: Vec<String> = Vec::new();
    for entry in entries.flatten() {
        let name = entry.file_name().to_string_lossy().to_string();
        if !(name.starts_with(CHECKPOINT_PREFIX) && name.ends_with(CHECKPOINT_SUFFIX)) {
            continue;
        }
        if !entry.path().is_file() {
            continue;
        }
        // Cheap JSON-object sniff on the first 2048 bytes.
        let Ok(bytes) = std::fs::read(entry.path()) else {
            continue;
        };
        let head: Vec<u8> = bytes.into_iter().take(2048).collect();
        let trimmed = String::from_utf8_lossy(&head);
        if !trimmed.trim_start().starts_with('{') {
            continue;
        }
        let extracted = &name[CHECKPOINT_PREFIX.len()..name.len() - CHECKPOINT_SUFFIX.len()];
        if !RUN_ID_RE.is_match(extracted) {
            continue;
        }
        ids.push(extracted.to_string());
    }
    ids.sort();
    ids.reverse();
    ids
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn run_id_validation() {
        assert!(validate_run_id("20260604T000000Z-abcd1234").is_ok());
        assert!(validate_run_id("bad id").is_err());
        assert!(validate_run_id("-leadinghyphen").is_err());
        assert!(validate_run_id(&"x".repeat(129)).is_err());
    }

    #[test]
    fn parity_checkpoint_json() {
        use std::collections::HashMap;
        let raw = std::fs::read_to_string("tests/parity/checkpoint.json").unwrap();
        let v: HashMap<String, serde_json::Value> = serde_json::from_str(&raw).unwrap();
        // run_id RE matches
        assert_eq!(
            v["run_id_valid"].as_bool().unwrap(),
            validate_run_id("run-20240101-120000-abc123de").is_ok()
        );
        assert_eq!(
            v["run_id_invalid_bang"].as_bool().unwrap(),
            validate_run_id("!bad").is_ok()
        );
        assert_eq!(
            v["run_id_invalid_empty"].as_bool().unwrap(),
            validate_run_id("").is_ok()
        );
        assert_eq!(
            v["run_id_valid_simple"].as_bool().unwrap(),
            validate_run_id("run-abc").is_ok()
        );
        // checkpoint_path dir and suffix
        let p = checkpoint_path(std::path::Path::new("."), "run-abc").unwrap();
        assert_eq!(
            p.file_name().unwrap().to_str().unwrap(),
            v["checkpoint_path_suffix"].as_str().unwrap()
        );
        assert_eq!(
            p.parent().unwrap().file_name().unwrap().to_str().unwrap(),
            v["checkpoint_path_dir"].as_str().unwrap()
        );
    }

    #[test]
    fn load_roundtrip() {
        let dir = std::env::temp_dir().join(format!("advisor_cp_{}", std::process::id()));
        std::fs::create_dir_all(dir.join(".advisor")).unwrap();
        let cp_json = r#"{"run_id":"r1","created_at":"2026-06-04T00:00:00+00:00","target":"/repo","team_name":"review","file_types":"*.py","min_priority":3,"max_runners":5,"advisor_model":"claude-opus-4-7","runner_model":"claude-sonnet-4-6","max_fixes_per_runner":2,"large_file_line_threshold":800,"large_file_max_fixes":3,"test_command":"","context":"","tasks":[{"file_path":"a.py","priority":5}],"batches":[{"batch_id":1,"tasks":[{"file_path":"a.py","priority":5}]}],"schema_version":"1.0"}"#;
        std::fs::write(dir.join(".advisor").join("run-r1.json"), cp_json).unwrap();
        let cp = load_checkpoint(&dir, "r1").unwrap();
        assert_eq!(cp.run_id, "r1");
        assert_eq!(cp.max_fixes_per_runner, 2);
        assert_eq!(cp.tasks.len(), 1);
        assert_eq!(cp.batches.len(), 1);
        assert!(load_checkpoint(&dir, "missing").is_err());
        let _ = std::fs::remove_dir_all(&dir);
    }
}
