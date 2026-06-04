//! Port of `advisor/sarif.py` — SARIF 2.1.0 emitter for advisor findings.
//!
//! Pure conversion: constants, [`synthesize_rule_id`], [`level_for`], the
//! `path:line[:col[:col]]` parser, control-character stripping, source-root
//! containment, and the full [`findings_to_sarif`] document builder.

use std::path::Path;

use serde_json::{json, Map, Value};
use sha1::{Digest, Sha1};

use crate::models::Finding;

/// SARIF schema URI advertised in the emitted document (`SARIF_SCHEMA_URI`).
pub const SARIF_SCHEMA_URI: &str = "https://json.schemastore.org/sarif-2.1.0.json";

/// SARIF spec version (`SARIF_VERSION`).
pub const SARIF_VERSION: &str = "2.1.0";

/// advisor's own emitter schema version (`SCHEMA_VERSION`).
pub const SCHEMA_VERSION: &str = "1.0";

/// Default SARIF level when a severity is unrecognized (`_DEFAULT_LEVEL`).
pub const DEFAULT_LEVEL: &str = "warning";

/// Map an advisor severity string to a SARIF level. Mirrors `_level_for` /
/// `_LEVEL_MAP`: CRITICAL/HIGH -> error, MEDIUM -> warning, LOW -> note.
pub fn level_for(severity: &str) -> &'static str {
    match severity.trim().to_ascii_uppercase().as_str() {
        "CRITICAL" | "HIGH" => "error",
        "MEDIUM" => "warning",
        "LOW" => "note",
        _ => DEFAULT_LEVEL,
    }
}

/// Stable rule key for a finding that lacks one: `{prefix}/{severity-lower}/{slug}`
/// where `slug` is the first 16 hex chars of SHA-1(description). The severity is
/// lowercased so `CRITICAL`/`critical` collapse to one id; the slug depends only
/// on the description. Mirrors Python `synthesize_rule_id`.
pub fn synthesize_rule_id(severity: &str, description: &str, prefix: &str) -> String {
    let mut hasher = Sha1::new();
    hasher.update(description.as_bytes());
    let digest = hasher.finalize();
    // First 16 hex chars (8 bytes). Convert the leading 8 bytes to a big-endian
    // u64 and format in one shot — byte-for-byte identical to per-byte `{:02x}`.
    let mut head = [0u8; 8];
    head.copy_from_slice(&digest[..8]);
    let slug = format!("{:016x}", u64::from_be_bytes(head));
    format!("{prefix}/{}/{slug}", severity.to_ascii_lowercase())
}

/// int32 max — upper clamp for SARIF region fields (`_SARIF_INT_MAX`).
const SARIF_INT_MAX: i64 = 2_147_483_647;

const BLOCK_KEEP: [u32; 3] = [0x09, 0x0A, 0x0D];
const INLINE_STRIP_EXTRA: [u32; 3] = [0x85, 0x2028, 0x2029];
const BIDI_CONTROLS: [u32; 12] = [
    0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2060, 0x200E, 0x200F, 0x2066, 0x2067, 0x2068, 0x2069,
];

/// Remove C0 controls (and DEL) that survive JSON but break consumers. With
/// `keep_block_whitespace`, tab/LF/CR are preserved; otherwise NEL/LS/PS are
/// also stripped. Bidi controls are always stripped. Mirrors `_strip_controls`.
fn strip_controls(text: &str, keep_block_whitespace: bool) -> String {
    if text.is_empty() {
        return String::new();
    }
    text.chars()
        .filter(|c| {
            let o = *c as u32;
            let printable =
                (o >= 0x20 && o != 0x7F) || (keep_block_whitespace && BLOCK_KEEP.contains(&o));
            let extra_ok = keep_block_whitespace || !INLINE_STRIP_EXTRA.contains(&o);
            printable && extra_ok && !BIDI_CONTROLS.contains(&o)
        })
        .collect()
}

/// Clip text to `limit` chars for the SARIF `shortDescription`, collapsing
/// whitespace runs first. Mirrors `_short_text`.
fn short_text(text: &str, limit: usize) -> String {
    let collapsed = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if collapsed.chars().count() <= limit {
        return if collapsed.is_empty() {
            "advisor finding".to_string()
        } else {
            collapsed
        };
    }
    let clipped: String = collapsed.chars().take(limit - 1).collect();
    format!("{}\u{2026}", clipped.trim_end())
}

fn is_int_token(s: &str) -> bool {
    let body = s.strip_prefix('-').unwrap_or(s);
    !body.is_empty() && body.bytes().all(|b| b.is_ascii_digit()) && s != "-"
}

/// Split `path:line[:col[:end_col]]` into `(path, line, start_col, end_col)`.
/// Mirrors `_parse_file_path`.
fn parse_file_path(raw: &str) -> (String, Option<i64>, Option<i64>, Option<i64>) {
    let stripped = raw.trim().trim_matches('`').trim_end();
    let stripped: String = stripped
        .chars()
        .filter(|c| !matches!(c, '\u{0}' | '\n' | '\r' | '\t'))
        .collect();
    let stripped = stripped.trim().to_string();

    let bytes = stripped.as_bytes();
    let (drive_prefix, body) =
        if bytes.len() >= 2 && bytes[1] == b':' && (bytes[0] as char).is_ascii_alphabetic() {
            (stripped[..2].to_string(), stripped[2..].to_string())
        } else {
            (String::new(), stripped.clone())
        };

    let mut all_parts: Vec<String> = body.split(':').map(|s| s.to_string()).collect();
    // Peel trailing non-numeric column-label segments (bounded by a numeric
    // line still being recoverable).
    while all_parts.len() > 2
        && !is_int_token(&all_parts[all_parts.len() - 1])
        && is_int_token(&all_parts[all_parts.len() - 2])
    {
        all_parts.pop();
    }
    let mut trailing_numeric: Vec<i64> = Vec::new();
    while all_parts.len() > 1 && is_int_token(&all_parts[all_parts.len() - 1]) {
        if let Ok(n) = all_parts.pop().unwrap().parse::<i64>() {
            trailing_numeric.push(n);
        }
    }
    if trailing_numeric.is_empty() {
        return (stripped, None, None, None);
    }
    trailing_numeric.reverse();
    let line = Some(trailing_numeric[0]);
    let start_col = trailing_numeric.get(1).copied();
    let end_col = trailing_numeric.get(2).copied();
    let path = if all_parts.iter().any(|p| !p.is_empty()) {
        all_parts.join(":")
    } else {
        String::new()
    };
    (format!("{drive_prefix}{path}"), line, start_col, end_col)
}

/// Percent-encode a path for a SARIF `artifactLocation.uri`, preserving `/`.
/// Mirrors `urllib.parse.quote(rel, safe="/")`.
fn url_quote(s: &str) -> String {
    const SAFE: &[u8] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-~/";
    let mut out = String::with_capacity(s.len());
    for &b in s.as_bytes() {
        if SAFE.contains(&b) {
            out.push(b as char);
        } else {
            out.push_str(&format!("%{b:02X}"));
        }
    }
    out
}

/// Build the `%SRCROOT%` `file://` URI with a trailing slash. Mirrors
/// `_srcroot_uri` over `target_dir.resolve()` for an already-absolute path.
fn srcroot_uri(target_dir: &Path) -> String {
    // Mirror Path.resolve()'s no-fail behavior: use the path lexically when
    // absolute (the live caller passes a resolved/absolute target).
    let abs = if target_dir.is_absolute() {
        target_dir.to_path_buf()
    } else {
        std::env::current_dir()
            .map(|c| c.join(target_dir))
            .unwrap_or_else(|_| target_dir.to_path_buf())
    };
    let uri = format!("file://{}", url_quote(&abs.to_string_lossy()));
    if uri.ends_with('/') {
        uri
    } else {
        format!("{uri}/")
    }
}

/// Resolve a finding path to POSIX-relative under `target_dir`. Mirrors
/// `_resolve_relative` for the common relative-path case (absolute-path
/// containment uses lexical resolution; see PORT_NOTES).
fn resolve_relative(path: &str, target_dir: &Path) -> Result<String, String> {
    let p = Path::new(path);
    if p.is_absolute() {
        let target_abs = if target_dir.is_absolute() {
            target_dir.to_path_buf()
        } else {
            std::env::current_dir()
                .map(|c| c.join(target_dir))
                .unwrap_or_else(|_| target_dir.to_path_buf())
        };
        return match p.strip_prefix(&target_abs) {
            Ok(rel) => Ok(rel.to_string_lossy().replace('\\', "/")),
            Err(_) => Err(format!(
                "file_path {path:?} is outside target_dir {}; SARIF requires paths to resolve under %SRCROOT%",
                target_dir.display()
            )),
        };
    }
    // Windows-style relative path with backslashes.
    if path.contains('\\') {
        if path.split(['/', '\\']).any(|seg| seg == "..") {
            return Err(format!(
                "file_path {path:?} escapes target_dir {} via '..'; SARIF requires paths to resolve under %SRCROOT%",
                target_dir.display()
            ));
        }
        let parts: Vec<&str> = path
            .split(['/', '\\'])
            .filter(|s| !s.is_empty() && *s != ".")
            .collect();
        return Ok(parts.join("/"));
    }
    if p.components().any(|c| c.as_os_str() == "..") {
        return Err(format!(
            "file_path {path:?} escapes target_dir {} via '..'; SARIF requires paths to resolve under %SRCROOT%",
            target_dir.display()
        ));
    }
    Ok(p.to_string_lossy().replace('\\', "/"))
}

fn rule_id_for(f: &Finding, prefix: &str) -> String {
    match &f.rule_id {
        Some(r) if !r.is_empty() => r.clone(),
        _ => synthesize_rule_id(&f.severity, &f.description, prefix),
    }
}

/// Convert verified findings into a SARIF 2.1.0 run object. Mirrors
/// `findings_to_sarif`. Returns `Err` when a path escapes `target_dir`.
pub fn findings_to_sarif(
    findings: &[Finding],
    tool_version: &str,
    target_dir: &Path,
    rule_id_fallback_prefix: &str,
) -> Result<Value, String> {
    let mut rules: Vec<Value> = Vec::new();
    let mut rule_index_by_id: std::collections::HashMap<String, usize> =
        std::collections::HashMap::new();
    let mut results: Vec<Value> = Vec::new();

    for f in findings {
        if f.file_path.trim().is_empty() {
            continue;
        }
        let (file_path, line, start_col, end_col) = parse_file_path(&f.file_path);
        if file_path.is_empty() || file_path == "." {
            continue;
        }
        let rule_id = rule_id_for(f, rule_id_fallback_prefix);
        if !rule_index_by_id.contains_key(&rule_id) {
            rule_index_by_id.insert(rule_id.clone(), rules.len());
            rules.push(json!({
                "id": rule_id,
                "name": rule_id.replace('/', "_"),
                "shortDescription": {"text": strip_controls(&short_text(&f.description, 120), false)},
                "fullDescription": {"text": strip_controls(if f.description.is_empty() { "advisor finding" } else { &f.description }, true)},
                "defaultConfiguration": {"level": level_for(&f.severity)},
                "help": {"text": strip_controls(if f.fix.is_empty() { "See advisor output for remediation guidance." } else { &f.fix }, true)},
                "properties": {"tags": [format!("severity:{}", f.severity.to_lowercase())]},
            }));
        }
        let rel = resolve_relative(&file_path, target_dir)?;
        let quoted = url_quote(&rel);

        let mut region = Map::new();
        if let Some(l) = line {
            region.insert("startLine".into(), json!(l.clamp(1, SARIF_INT_MAX)));
            if let Some(c) = start_col {
                region.insert("startColumn".into(), json!(c.clamp(1, SARIF_INT_MAX)));
            }
            if let Some(c) = end_col {
                region.insert("endColumn".into(), json!(c.clamp(1, SARIF_INT_MAX)));
            }
        }

        let mut physical = Map::new();
        physical.insert(
            "artifactLocation".into(),
            json!({"uri": quoted, "uriBaseId": "%SRCROOT%"}),
        );
        if !region.is_empty() {
            physical.insert("region".into(), Value::Object(region));
        }

        let line_tok = match line {
            None => "?".to_string(),
            Some(l) => l.to_string(),
        };
        let mut hasher = Sha1::new();
        hasher.update(format!("{rule_id}|{quoted}|{line_tok}").as_bytes());
        let digest = hasher.finalize();
        let mut head = [0u8; 8];
        head.copy_from_slice(&digest[..8]);
        let fingerprint = format!("{:016x}", u64::from_be_bytes(head));

        let mut props = Map::new();
        props.insert("severity".into(), json!(strip_controls(&f.severity, false)));
        props.insert("evidence".into(), json!(strip_controls(&f.evidence, true)));
        props.insert("fix".into(), json!(strip_controls(&f.fix, true)));
        if !f.expected_vs_actual.is_empty() {
            props.insert(
                "expected_vs_actual".into(),
                json!(strip_controls(&f.expected_vs_actual, true)),
            );
        }

        results.push(json!({
            "ruleId": rule_id,
            "ruleIndex": rule_index_by_id[&rule_id],
            "level": level_for(&f.severity),
            "message": {"text": strip_controls(if f.description.is_empty() { "advisor finding" } else { &f.description }, true)},
            "locations": [{"physicalLocation": Value::Object(physical)}],
            "partialFingerprints": {"primaryLocationLineHash": fingerprint},
            "properties": Value::Object(props),
        }));
    }

    Ok(json!({
        "$schema": SARIF_SCHEMA_URI,
        "version": SARIF_VERSION,
        "runs": [{
            "tool": {"driver": {
                "name": "advisor",
                "version": tool_version,
                "informationUri": "https://github.com/vzwjustin/advisor",
                "rules": rules,
                "properties": {"advisor_schema_version": SCHEMA_VERSION},
            }},
            "originalUriBaseIds": {"%SRCROOT%": {"uri": srcroot_uri(target_dir)}},
            "results": results,
        }],
    }))
}

/// Serialize a JSON value the way the Python CLI does: `json.dumps(indent=2)`
/// (2-space indent, `ensure_ascii=True`).
pub fn to_pretty_json(value: &Value) -> String {
    crate::jsonutil::ensure_ascii(&serde_json::to_string_pretty(value).unwrap_or_default())
}

#[cfg(test)]
mod tests {
    use super::*;

    // Reference values captured from the Python implementation.
    #[test]
    fn rule_id_matches_python() {
        assert_eq!(
            synthesize_rule_id("HIGH", "SQL injection in query builder", "advisor"),
            "advisor/high/d20d606a67707625"
        );
        // Slug is severity-independent; only the severity segment changes.
        assert_eq!(
            synthesize_rule_id("low", "SQL injection in query builder", "advisor"),
            "advisor/low/d20d606a67707625"
        );
    }

    #[test]
    fn level_mapping() {
        assert_eq!(level_for("CRITICAL"), "error");
        assert_eq!(level_for("high"), "error");
        assert_eq!(level_for("MEDIUM"), "warning");
        assert_eq!(level_for("LOW"), "note");
        assert_eq!(level_for("bogus"), "warning");
    }

    fn golden() -> serde_json::Value {
        serde_json::from_str(include_str!("../tests/parity/sarif.json")).unwrap()
    }

    #[test]
    fn parse_file_path_matches_python() {
        let g = golden();
        for (raw, expected) in g["parse_file_path"].as_object().unwrap() {
            let (path, line, sc, ec) = parse_file_path(raw);
            let exp = expected.as_array().unwrap();
            assert_eq!(Value::from(path), exp[0], "raw={raw:?}");
            let to_opt = |v: Option<i64>| v.map(Value::from).unwrap_or(Value::Null);
            assert_eq!(to_opt(line), exp[1], "raw={raw:?} line");
            assert_eq!(to_opt(sc), exp[2], "raw={raw:?} start_col");
            assert_eq!(to_opt(ec), exp[3], "raw={raw:?} end_col");
        }
    }

    #[test]
    fn short_text_matches_python() {
        let g = golden();
        let st = &g["short_text"];
        assert_eq!(
            short_text("a short one", 120),
            st["short"].as_str().unwrap()
        );
        assert_eq!(
            short_text(&"x".repeat(200), 120),
            st["long"].as_str().unwrap()
        );
        assert_eq!(
            short_text("line1\nline2\tcol", 120),
            st["newlines"].as_str().unwrap()
        );
        assert_eq!(short_text("", 120), st["empty"].as_str().unwrap());
    }

    #[test]
    fn strip_controls_matches_python() {
        let g = golden();
        let sc = &g["strip_controls"];
        assert_eq!(
            strip_controls("a\x00b\x07c d", false),
            sc["inline"].as_str().unwrap()
        );
        assert_eq!(
            strip_controls("a\nb\tc\x00d", true),
            sc["block"].as_str().unwrap()
        );
    }

    #[test]
    fn findings_to_sarif_matches_python() {
        let g = golden();
        let f = |fp: &str,
                 sev: &str,
                 desc: &str,
                 ev: &str,
                 fix: &str,
                 rule: Option<&str>,
                 eva: &str| Finding {
            file_path: fp.into(),
            severity: sev.into(),
            description: desc.into(),
            evidence: ev.into(),
            fix: fix.into(),
            rule_id: rule.map(|s| s.into()),
            expected_vs_actual: eva.into(),
        };
        let findings = vec![
            f(
                "src/auth.py:42:5:15",
                "HIGH",
                "SQL injection in query",
                "user input concatenated",
                "use params",
                None,
                "",
            ),
            f(
                "src/auth.py:99",
                "high",
                "SQL injection in query",
                "another spot",
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
            f(
                "a.py:0",
                "medium",
                "file-level note",
                "n/a",
                "n/a",
                None,
                "",
            ),
        ];
        let doc = findings_to_sarif(&findings, "0.8.4", Path::new("/repo"), "advisor").unwrap();
        assert_eq!(to_pretty_json(&doc), g["doc"].as_str().unwrap());
    }
}
