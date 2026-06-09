use std::collections::VecDeque;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

pub const LIVE_DIR_NAME: &str = ".advisor";
pub const LIVE_SUBDIR: &str = "live";
pub const LIVE_FILE_NAME: &str = "events.jsonl";
pub const LIVE_SCHEMA_VERSION: &str = "1.0";

pub const MAX_LINE: usize = 65536;
pub const MAX_TAIL: usize = 5000;
pub const TAIL_READ_BYTES: usize = MAX_LINE + 1;

pub fn live_dir(target: &Path) -> PathBuf {
    target.join(LIVE_DIR_NAME).join(LIVE_SUBDIR)
}

pub fn live_events_path(target: &Path) -> PathBuf {
    live_dir(target).join(LIVE_FILE_NAME)
}

/// Extract last `seq` from a JSONL tail chunk. Returns 0 if absent/malformed.
pub fn last_seq_from_tail(tail: &[u8]) -> i64 {
    let lines: Vec<&[u8]> = tail
        .split(|&b| b == b'\n')
        .filter(|l| !l.iter().all(|&b| b == b' ' || b == b'\t' || b == b'\r'))
        .collect();
    for line in lines.iter().rev() {
        let s = match std::str::from_utf8(line) {
            Ok(s) => s.trim(),
            Err(_) => continue,
        };
        if s.is_empty() {
            continue;
        }
        let record: Value = match serde_json::from_str(s) {
            Ok(v) => v,
            Err(_) => continue,
        };
        if let Some(obj) = record.as_object() {
            if let Some(seq_val) = obj.get("seq") {
                if let Some(seq) = seq_val.as_i64() {
                    if seq >= 0 {
                        return seq;
                    }
                }
            }
        }
    }
    0
}

fn read_final_tail(path: &Path) -> Vec<u8> {
    let meta = match std::fs::metadata(path) {
        Ok(m) => m,
        Err(_) => return vec![],
    };
    let size = meta.len() as usize;
    if size == 0 {
        return vec![];
    }
    let chunk = size.min(TAIL_READ_BYTES);
    let offset = (size - chunk) as u64;
    use std::io::{Read, Seek, SeekFrom};
    let mut f = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(_) => return vec![],
    };
    if f.seek(SeekFrom::Start(offset)).is_err() {
        return vec![];
    }
    let mut buf = vec![0u8; chunk];
    match f.read(&mut buf) {
        Ok(n) => buf[..n].to_vec(),
        Err(_) => vec![],
    }
}

fn next_seq(path: &Path) -> i64 {
    if !path.exists() {
        return 1;
    }
    let tail = read_final_tail(path);
    last_seq_from_tail(&tail) + 1
}

/// Append a single event. Returns the path written. Simplified lock (no flock, matches history.rs).
pub fn append_event(
    target: &Path,
    kind: &str,
    data: Option<Value>,
    ts: Option<&str>,
) -> Result<PathBuf, String> {
    if kind.is_empty() {
        return Err(format!("kind must be a non-empty string, got {:?}", kind));
    }
    let data = data.unwrap_or_else(|| json!({}));
    if !data.is_object() {
        return Err(format!("data must be a dict or None, got {}", data));
    }
    let path = live_events_path(target);
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| format!("create_dir_all: {e}"))?;
    }

    let ts_string: String = match ts {
        Some(s) => s.to_string(),
        None => {
            // chrono not in deps — use SystemTime → manual ISO-8601 ms UTC
            use std::time::{SystemTime, UNIX_EPOCH};
            let now = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default();
            let total_ms = now.as_millis();
            let secs = (total_ms / 1000) as u64;
            let ms = (total_ms % 1000) as u32;
            // Convert epoch seconds → ISO-8601 UTC string
            epoch_to_iso8601_ms(secs, ms)
        }
    };

    // Read tail to get last seq, then append atomically enough for our use case
    // (append_event called at most a few times per run, not per-token).
    let tail = read_final_tail(&path);
    let seq = if tail.is_empty() && !path.exists() {
        1
    } else {
        last_seq_from_tail(&tail) + 1
    };

    let record = json!({
        "schema_version": LIVE_SCHEMA_VERSION,
        "ts": ts_string,
        "seq": seq,
        "kind": kind,
        "data": data,
    });
    // Python uses separators=(",", ":") — compact, no spaces
    let line = serde_json::to_string(&record).map_err(|e| e.to_string())?;
    let encoded = line.as_bytes();
    if encoded.len() > MAX_LINE {
        return Err(format!(
            "live event too large ({} bytes > {} byte per-line cap); trim the data payload",
            encoded.len(),
            MAX_LINE
        ));
    }
    let mut f = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(&path)
        .map_err(|e| e.to_string())?;
    crate::fs::lock_exclusive(&f).map_err(|e| e.to_string())?;
    let write_result = (|| -> Result<(), String> {
        f.write_all(line.as_bytes()).map_err(|e| e.to_string())?;
        f.write_all(b"\n").map_err(|e| e.to_string())?;
        f.flush().map_err(|e| e.to_string())?;
        Ok(())
    })();
    let _ = crate::fs::unlock(&f);
    write_result?;
    Ok(path)
}

/// Return events with seq > since, up to limit. Chronological order.
pub fn load_recent_events(target: &Path, since: Option<i64>, limit: usize) -> Vec<Value> {
    if limit == 0 {
        return vec![];
    }
    let cap = limit.min(MAX_TAIL);
    let path = live_events_path(target);
    if !path.exists() {
        return vec![];
    }
    let f = match std::fs::File::open(&path) {
        Ok(f) => f,
        Err(_) => return vec![],
    };
    let reader = BufReader::new(f);
    let mut keep: VecDeque<Value> = VecDeque::new();

    for raw_line in reader.lines() {
        let raw = match raw_line {
            Ok(s) => s,
            Err(_) => continue,
        };
        // oversized lines: BufReader::lines() returns the full line regardless
        // of length; cap check below mirrors Python's _MAX_LINE guard.
        if raw.len() > MAX_LINE {
            continue;
        }
        let line = raw.trim();
        if line.is_empty() {
            continue;
        }
        let record: Value = match serde_json::from_str(line) {
            Ok(v) => v,
            Err(_) => continue,
        };
        if !record.is_object() {
            continue;
        }
        let seq = match record.get("seq").and_then(|v| v.as_i64()) {
            Some(s) if s >= 0 => s,
            _ => continue,
        };
        if let Some(s) = since {
            if seq <= s {
                continue;
            }
        }
        if keep.len() == cap {
            keep.pop_front();
        }
        keep.push_back(record);
    }
    keep.into_iter().collect()
}

/// Return highest seq written, or 0 if absent.
pub fn latest_seq(target: &Path) -> i64 {
    let path = live_events_path(target);
    if !path.exists() {
        return 0;
    }
    (next_seq(&path) - 1).max(0)
}

/// Convert epoch seconds + milliseconds → "2026-05-26T17:41:57.892Z"
fn epoch_to_iso8601_ms(secs: u64, ms: u32) -> String {
    // Gregorian calendar computation — no deps.
    let mut days = (secs / 86400) as u32;
    let time_secs = (secs % 86400) as u32;
    let hour = time_secs / 3600;
    let minute = (time_secs % 3600) / 60;
    let second = time_secs % 60;

    // Days since 1970-01-01 → year/month/day
    let mut year = 1970u32;
    loop {
        let dy = days_in_year(year);
        if days < dy {
            break;
        }
        days -= dy;
        year += 1;
    }
    let mut month = 1u32;
    loop {
        let dm = days_in_month(year, month);
        if days < dm {
            break;
        }
        days -= dm;
        month += 1;
    }
    let day = days + 1;
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}.{:03}Z",
        year, month, day, hour, minute, second, ms
    )
}

fn is_leap(y: u32) -> bool {
    (y % 4 == 0 && y % 100 != 0) || y % 400 == 0
}
fn days_in_year(y: u32) -> u32 {
    if is_leap(y) {
        366
    } else {
        365
    }
}
fn days_in_month(y: u32, m: u32) -> u32 {
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::TempDir;

    fn golden() -> serde_json::Value {
        let s = include_str!("../tests/parity/live.json");
        serde_json::from_str(s).unwrap()
    }

    #[test]
    fn parity_constants() {
        let g = golden();
        assert_eq!(LIVE_DIR_NAME, g["LIVE_DIR_NAME"].as_str().unwrap());
        assert_eq!(LIVE_SUBDIR, g["LIVE_SUBDIR"].as_str().unwrap());
        assert_eq!(LIVE_FILE_NAME, g["LIVE_FILE_NAME"].as_str().unwrap());
        assert_eq!(
            LIVE_SCHEMA_VERSION,
            g["LIVE_SCHEMA_VERSION"].as_str().unwrap()
        );
        assert_eq!(MAX_LINE, g["_MAX_LINE"].as_u64().unwrap() as usize);
        assert_eq!(MAX_TAIL, g["_MAX_TAIL"].as_u64().unwrap() as usize);
        assert_eq!(
            TAIL_READ_BYTES,
            g["_TAIL_READ_BYTES"].as_u64().unwrap() as usize
        );
    }

    #[test]
    fn parity_paths() {
        let g = golden();
        let t = Path::new("<target>");
        let dir = live_dir(t).to_string_lossy().replace('\\', "/");
        let ep = live_events_path(t).to_string_lossy().replace('\\', "/");
        assert_eq!(dir, g["live_dir"].as_str().unwrap());
        assert_eq!(ep, g["live_events_path"].as_str().unwrap());
    }

    #[test]
    fn parity_last_seq_from_tail() {
        let g = golden();
        assert_eq!(
            last_seq_from_tail(b""),
            g["last_seq_empty"].as_i64().unwrap()
        );
        assert_eq!(
            last_seq_from_tail(b"{\"seq\":5,\"kind\":\"run_start\"}\n"),
            g["last_seq_single"].as_i64().unwrap()
        );
        assert_eq!(
            last_seq_from_tail(b"{\"seq\":3,\"kind\":\"a\"}\n{\"seq\":7,\"kind\":\"b\"}\n"),
            g["last_seq_multi"].as_i64().unwrap()
        );
        assert_eq!(
            last_seq_from_tail(b"{\"seq\":4,\"kind\":\"a\"}\nnot-json\n"),
            g["last_seq_corrupt_last"].as_i64().unwrap()
        );
        assert_eq!(
            last_seq_from_tail(b"{\"kind\":\"x\"}\n"),
            g["last_seq_no_seq_field"].as_i64().unwrap()
        );
        assert_eq!(
            last_seq_from_tail(b"{\"seq\":-1,\"kind\":\"x\"}\n"),
            g["last_seq_negative"].as_i64().unwrap()
        );
    }

    #[test]
    fn parity_append_and_load() {
        let g = golden();
        let tmp = TempDir::new().unwrap();
        let target = tmp.path().join("proj");
        fs::create_dir_all(&target).unwrap();

        let p = append_event(
            &target,
            "run_start",
            Some(json!({"runner_count": 2})),
            Some("2026-05-26T17:41:57.892Z"),
        )
        .unwrap();
        assert_eq!(
            p.file_name().unwrap().to_str().unwrap(),
            g["append_returns_events_jsonl"].as_str().unwrap()
        );

        append_event(
            &target,
            "runner_spawn",
            Some(json!({"runner_id": "runner-1"})),
            Some("2026-05-26T17:41:58.100Z"),
        )
        .unwrap();
        append_event(
            &target,
            "run_end",
            Some(json!({"status": "ok"})),
            Some("2026-05-26T17:42:00.000Z"),
        )
        .unwrap();

        let events = load_recent_events(&target, None, 200);
        assert_eq!(events.len(), g["load_all_count"].as_u64().unwrap() as usize);

        let seqs: Vec<i64> = events.iter().map(|e| e["seq"].as_i64().unwrap()).collect();
        let exp_seqs: Vec<i64> = g["load_all_seqs"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_i64().unwrap())
            .collect();
        assert_eq!(seqs, exp_seqs);

        let kinds: Vec<&str> = events.iter().map(|e| e["kind"].as_str().unwrap()).collect();
        let exp_kinds: Vec<&str> = g["load_all_kinds"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert_eq!(kinds, exp_kinds);

        let since1: Vec<&str> = load_recent_events(&target, Some(1), 200)
            .iter()
            .map(|e| e["kind"].as_str().unwrap().to_owned())
            .map(|s| -> &'static str {
                // leak for lifetime simplicity in test
                Box::leak(s.into_boxed_str())
            })
            .collect();
        let exp_since1: Vec<&str> = g["load_since_1"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap())
            .collect();
        assert_eq!(since1, exp_since1);

        let since2: Vec<String> = load_recent_events(&target, Some(2), 200)
            .iter()
            .map(|e| e["kind"].as_str().unwrap().to_string())
            .collect();
        let exp_since2: Vec<String> = g["load_since_2"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        assert_eq!(since2, exp_since2);

        let limit1: Vec<String> = load_recent_events(&target, None, 1)
            .iter()
            .map(|e| e["kind"].as_str().unwrap().to_string())
            .collect();
        let exp_limit1: Vec<String> = g["load_limit_1"]
            .as_array()
            .unwrap()
            .iter()
            .map(|v| v.as_str().unwrap().to_string())
            .collect();
        assert_eq!(limit1, exp_limit1);

        assert_eq!(latest_seq(&target), g["latest_seq_val"].as_i64().unwrap());
    }

    #[test]
    fn parity_missing_path() {
        let g = golden();
        let tmp = TempDir::new().unwrap();
        let missing = tmp.path().join("nonexistent");
        let events = load_recent_events(&missing, None, 200);
        assert!(events.is_empty());
        let exp: Vec<Value> = g["load_missing"].as_array().unwrap().to_vec();
        assert_eq!(events, exp);
        assert_eq!(
            latest_seq(&missing),
            g["latest_seq_missing"].as_i64().unwrap()
        );
    }
}
