//! Web dashboard server for advisor — pure Rust port of `advisor/web/server.py`.

use serde_json::json;
use std::collections::HashMap;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};
use tiny_http::{Header, Method, Response, Server};

use crate::cost::estimate_cost;
use crate::focus::{create_focus_tasks, FocusTask};
use crate::fs::CONTENT_SCAN_LIMIT;
use crate::history::{history_path, load_recent, HISTORY_SCHEMA_VERSION};
use crate::live::{latest_seq, live_events_path, load_recent_events, LIVE_SCHEMA_VERSION};
use crate::rank::{load_advisorignore, rank_files};

// Embedded assets via include_str!
const INDEX_HTML: &str = include_str!("web/assets/index.html");
const APP_CSS: &str = include_str!("web/assets/app.css");
const APP_JS: &str = include_str!("web/assets/app.js");

#[derive(Debug, Clone)]
pub struct AppState {
    pub target: PathBuf,
    pub default_file_types: String,
    pub default_min_priority: u8,
    pub default_max_runners: usize,
    pub default_advisor_model: String,
    pub default_runner_model: String,
}

const ACTIVE_WINDOW_SECONDS: f64 = 15.0;

// Percent-decode a URL component. Accumulates raw bytes then UTF-8-decodes once,
// so multi-byte sequences (e.g. %C3%A9) decode correctly instead of as Latin-1.
fn decode_percent(s: &str) -> String {
    let bytes = s.as_bytes();
    let mut buf: Vec<u8> = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' {
            if i + 2 < bytes.len() {
                if let Some(b) = hex_byte(bytes[i + 1], bytes[i + 2]) {
                    buf.push(b);
                    i += 3;
                    continue;
                }
            }
            // truncated or invalid %XX: emit remaining bytes literally
            buf.extend_from_slice(&bytes[i..]);
            break;
        } else if bytes[i] == b'+' {
            buf.push(b' ');
        } else {
            buf.push(bytes[i]);
        }
        i += 1;
    }
    String::from_utf8_lossy(&buf).into_owned()
}

fn hex_byte(h: u8, l: u8) -> Option<u8> {
    let h_val = (h as char).to_digit(16)? as u8;
    let l_val = (l as char).to_digit(16)? as u8;
    Some((h_val << 4) | l_val)
}

// Simple query parameter parser
fn parse_query(query: &str) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for part in query.split('&') {
        if part.is_empty() {
            continue;
        }
        let mut sub = part.splitn(2, '=');
        let key = sub.next().unwrap_or("");
        let val = sub.next().unwrap_or("");
        map.insert(key.to_string(), decode_percent(val));
    }
    map
}

// Gregorian calendar checks for manual ISO-8601 formatting without chrono
fn is_leap(y: u32) -> bool {
    (y % 4 == 0 && y % 100 != 0) || y % 400 == 0
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

fn epoch_to_iso8601_sec(secs: u64) -> String {
    let mut days = (secs / 86400) as u32;
    let time_secs = (secs % 86400) as u32;
    let hour = time_secs / 3600;
    let minute = (time_secs % 3600) / 60;
    let second = time_secs % 60;

    let mut year = 1970u32;
    loop {
        let dy = if is_leap(year) { 366 } else { 365 };
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
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}+00:00",
        year, month, day, hour, minute, second
    )
}

fn get_file_mtime_info(path: &Path) -> (Option<String>, Option<String>, bool) {
    if !path.exists() {
        return (None, None, false);
    }
    let stat = match std::fs::metadata(path) {
        Ok(m) => m,
        Err(_) => return (None, None, false),
    };
    let mtime = match stat.modified() {
        Ok(t) => t,
        Err(_) => return (None, None, false),
    };
    let duration = match mtime.duration_since(UNIX_EPOCH) {
        Ok(d) => d,
        Err(_) => return (None, None, false),
    };
    let secs = duration.as_secs();

    let now = SystemTime::now();
    let age = match now.duration_since(mtime) {
        Ok(d) => d.as_secs_f64(),
        Err(_) => 0.0,
    };

    let iso = epoch_to_iso8601_sec(secs);
    let token = format!("{}:{}", duration.as_nanos(), stat.len());
    let active = age < ACTIVE_WINDOW_SECONDS;

    (Some(iso), Some(token), active)
}

fn discover(target: &Path, file_types: &str) -> Result<Vec<String>, String> {
    crate::fs::validate_file_types(file_types)
        .map_err(|e| format!("invalid --file-types pattern: {e}"))?;
    let pats: Vec<&str> = file_types
        .split(',')
        .map(|s| s.trim())
        .filter(|s| !s.is_empty())
        .collect();
    if pats.is_empty() {
        return Ok(Vec::new());
    }
    let root = target
        .canonicalize()
        .map_err(|e| format!("filesystem error scanning {}: {e}", target.display()))?;

    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::new();
    let mut stack = vec![root];
    while let Some(dir) = stack.pop() {
        let entries = match std::fs::read_dir(&dir) {
            Ok(e) => e,
            Err(_) => continue,
        };
        for entry in entries.flatten() {
            let ft = match entry.file_type() {
                Ok(t) => t,
                Err(_) => continue,
            };
            if ft.is_symlink() {
                continue;
            }
            let path = entry.path();
            if ft.is_dir() {
                stack.push(path);
            } else if ft.is_file() {
                let name = entry.file_name();
                let name = name.to_string_lossy();
                if pats
                    .iter()
                    .any(|pat| crate::rank::fnmatch_match(&name, pat))
                {
                    let s = path.to_string_lossy().to_string();
                    if seen.insert(s.clone()) {
                        out.push(s);
                    }
                }
            }
        }
    }
    Ok(out)
}

fn rank_target(state: &AppState, file_types: &str, min_priority: u8) -> Vec<FocusTask> {
    let min_priority = min_priority.clamp(1, 5);
    let paths = discover(&state.target, file_types).unwrap_or_default();

    let read_fn = |p: &str| -> Option<String> {
        let mut f = std::fs::File::open(p).ok()?;
        let mut buf = vec![0; CONTENT_SCAN_LIMIT];
        let n = f.read(&mut buf).unwrap_or(0);
        Some(String::from_utf8_lossy(&buf[..n]).into_owned())
    };

    let ignore_patterns = load_advisorignore(&state.target.to_string_lossy());
    let ranked = rank_files(
        &paths,
        Some(&read_fn),
        &ignore_patterns,
        None,
        None,
        None,
        90,
    );
    create_focus_tasks(
        &ranked,
        None,
        min_priority,
        crate::focus::DEFAULT_TASK_PROMPT,
    )
}

// JSON Send helper
fn send_json(request: tiny_http::Request, status: u32, json_value: &serde_json::Value) {
    let body = serde_json::to_string(json_value).unwrap_or_default();
    let body_bytes = body.as_bytes();
    let mut response = Response::from_data(body_bytes).with_status_code(status);
    response.add_header(
        Header::from_bytes(
            &b"Content-Type"[..],
            &b"application/json; charset=utf-8"[..],
        )
        .unwrap(),
    );
    response.add_header(Header::from_bytes(&b"Cache-Control"[..], &b"no-store"[..]).unwrap());
    response
        .add_header(Header::from_bytes(&b"X-Content-Type-Options"[..], &b"nosniff"[..]).unwrap());
    response.add_header(
        Header::from_bytes(
            &b"Content-Security-Policy"[..],
            &b"default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"[..],
        )
        .unwrap(),
    );
    let _ = request.respond(response);
}

// Text / Static Asset Send helper
fn send_text(request: tiny_http::Request, body: &str, content_type: &str) {
    let body_bytes = body.as_bytes();
    let mut response = Response::from_data(body_bytes).with_status_code(200);
    response.add_header(
        Header::from_bytes(
            &b"Content-Type"[..],
            format!("{content_type}; charset=utf-8").as_bytes(),
        )
        .unwrap(),
    );
    response
        .add_header(Header::from_bytes(&b"X-Content-Type-Options"[..], &b"nosniff"[..]).unwrap());
    response.add_header(Header::from_bytes(&b"Content-Security-Policy"[..], &b"default-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"[..]).unwrap());
    let _ = request.respond(response);
}

fn host_header_allowed(host_header: &str, bound_port: u16) -> bool {
    let allowed_hosts = [
        format!("127.0.0.1:{bound_port}"),
        format!("localhost:{bound_port}"),
        format!("[::1]:{bound_port}"),
    ];
    !host_header.is_empty() && allowed_hosts.iter().any(|allowed| host_header == allowed)
}

fn handle_get(state: &AppState, request: tiny_http::Request, bound_port: u16) {
    // DNS-rebinding defense: verify Host header against the server's local
    // listening port. The request remote port is the client's ephemeral port.
    let host_header = request
        .headers()
        .iter()
        .find(|h| h.field.as_str().as_str().eq_ignore_ascii_case("Host"))
        .map(|h| h.value.as_str())
        .unwrap_or("");

    if !host_header_allowed(host_header, bound_port) {
        send_json(request, 403, &json!({"error": "forbidden"}));
        return;
    }

    let url_str = request.url();
    let mut parts = url_str.splitn(2, '?');
    let route = parts.next().unwrap_or("/");
    let route_owned = route.to_string();
    let query_str = parts.next().unwrap_or("");
    let qs = parse_query(query_str);

    match route_owned.as_str() {
        "/" | "/index.html" => {
            send_text(request, INDEX_HTML, "text/html");
        }
        "/static/app.css" => {
            send_text(request, APP_CSS, "text/css");
        }
        "/static/app.js" => {
            send_text(request, APP_JS, "application/javascript");
        }
        "/api/target" => {
            let payload = json!({
                "target": state.target.display().to_string(),
                "defaults": {
                    "file_types": state.default_file_types,
                    "min_priority": state.default_min_priority,
                    "max_runners": state.default_max_runners,
                    "advisor_model": state.default_advisor_model,
                    "runner_model": state.default_runner_model,
                }
            });
            send_json(request, 200, &payload);
        }
        "/api/status" => {
            let (h_mtime, h_token, h_active) = get_file_mtime_info(&history_path(&state.target));
            let (l_mtime, l_token, l_active) =
                get_file_mtime_info(&live_events_path(&state.target));
            let payload = json!({
                "last_mtime": h_mtime,
                "token": h_token,
                "is_active": h_active || l_active,
                "history_is_active": h_active,
                "live_is_active": l_active,
                "live_mtime": l_mtime,
                "live_token": l_token,
            });
            send_json(request, 200, &payload);
        }
        "/api/history" => {
            let limit = qs
                .get("limit")
                .and_then(|s| s.parse::<usize>().ok())
                .unwrap_or(100)
                .clamp(1, 1000);
            let entries = load_recent(&state.target, limit);
            let entries_json: Vec<serde_json::Value> = entries
                .into_iter()
                .map(|e| {
                    json!({
                        "timestamp": e.timestamp,
                        "file_path": e.file_path,
                        "severity": e.severity,
                        "description": e.description,
                        "status": e.status,
                        "run_id": e.run_id,
                    })
                })
                .collect();
            let payload = json!({
                "schema_version": HISTORY_SCHEMA_VERSION,
                "target": state.target.display().to_string(),
                "count": entries_json.len(),
                "entries": entries_json,
            });
            send_json(request, 200, &payload);
        }
        "/api/plan" => {
            let file_types = qs
                .get("file_types")
                .map(|s| s.as_str())
                .unwrap_or(&state.default_file_types);
            let min_priority = qs
                .get("min_priority")
                .and_then(|s| s.parse::<u8>().ok())
                .unwrap_or(state.default_min_priority);
            let tasks = rank_target(state, file_types, min_priority);
            let tasks_json: Vec<serde_json::Value> = tasks
                .iter()
                .map(|t| {
                    json!({
                        "file_path": t.file_path,
                        "priority": t.priority,
                    })
                })
                .collect();
            let payload = json!({
                "target": state.target.display().to_string(),
                "file_types": file_types,
                "min_priority": min_priority,
                "task_count": tasks_json.len(),
                "tasks": tasks_json,
            });
            send_json(request, 200, &payload);
        }
        "/api/cost" => {
            let advisor_model = qs
                .get("advisor_model")
                .map(|s| s.as_str())
                .unwrap_or(&state.default_advisor_model);
            let runner_model = qs
                .get("runner_model")
                .map(|s| s.as_str())
                .unwrap_or(&state.default_runner_model);
            let max_runners = qs
                .get("max_runners")
                .and_then(|s| s.parse::<usize>().ok())
                .unwrap_or(state.default_max_runners)
                .clamp(1, crate::config::POOL_SIZE_CEILING as usize);
            let max_fixes = qs
                .get("max_fixes_per_runner")
                .and_then(|s| s.parse::<usize>().ok())
                .unwrap_or(5)
                .clamp(0, 100);
            let file_types = qs
                .get("file_types")
                .map(|s| s.as_str())
                .unwrap_or(&state.default_file_types);
            let min_priority = qs
                .get("min_priority")
                .and_then(|s| s.parse::<u8>().ok())
                .unwrap_or(state.default_min_priority);
            let tasks = rank_target(state, file_types, min_priority);

            if tasks.is_empty() {
                send_json(
                    request,
                    200,
                    &json!({
                        "target": state.target.display().to_string(),
                        "advisor_model": advisor_model,
                        "runner_model": runner_model,
                        "task_count": 0,
                        "estimate": null,
                    }),
                );
                return;
            }

            let estimate = match estimate_cost(
                &tasks,
                None,
                advisor_model,
                runner_model,
                max_fixes as i64,
                Some(max_runners as i64),
                None,
                Some(&state.target),
            ) {
                Ok(est) => est,
                Err(e) => {
                    send_json(request, 500, &json!({"error": e}));
                    return;
                }
            };

            let payload = json!({
                "target": state.target.display().to_string(),
                "advisor_model": advisor_model,
                "runner_model": runner_model,
                "task_count": tasks.len(),
                "estimate": estimate.to_dict(),
            });
            send_json(request, 200, &payload);
        }
        "/api/events" => {
            let since = qs.get("since").and_then(|s| s.parse::<i64>().ok());
            let limit = qs
                .get("limit")
                .and_then(|s| s.parse::<usize>().ok())
                .unwrap_or(200)
                .clamp(1, 1000);
            let events = load_recent_events(&state.target, since, limit);
            let file_latest = latest_seq(&state.target);
            let next_token = if file_latest > 0 {
                file_latest
            } else {
                since.unwrap_or(0)
            };
            let payload = json!({
                "schema_version": LIVE_SCHEMA_VERSION,
                "target": state.target.display().to_string(),
                "count": events.len(),
                "events": events,
                "next_token": next_token,
            });
            send_json(request, 200, &payload);
        }
        _ => {
            send_json(
                request,
                404,
                &json!({"error": format!("no route {:?}", route_owned)}),
            );
        }
    }
}

#[allow(unreachable_code)]
pub fn run_server(
    state: AppState,
    host: &str,
    port: u16,
    log_requests: bool,
    quiet: bool,
) -> Result<(), std::io::Error> {
    let allowed_binds = ["127.0.0.1", "localhost", "::1", "[::1]"];
    if !allowed_binds.contains(&host) {
        return Err(std::io::Error::other(
            format!(
                "refusing to bind non-loopback host {:?}: only loopback binds are supported. Pass one of {:?}",
                host, allowed_binds
            ),
        ));
    }

    let addr = format!("{}:{}", host, port);
    let server = Server::http(&addr).map_err(|e| std::io::Error::other(e.to_string()))?;

    let actual_port = match server.server_addr() {
        tiny_http::ListenAddr::IP(addr) => addr.port(),
        _ => port,
    };
    let display_host = if host.contains(':') && !host.starts_with('[') {
        format!("[{host}]")
    } else {
        host.to_string()
    };
    let url = format!("http://{}:{}", display_host, actual_port);

    if !quiet {
        // Unicode mark colors: check if terminal supports it
        println!(
            "  ✓ advisor dashboard serving {} at {}",
            state.target.display(),
            url
        );
        println!("  ℹ press Ctrl-C to stop");
        println!("  ↻ open in browser: {}", url);
    }

    loop {
        let request = match server.recv() {
            Ok(rq) => rq,
            Err(e) => {
                if log_requests {
                    eprintln!("Error receiving request: {}", e);
                }
                continue;
            }
        };

        if log_requests {
            println!("{} {}", request.method(), request.url());
        }

        if request.method() != &Method::Get {
            send_json(request, 405, &json!({"error": "method not allowed"}));
            continue;
        }

        handle_get(&state, request, actual_port);
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_decode_percent() {
        assert_eq!(decode_percent("hello+world"), "hello world");
        assert_eq!(decode_percent("hello%20world"), "hello world");
        assert_eq!(decode_percent("abc%2Fdef"), "abc/def");
        assert_eq!(decode_percent("no_percent"), "no_percent");
    }

    #[test]
    fn test_parse_query() {
        let qs = parse_query("a=1&b=hello+world&c=%2Fpath%2Fto%2Ffile");
        assert_eq!(qs.get("a").unwrap(), "1");
        assert_eq!(qs.get("b").unwrap(), "hello world");
        assert_eq!(qs.get("c").unwrap(), "/path/to/file");

        let qs_empty = parse_query("");
        assert!(qs_empty.is_empty());
    }

    #[test]
    fn test_epoch_to_iso8601() {
        // 1780576200 is 2026-06-04T12:30:00 UTC
        assert_eq!(
            epoch_to_iso8601_sec(1780576200),
            "2026-06-04T12:30:00+00:00"
        );
    }

    #[test]
    fn test_host_header_allowed_uses_bound_port() {
        assert!(host_header_allowed("127.0.0.1:7070", 7070));
        assert!(host_header_allowed("localhost:7070", 7070));
        assert!(host_header_allowed("[::1]:7070", 7070));

        assert!(!host_header_allowed("", 7070));
        assert!(!host_header_allowed("127.0.0.1:51234", 7070));
        assert!(!host_header_allowed("evil.example:7070", 7070));
    }
}
