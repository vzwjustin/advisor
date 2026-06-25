//! Port of `advisor/rank.py` — priority ranker scoring files by likelihood of
//! containing issues.
//!
//! The Python ranker builds one large alternation regex with `(?<!\w)` /
//! `(?!\w)` lookarounds and uses `finditer` + `lastgroup` for attribution. The
//! Rust `regex` crate has no lookaround, so [`score_file`] reproduces the
//! *observable behavior*: a left-to-right, non-overlapping, first-alternative
//! match simulation with manual word-boundary checks. The two left-anchor forms
//! (`\b` for word-initial keywords and `(?<!\w)` for symbol-initial ones) both
//! reduce to "the character before the match is a non-word char or start"; the
//! right anchor reduces to "non-word/end" except for `_`-terminated prefixes
//! (e.g. `wp_`) which are unanchored on the right.

use std::collections::HashMap;
use std::path::{Path, PathBuf};

use crate::models::RankedFile;

/// Bytes (characters) scanned per file for keyword matching (`CONTENT_SCAN_LIMIT`).
pub const CONTENT_SCAN_LIMIT: usize = 1024;

/// Filename for project-local ignore rules (`ADVISORIGNORE_FILENAME`).
pub const ADVISORIGNORE_FILENAME: &str = ".advisorignore";

/// Hard ceiling on `.advisorignore` size (1 MiB).
const ADVISORIGNORE_MAX_BYTES: u64 = 1024 * 1024;

/// Minimum history score that earns a +1 tier boost (`_HISTORY_BOOST_THRESHOLD`).
const HISTORY_BOOST_THRESHOLD: f64 = 1.5;

/// Glob-quantifier ceiling guarding against ReDoS in the Python engine. The
/// Rust `regex` crate is linear-time so this only governs which patterns are
/// disabled (for parity). Uses the modern (3.12+) value.
const MAX_GLOB_QUANTIFIERS: usize = 8;

/// Base, language-agnostic priority keywords, tiers in declaration order
/// (5 highest → 1 lowest). Mirrors `PRIORITY_KEYWORDS`.
fn base_priority_keywords() -> Vec<(i64, Vec<&'static str>)> {
    vec![
        (
            5,
            vec![
                "auth",
                "login",
                "password",
                "token",
                "session",
                "cookie",
                "oauth",
                "jwt",
                "credential",
                "secret",
                "cert",
                "api_key",
                "private_key",
                "passphrase",
                "hmac",
            ],
        ),
        (
            4,
            vec![
                "input",
                "request",
                "upload",
                "form",
                "parse",
                "serialize",
                "deserialize",
                "deserializer",
                "deserialization",
                "admin",
                "permission",
                "role",
                "access",
            ],
        ),
        (
            3,
            vec![
                "http",
                "api",
                "endpoint",
                "route",
                "handler",
                "middleware",
                "query",
                "sql",
                "database",
                "exec",
                "shell",
                "command",
                "subprocess",
            ],
        ),
        (
            2,
            vec![
                "config", "setting", "env", "cache", "log", "error", "crypto", "encrypt",
                "decrypt", "hash", "sign",
            ],
        ),
        (
            1,
            vec![
                "util", "helper", "constant", "schema", "test", "mock", "fixture",
            ],
        ),
    ]
}

/// Language-specific keyword overlays, keyed by canonical language name.
/// Mirrors `LANGUAGE_EXTRA_KEYWORDS`.
fn language_extra_keywords(language: &str) -> Option<Vec<(i64, Vec<&'static str>)>> {
    let v = match language {
        "python" => vec![
            (5, vec!["passlib", "pyjwt", "itsdangerous"]),
            (
                4,
                vec![
                    "pickle",
                    "yaml.load",
                    "yaml.load_all",
                    "marshal",
                    "pydantic",
                ],
            ),
            (
                3,
                vec![
                    "flask",
                    "django",
                    "fastapi",
                    "sqlalchemy",
                    "psycopg",
                    "pymongo",
                ],
            ),
            (2, vec!["os.environ", "secrets"]),
        ],
        "javascript" => vec![
            (
                5,
                vec!["passport", "next-auth", "nextauth", "firebase.auth"],
            ),
            (
                4,
                vec![
                    "innerhtml",
                    "dangerouslysetinnerhtml",
                    "eval",
                    "document.write",
                    "localstorage",
                    "sessionstorage",
                ],
            ),
            (
                3,
                vec![
                    "express", "fastify", "nextjs", "next.js", "graphql", "prisma", "mongoose",
                ],
            ),
            (2, vec!["dotenv", "process.env"]),
        ],
        "go" => vec![
            (5, vec!["crypto/tls", "crypto/x509", "golang.org/x/oauth2"]),
            (
                4,
                vec![
                    "encoding/json",
                    "encoding/xml",
                    "encoding/gob",
                    "html/template",
                ],
            ),
            (
                3,
                vec!["net/http", "database/sql", "os/exec", "context.background"],
            ),
            (2, vec!["os.getenv"]),
        ],
        "rust" => vec![
            (5, vec!["jsonwebtoken", "argon2", "oauth2"]),
            (
                4,
                vec![
                    "serde_json",
                    "serde_yaml",
                    "unsafe",
                    "transmute",
                    "from_utf8_unchecked",
                ],
            ),
            (
                3,
                vec![
                    "reqwest",
                    "actix_web",
                    "axum",
                    "rocket",
                    "tokio",
                    "sqlx",
                    "diesel",
                ],
            ),
            (2, vec!["std::env"]),
        ],
        "java" => vec![
            (5, vec!["spring.security", "shiro", "jjwt", "keycloak"]),
            (
                4,
                vec!["objectinputstream", "readobject", "xmldecoder", "jackson"],
            ),
            (
                3,
                vec![
                    "restcontroller",
                    "requestmapping",
                    "httpservletrequest",
                    "preparedstatement",
                    "runtime.getruntime",
                ],
            ),
            (2, vec!["system.getenv"]),
        ],
        "ruby" => vec![
            (5, vec!["devise", "omniauth", "warden"]),
            (4, vec!["params", "marshal.load", "yaml.load"]),
            (3, vec!["rails", "rack", "sinatra", "activerecord"]),
        ],
        "php" => vec![
            (5, vec!["password_hash", "password_verify"]),
            (
                4,
                vec!["$_get", "$_post", "$_request", "$_files", "unserialize"],
            ),
            (3, vec!["mysqli", "pdo", "wp_", "laravel", "symfony"]),
            (2, vec!["getenv"]),
        ],
        _ => return None,
    };
    Some(v)
}

/// File-extension → canonical language. Mirrors `EXTENSION_LANGUAGE`.
fn extension_language(suffix_lower: &str) -> Option<&'static str> {
    Some(match suffix_lower {
        ".py" | ".pyi" => "python",
        ".js" | ".jsx" | ".mjs" | ".cjs" | ".ts" | ".tsx" => "javascript",
        ".go" => "go",
        ".rs" => "rust",
        ".java" | ".kt" | ".scala" => "java",
        ".rb" | ".rake" => "ruby",
        ".php" => "php",
        _ => return None,
    })
}

/// Shebang interpreter basename → canonical language. Mirrors `_SHEBANG_INTERPRETERS`.
fn shebang_interpreter(name: &str) -> Option<&'static str> {
    Some(match name {
        "python" | "python2" | "python3" => "python",
        "node" | "deno" | "bun" => "javascript",
        "ruby" => "ruby",
        "php" => "php",
        _ => return None,
    })
}

/// Directory names skipped entirely during ranking (`SKIP_DIRS`).
fn is_skip_dir(part: &str) -> bool {
    matches!(
        part,
        "__pycache__"
            | "node_modules"
            | ".claude"
            | ".git"
            | ".venv"
            | "venv"
            | "dist"
            | "build"
            | ".tox"
            | ".mypy_cache"
            | ".pytest_cache"
            | ".next"
            | ".nuxt"
            | "target"
            | "vendor"
            | ".bundle"
            | ".gradle"
            | ".idea"
            | ".vscode"
            | "coverage"
            | "htmlcov"
            | ".turbo"
    )
}

/// File extensions skipped during ranking (`SKIP_EXTENSIONS`). Compared
/// case-sensitively, matching the Python `p.suffix in SKIP_EXTENSIONS` check.
fn is_skip_extension(suffix: &str) -> bool {
    matches!(
        suffix,
        ".pyc"
            | ".pyo"
            | ".so"
            | ".dylib"
            | ".lock"
            | ".svg"
            | ".png"
            | ".jpg"
            | ".jpeg"
            | ".gif"
            | ".ico"
            | ".woff"
            | ".woff2"
            | ".ttf"
            | ".class"
            | ".jar"
            | ".o"
            | ".a"
            | ".dll"
            | ".exe"
            | ".map"
    )
}

// ── Path helpers mirroring pathlib.PurePath semantics ──────────────

/// Final path component (`PurePath.name`).
fn path_name(path: &str) -> &str {
    path.rsplit('/').find(|s| !s.is_empty()).unwrap_or("")
}

/// File extension including the leading dot (`PurePath.suffix`): `name[i:]` where
/// `i = name.rfind('.')` and `0 < i < len-1`, else empty.
fn path_suffix(path: &str) -> &str {
    let name = path_name(path);
    match name.rfind('.') {
        Some(i) if i > 0 && i < name.len() - 1 => &name[i..],
        _ => "",
    }
}

/// Path components, dropping empties and the root slash (sufficient for the
/// POSIX inputs the ranker sees).
fn path_parts(path: &str) -> Vec<&str> {
    path.split('/').filter(|s| !s.is_empty()).collect()
}

/// Return the canonical language for a file path, or `None`. Mirrors
/// `language_for_path`.
pub fn language_for_path(path: &str) -> Option<&'static str> {
    let suffix = path_suffix(path).to_lowercase();
    extension_language(&suffix)
}

/// Extract a canonical language from a `#!...` line, or `None`. Mirrors
/// `_language_from_shebang`.
fn language_from_shebang(first_line: &str) -> Option<&'static str> {
    let line = first_line.trim_start_matches('\u{FEFF}');
    let rest = line.strip_prefix("#!")?;
    let tokens: Vec<&str> = rest.split_whitespace().collect();
    if tokens.is_empty() {
        return None;
    }
    let mut first = tokens[0].rsplit('/').next().unwrap_or(tokens[0]);
    if first == "env" {
        let mut found = None;
        for tok in &tokens[1..] {
            if tok.starts_with('-') {
                continue;
            }
            found = Some(tok.rsplit('/').next().unwrap_or(tok));
            break;
        }
        first = found?;
    }
    // base = first.split('.', 1)[0]; base_stripped = base.rstrip(digits)
    let base = first.split('.').next().unwrap_or(first);
    let base_stripped = base.trim_end_matches(|c: char| c.is_ascii_digit());
    shebang_interpreter(base)
        .or_else(|| shebang_interpreter(first))
        .or_else(|| shebang_interpreter(base_stripped))
}

// ── Keyword table assembly ─────────────────────────────────────────

/// A flattened keyword with its tier; list position is the pattern-alternation
/// order used to break ties (mirrors the named-group order in the Python regex).
struct KeywordEntry {
    kw: String,
    priority: i64,
}

/// Build the merged keyword table for a language (base + language extras),
/// deduped per tier, tiers in base order. Mirrors `_merged_keywords_for`.
fn merged_keywords_for(language: Option<&str>) -> Vec<(i64, Vec<String>)> {
    let base = base_priority_keywords();
    let extras = language.and_then(language_extra_keywords);
    let Some(extras) = extras else {
        return base
            .into_iter()
            .map(|(p, kws)| (p, kws.into_iter().map(String::from).collect()))
            .collect();
    };
    let extras_map: HashMap<i64, Vec<&'static str>> = extras.iter().cloned().collect();
    let mut merged: Vec<(i64, Vec<String>)> = Vec::new();
    for (priority, kws) in &base {
        let mut seen: Vec<String> = Vec::new();
        for kw in kws
            .iter()
            .chain(extras_map.get(priority).into_iter().flatten())
        {
            let s = (*kw).to_string();
            if !seen.contains(&s) {
                seen.push(s);
            }
        }
        merged.push((*priority, seen));
    }
    // Extra-only tiers (none for shipped languages, but kept for fidelity),
    // appended in the extras' declaration order.
    for (priority, extra) in &extras {
        if !merged.iter().any(|(p, _)| p == priority) {
            let mut seen: Vec<String> = Vec::new();
            for kw in extra {
                let s = (*kw).to_string();
                if !seen.contains(&s) {
                    seen.push(s);
                }
            }
            merged.push((*priority, seen));
        }
    }
    merged
}

/// Build the flattened keyword list for a language with an optional preset
/// overlay. Mirrors the merged table built in `_regex_with_extras_cached` /
/// `_combined_regex_for`.
fn build_keyword_list(
    language: Option<&str>,
    extra: Option<&[(i64, Vec<String>)]>,
) -> Vec<KeywordEntry> {
    let mut merged = merged_keywords_for(language);
    if let Some(extra) = extra {
        let extra_map: HashMap<i64, &Vec<String>> = extra.iter().map(|(p, v)| (*p, v)).collect();
        for (priority, kws) in merged.iter_mut() {
            if let Some(ex) = extra_map.get(priority) {
                for kw in ex.iter() {
                    if !kws.contains(kw) {
                        kws.push(kw.clone());
                    }
                }
            }
        }
        for (priority, ex) in extra {
            if !merged.iter().any(|(p, _)| p == priority) {
                let mut seen: Vec<String> = Vec::new();
                for kw in ex {
                    if !seen.contains(kw) {
                        seen.push(kw.clone());
                    }
                }
                merged.push((*priority, seen));
            }
        }
    }
    let mut out = Vec::new();
    for (priority, kws) in merged {
        for kw in kws {
            out.push(KeywordEntry { kw, priority });
        }
    }
    out
}

/// Word character per Python `\w` (Unicode alphanumeric or underscore).
fn is_word(c: char) -> bool {
    c.is_alphanumeric() || c == '_'
}

/// Score a single file by path + content keywords. Returns `(priority, reasons)`
/// with reasons attributed only to the winning tier, in first-match order.
/// Mirrors `_score_file`.
fn score_file(path: &str, content: &str, keyword_list: &[KeywordEntry]) -> (i64, Vec<String>) {
    // combined = path[:256] + content[:CONTENT_SCAN_LIMIT]; large files also
    // append a trailing window so late-file auth keywords are not missed.
    const TAIL_EXTRA: usize = 256;
    let content_chars: Vec<char> = content.chars().collect();
    let mut combined: Vec<char> = path.chars().take(256).collect();
    combined.push(' ');
    combined.extend(content_chars.iter().take(CONTENT_SCAN_LIMIT).copied());
    if content_chars.len() > CONTENT_SCAN_LIMIT {
        combined.push(' ');
        combined.extend(
            content_chars[content_chars.len().saturating_sub(TAIL_EXTRA)..]
                .iter()
                .copied(),
        );
    }

    // Collect every boundary-valid candidate match.
    struct Cand {
        start: usize,
        end: usize,
        order: usize,
        priority: i64,
        kw_index: usize,
    }
    let mut cands: Vec<Cand> = Vec::new();
    for (order, e) in keyword_list.iter().enumerate() {
        let kw: Vec<char> = e.kw.chars().collect();
        if kw.is_empty() || kw.len() > combined.len() {
            continue;
        }
        let ends_underscore = e.kw.ends_with('_');
        let last = combined.len() - kw.len();
        for start in 0..=last {
            let mut matched = true;
            for (k, &kc) in kw.iter().enumerate() {
                if combined[start + k].to_ascii_lowercase() != kc {
                    matched = false;
                    break;
                }
            }
            if !matched {
                continue;
            }
            let left_ok = start == 0 || !is_word(combined[start - 1]);
            let end = start + kw.len();
            let right_ok = ends_underscore || end == combined.len() || !is_word(combined[end]);
            if left_ok && right_ok {
                cands.push(Cand {
                    start,
                    end,
                    order,
                    priority: e.priority,
                    kw_index: order,
                });
            }
        }
    }

    // Simulate finditer: repeatedly take the leftmost candidate (ties broken by
    // alternation order), then advance past its end.
    let mut pos = 0usize;
    let mut result: Vec<(i64, usize)> = Vec::new();
    loop {
        let mut best: Option<&Cand> = None;
        for c in &cands {
            if c.start < pos {
                continue;
            }
            match best {
                None => best = Some(c),
                Some(b) => {
                    if c.start < b.start || (c.start == b.start && c.order < b.order) {
                        best = Some(c);
                    }
                }
            }
        }
        match best {
            None => break,
            Some(b) => {
                result.push((b.priority, b.kw_index));
                pos = b.end;
            }
        }
    }

    if result.is_empty() {
        return (1, Vec::new());
    }
    let best_priority = result.iter().map(|(p, _)| *p).max().unwrap_or(1);
    let mut reasons: Vec<String> = Vec::new();
    for (p, idx) in &result {
        if *p == best_priority {
            let kw = &keyword_list[*idx].kw;
            if !reasons.iter().any(|r| r == kw) {
                reasons.push(kw.clone());
            }
        }
    }
    (best_priority, reasons)
}

/// True for conventional test files/dirs that should not outrank source.
/// Mirrors `_is_test_path`.
fn is_test_path(path: &str) -> bool {
    let parts_lower: Vec<String> = path_parts(path).iter().map(|p| p.to_lowercase()).collect();
    let name = path_name(path).to_lowercase();
    parts_lower
        .iter()
        .any(|p| p == "tests" || p == "test" || p == "__tests__")
        || name.starts_with("test_")
        || name.ends_with("_test.py")
        || name.ends_with(".test.js")
        || name.ends_with(".test.ts")
        || name.ends_with(".spec.js")
        || name.ends_with(".spec.ts")
}

// ── History boost ──────────────────────────────────────────────────

fn normalize_history_key(path: &str) -> String {
    let mut s = path.trim_start_matches('\u{FEFF}').trim().to_string();
    s = s.trim_matches('`').to_string();
    s = s.replace('\\', "/");
    while let Some(rest) = s.strip_prefix("./") {
        s = rest.to_string();
    }
    s
}

fn is_repo_relative_history_key(key: &str) -> bool {
    if !key.contains('/') {
        return false;
    }
    if key.starts_with('/') {
        return false;
    }
    // ^[A-Za-z]:/ (Windows drive) → not repo-relative.
    let b = key.as_bytes();
    if b.len() >= 3 && (b[0] as char).is_ascii_alphabetic() && b[1] == b':' && b[2] == b'/' {
        return false;
    }
    true
}

/// Collect history values matching `file_path` (exact aliases first, then
/// repo-relative suffix matches). Mirrors `_history_values_for`.
fn history_values_for<T: Copy>(file_path: &str, values_by_path: &HashMap<String, T>) -> Vec<T> {
    let path_norm = normalize_history_key(file_path);
    // Exact candidates: dedup of [file_path, posix-collapsed, normalized].
    let mut exact: Vec<String> = Vec::new();
    for c in [
        file_path.to_string(),
        normalize_history_key(file_path),
        path_norm.clone(),
    ] {
        if !exact.contains(&c) {
            exact.push(c);
        }
    }
    let mut matches: Vec<T> = Vec::new();
    for c in &exact {
        if let Some(v) = values_by_path.get(c) {
            matches.push(*v);
        }
    }
    for (key, value) in values_by_path {
        let key_norm = normalize_history_key(key);
        if !is_repo_relative_history_key(&key_norm) {
            continue;
        }
        if path_norm == key_norm || path_norm.ends_with(&format!("/{key_norm}")) {
            matches.push(*value);
        }
    }
    matches
}

fn history_boost(file_path: &str, history_scores: &HashMap<String, f64>) -> f64 {
    let best = history_values_for(file_path, history_scores)
        .into_iter()
        .fold(0.0_f64, f64::max);
    if best < HISTORY_BOOST_THRESHOLD {
        0.0
    } else {
        best
    }
}

fn history_count_for(file_path: &str, history_counts: &HashMap<String, i64>) -> i64 {
    history_values_for(file_path, history_counts)
        .into_iter()
        .max()
        .unwrap_or(0)
}

/// Callback that returns a file's content for ranking (`None` → treated as
/// empty). Mirrors Python's `read_fn` parameter.
pub type ReadFn<'a> = dyn Fn(&str) -> Option<String> + 'a;

/// Rank files by vulnerability likelihood, highest priority first. Mirrors
/// `rank_files` (serial reads — the Python thread pool is a perf detail that
/// does not affect output ordering).
#[allow(clippy::too_many_arguments)]
pub fn rank_files(
    file_paths: &[String],
    read_fn: Option<&ReadFn<'_>>,
    ignore_patterns: &[String],
    extra_keywords: Option<&[(i64, Vec<String>)]>,
    history_scores: Option<&HashMap<String, f64>>,
    history_counts: Option<&HashMap<String, i64>>,
    history_window_days: i64,
) -> Vec<RankedFile> {
    rank_files_inner(
        file_paths,
        read_fn,
        ignore_patterns,
        extra_keywords,
        history_scores,
        history_counts,
        history_window_days,
        None,
    )
}

#[allow(clippy::too_many_arguments)]
/// Rank files while applying `.advisorignore` slash-bearing rules against paths
/// relative to `target_base`. Discovered files are often absolute; ignore files
/// are project-relative, so callers that know the scan root should use this.
pub fn rank_files_with_base(
    file_paths: &[String],
    read_fn: Option<&ReadFn<'_>>,
    ignore_patterns: &[String],
    extra_keywords: Option<&[(i64, Vec<String>)]>,
    history_scores: Option<&HashMap<String, f64>>,
    history_counts: Option<&HashMap<String, i64>>,
    history_window_days: i64,
    target_base: &Path,
) -> Vec<RankedFile> {
    rank_files_inner(
        file_paths,
        read_fn,
        ignore_patterns,
        extra_keywords,
        history_scores,
        history_counts,
        history_window_days,
        Some(target_base),
    )
}

#[allow(clippy::too_many_arguments)]
fn rank_files_inner(
    file_paths: &[String],
    read_fn: Option<&ReadFn<'_>>,
    ignore_patterns: &[String],
    extra_keywords: Option<&[(i64, Vec<String>)]>,
    history_scores: Option<&HashMap<String, f64>>,
    history_counts: Option<&HashMap<String, i64>>,
    history_window_days: i64,
    target_base: Option<&Path>,
) -> Vec<RankedFile> {
    let matchers = compile_ignore_patterns(ignore_patterns);
    let target_base = target_base.map(normalize_base_for_ignore);

    let mut kept: Vec<&String> = Vec::new();
    for fp in file_paths {
        if path_parts(fp).iter().any(|p| is_skip_dir(p)) {
            continue;
        }
        let suffix = path_suffix(fp);
        let name = path_name(fp);
        if is_skip_extension(suffix)
            || [".min.js", ".min.mjs", ".min.cjs", ".min.css"]
                .iter()
                .any(|s| name.ends_with(s))
        {
            continue;
        }
        if matches_compiled(fp, &matchers)
            || target_base
                .as_ref()
                .and_then(|base| relative_to_base_for_ignore(fp, base))
                .is_some_and(|rel| matches_compiled(&rel, &matchers))
        {
            continue;
        }
        kept.push(fp);
    }
    if kept.is_empty() {
        return Vec::new();
    }

    let use_extra = extra_keywords.is_some_and(|e| !e.is_empty());
    let mut ranked: Vec<RankedFile> = Vec::with_capacity(kept.len());
    for fp in kept {
        let content = match read_fn {
            Some(f) => f(fp).unwrap_or_default(),
            None => String::new(),
        };
        let mut language = language_for_path(fp).map(|s| s.to_string());
        if language.is_none() && !content.is_empty() {
            let first_line = content.split('\n').next().unwrap_or(&content);
            language = language_from_shebang(first_line).map(|s| s.to_string());
        }
        let keyword_list = build_keyword_list(
            language.as_deref(),
            if use_extra { extra_keywords } else { None },
        );
        let (mut priority, mut reasons) = score_file(fp, &content, &keyword_list);

        if is_test_path(fp) {
            priority = priority.min(1);
            reasons = vec!["test file".to_string()];
        }
        if let Some(scores) = history_scores.filter(|_| !is_test_path(fp)) {
            let boost = history_boost(fp, scores);
            if boost > 0.0 {
                let boosted = (priority + 1).min(5);
                if boosted > priority {
                    priority = boosted;
                }
                let mut count_label = String::new();
                if let Some(counts) = history_counts {
                    let n = history_count_for(fp, counts);
                    if n > 0 {
                        let plural = if n != 1 { "s" } else { "" };
                        count_label =
                            format!(": {n} finding{plural} in last {history_window_days}d");
                    }
                }
                reasons.push(format!("repeat offender{count_label}"));
            }
        }
        ranked.push(RankedFile {
            path: fp.clone(),
            priority: priority as u8,
            reasons,
        });
    }

    // Sort by priority desc, then path asc (stable).
    ranked.sort_by(|a, b| {
        b.priority
            .cmp(&a.priority)
            .then_with(|| a.path.cmp(&b.path))
    });
    ranked
}

fn normalize_base_for_ignore(base: &Path) -> PathBuf {
    base.to_path_buf()
}

fn relative_to_base_for_ignore(file_path: &str, base: &Path) -> Option<String> {
    let path = Path::new(file_path);
    let rel = path.strip_prefix(base).ok()?;
    let rel = rel.to_string_lossy().replace('\\', "/");
    if rel.is_empty() {
        None
    } else {
        Some(rel)
    }
}

/// Format ranked files into a prompt-ready priority list. Mirrors `rank_to_prompt`.
pub fn rank_to_prompt(ranked: &[RankedFile], top_n: usize) -> String {
    let mut lines = vec![
        "## File Priority Ranking".to_string(),
        "_P5 = highest risk · P1 = lowest. Reasons are the keywords that drove the score._"
            .to_string(),
        String::new(),
    ];
    for (i, rf) in ranked.iter().take(top_n).enumerate() {
        let reasons_str = if rf.reasons.is_empty() {
            "general".to_string()
        } else {
            rf.reasons.join(", ")
        };
        lines.push(format!(
            "{}. **P{}** `{}` — {}",
            i + 1,
            rf.priority,
            rf.path,
            reasons_str
        ));
    }
    if ranked.len() > top_n && top_n > 0 {
        lines.push(format!(
            "\n_(Showing top {} of {} ranked files)_",
            top_n,
            ranked.len()
        ));
    }
    lines.join("\n")
}

// ── .advisorignore glob engine ─────────────────────────────────────

/// Load ignore patterns from a `.advisorignore` file in `base_dir`, or an empty
/// list if absent. Mirrors `load_advisorignore`.
pub fn load_advisorignore(base_dir: &str) -> Vec<String> {
    let path = std::path::Path::new(base_dir).join(ADVISORIGNORE_FILENAME);
    let meta = match std::fs::metadata(&path) {
        Ok(m) => m,
        Err(_) => return Vec::new(), // missing file is expected; no warning
    };
    if meta.len() > ADVISORIGNORE_MAX_BYTES {
        eprintln!(
            "{}: {} bytes (>{}); refusing to load — treating as no ignore patterns",
            path.display(),
            meta.len(),
            ADVISORIGNORE_MAX_BYTES
        );
        return Vec::new();
    }
    let raw = match std::fs::read(&path) {
        Ok(b) => b,
        Err(exc) => {
            eprintln!(
                "could not read {}: {exc}; treating as no ignore patterns",
                path.display()
            );
            return Vec::new();
        }
    };
    // utf-8-sig: strip a leading BOM if present.
    let text = String::from_utf8_lossy(&raw);
    let text = text.strip_prefix('\u{FEFF}').unwrap_or(&text);
    let mut patterns = Vec::new();
    for line in text.lines() {
        let stripped = line.trim();
        if stripped.is_empty() || stripped.starts_with('#') {
            continue;
        }
        if stripped.starts_with('!') {
            eprintln!(
                "{}: negation pattern {stripped:?} is not supported and will be ignored",
                path.display()
            );
            continue;
        }
        if stripped.starts_with('/') {
            eprintln!(
                "{}: anchored pattern {stripped:?} is not fully supported — matching unanchored",
                path.display()
            );
        }
        patterns.push(stripped.trim_start_matches('/').to_string());
    }
    patterns
}

/// Count glob quantifiers; returns false when over the cap. Mirrors
/// `_check_quantifier_count` (returns the rejection decision rather than raising).
fn quantifier_ok(pattern: &str, limit: usize) -> bool {
    let chars: Vec<char> = pattern.chars().collect();
    let mut count = 0usize;
    let mut j = 0usize;
    while j < chars.len() {
        let ch = chars[j];
        if ch == '[' {
            match chars[j..].iter().position(|&c| c == ']') {
                Some(off) => {
                    j += off + 1;
                }
                None => {
                    j += 1;
                }
            }
            continue;
        }
        if ch == '*' {
            count += 1;
            if j + 1 < chars.len() && chars[j + 1] == '*' {
                j += 1;
            }
        } else if ch == '?' {
            count += 1;
        }
        j += 1;
    }
    count <= limit
}

/// Translate a glob with `**` into a whole-path regex string. Mirrors
/// `_double_star_to_regex`. Returns `None` when the quantifier cap is exceeded.
fn double_star_to_regex(pattern: &str) -> Option<String> {
    if !quantifier_ok(pattern, MAX_GLOB_QUANTIFIERS) {
        return None;
    }
    let chars: Vec<char> = pattern.chars().collect();
    let mut parts = String::from("^");
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c == '*' {
            if i + 1 < chars.len() && chars[i + 1] == '*' {
                if i + 2 < chars.len() && chars[i + 2] == '/' {
                    parts.push_str("(?:.*/)?");
                    i += 3;
                } else {
                    parts.push_str(".*");
                    i += 2;
                }
            } else {
                parts.push_str("[^/]*");
                i += 1;
            }
        } else if c == '?' {
            parts.push_str("[^/]");
            i += 1;
        } else if c == '[' {
            push_char_class(&chars, &mut i, &mut parts);
        } else {
            parts.push_str(&regex::escape(&c.to_string()));
            i += 1;
        }
    }
    parts.push('$');
    Some(parts)
}

/// Translate a slash-bearing glob (no `**`) into a path-aware regex string.
/// Mirrors `_slash_pattern_to_regex`.
fn slash_pattern_to_regex(pattern: &str) -> Option<String> {
    if !quantifier_ok(pattern, MAX_GLOB_QUANTIFIERS) {
        return None;
    }
    let chars: Vec<char> = pattern.chars().collect();
    let mut parts = String::from("^");
    let mut j = 0;
    while j < chars.len() {
        let pc = chars[j];
        if pc == '*' {
            parts.push_str("[^/]*");
            j += 1;
        } else if pc == '?' {
            parts.push_str("[^/]");
            j += 1;
        } else if pc == '[' {
            push_char_class(&chars, &mut j, &mut parts);
        } else {
            parts.push_str(&regex::escape(&pc.to_string()));
            j += 1;
        }
    }
    parts.push('$');
    Some(parts)
}

/// Shared char-class handling for the two slash-aware translators. Advances `i`
/// and appends to `parts`, mirroring the Python char-class branch.
fn push_char_class(chars: &[char], i: &mut usize, parts: &mut String) {
    let start = *i;
    match chars[start..].iter().position(|&c| c == ']') {
        None => {
            parts.push_str(&regex::escape("["));
            *i += 1;
        }
        Some(off) => {
            let end = start + off;
            let body: String = chars[start + 1..end].iter().collect();
            if body.is_empty() {
                parts.push_str(&regex::escape("["));
                *i += 1;
                return;
            }
            let body = if let Some(rest) = body.strip_prefix('!') {
                format!("^{rest}")
            } else {
                body
            };
            if body == "^" || body.is_empty() {
                let literal: String = chars[start..=end].iter().collect();
                parts.push_str(&regex::escape(&literal));
            } else {
                parts.push('[');
                parts.push_str(&body.replace('[', "\\["));
                parts.push(']');
            }
            *i = end + 1;
        }
    }
}

/// Translate an fnmatch pattern into an anchored regex string (Python `fnmatch`
/// semantics: `*` matches anything incl. `/`, `?` any char, `[seq]` a class).
fn fnmatch_translate(pattern: &str) -> String {
    let chars: Vec<char> = pattern.chars().collect();
    let mut res = String::from("(?s)^");
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        i += 1;
        match c {
            '*' => res.push_str(".*"),
            '?' => res.push('.'),
            '[' => {
                let mut j = i;
                if j < chars.len() && chars[j] == '!' {
                    j += 1;
                }
                if j < chars.len() && chars[j] == ']' {
                    j += 1;
                }
                while j < chars.len() && chars[j] != ']' {
                    j += 1;
                }
                if j >= chars.len() {
                    res.push_str("\\[");
                } else {
                    let mut stuff: String = chars[i..j].iter().collect();
                    stuff = stuff.replace('\\', "\\\\");
                    // Mirror CPython fnmatch.translate: '!' negates; a leading
                    // '^' or '[' is a literal and must be backslash-escaped.
                    let rendered = if let Some(rest) = stuff.strip_prefix('!') {
                        format!("^{rest}")
                    } else if stuff.starts_with('^') || stuff.starts_with('[') {
                        format!("\\{stuff}")
                    } else {
                        stuff
                    };
                    res.push('[');
                    res.push_str(&rendered);
                    res.push(']');
                    i = j + 1;
                }
            }
            other => res.push_str(&regex::escape(&other.to_string())),
        }
    }
    res.push_str("\\z");
    res
}

/// Compile a `**`-aware glob to a whole-path regex (mirrors
/// `_double_star_to_regex`), or `None` if the quantifier cap trips or the
/// translated regex fails to compile. Shared with the suppressions matcher.
pub fn try_double_star_regex(pattern: &str) -> Option<regex::Regex> {
    double_star_to_regex(pattern).and_then(|s| regex::Regex::new(&s).ok())
}

/// Match a filename against a single fnmatch glob (Python `fnmatch.fnmatch`
/// semantics, used by `--file-types` discovery and git-scope filtering).
pub fn fnmatch_match(name: &str, pattern: &str) -> bool {
    match regex::Regex::new(&fnmatch_translate(pattern)) {
        Ok(re) => re.is_match(name),
        Err(_) => false,
    }
}

/// Match a discovered file against a `--file-types` pattern using basename and,
/// when the pattern contains path segments, the repo-relative path.
pub fn file_matches_pattern(relative_path: &str, basename: &str, pattern: &str) -> bool {
    if fnmatch_match(basename, pattern) {
        return true;
    }
    if pattern.contains('/') || pattern.contains("**") {
        if let Some(re) = try_double_star_regex(pattern) {
            return re.is_match(relative_path);
        }
        return fnmatch_match(relative_path, pattern);
    }
    false
}

/// True when any comma-separated pattern matches the file.
pub fn path_matches_file_types(relative_path: &str, basename: &str, patterns: &[&str]) -> bool {
    patterns
        .iter()
        .any(|pat| file_matches_pattern(relative_path, basename, pat))
}

/// Never-match regex used as the inert fallback (mirrors `re.compile(r"$.^")`).
fn never_match() -> regex::Regex {
    regex::Regex::new(r"\z.\A").expect("never-match regex is valid")
}

fn compile_or_inert(pat_src: Option<String>) -> Option<regex::Regex> {
    match pat_src {
        None => Some(never_match()), // quantifier cap tripped
        Some(s) => Some(regex::Regex::new(&s).unwrap_or_else(|_| never_match())),
    }
}

/// One preprocessed `.advisorignore` matcher. Mirrors `_IgnorePatternMatcher`.
struct Matcher {
    recursive_re: Option<regex::Regex>,
    slash_re: Option<regex::Regex>,
    dir_re: Option<regex::Regex>,
    filename_re: Option<regex::Regex>,
    bare_re: Option<regex::Regex>,
}

/// Compile ignore patterns once. Mirrors `_compile_ignore_patterns`.
fn compile_ignore_patterns(patterns: &[String]) -> Vec<Matcher> {
    let mut out = Vec::with_capacity(patterns.len());
    for pattern in patterns {
        let recursive_re = if pattern.contains("**") {
            let src = pattern.strip_suffix('/').unwrap_or(pattern);
            compile_or_inert(double_star_to_regex(src))
        } else {
            None
        };

        let slash_re =
            if pattern.contains('/') && !pattern.contains("**") && !pattern.ends_with('/') {
                compile_or_inert(slash_pattern_to_regex(pattern))
            } else {
                None
            };

        let dir_re = if pattern.ends_with('/') && !pattern.contains("**") {
            let trimmed = pattern.trim_end_matches('/');
            if quantifier_ok(trimmed, MAX_GLOB_QUANTIFIERS) {
                Some(
                    regex::Regex::new(&fnmatch_translate(trimmed))
                        .unwrap_or_else(|_| never_match()),
                )
            } else {
                Some(never_match())
            }
        } else {
            None
        };

        let filename_re = if dir_re.is_none()
            && recursive_re.is_none()
            && slash_re.is_none()
            && pattern.chars().any(|c| matches!(c, '*' | '?' | '[' | '.'))
        {
            if quantifier_ok(pattern, MAX_GLOB_QUANTIFIERS) {
                Some(
                    regex::Regex::new(&fnmatch_translate(pattern))
                        .unwrap_or_else(|_| never_match()),
                )
            } else {
                Some(never_match())
            }
        } else {
            None
        };

        let bare_re = if filename_re.is_none()
            && dir_re.is_none()
            && !pattern
                .chars()
                .any(|c| matches!(c, '*' | '?' | '[' | '.' | '/'))
        {
            Some(regex::Regex::new(&fnmatch_translate(pattern)).unwrap_or_else(|_| never_match()))
        } else {
            None
        };

        out.push(Matcher {
            recursive_re,
            slash_re,
            dir_re,
            filename_re,
            bare_re,
        });
    }
    out
}

/// Check whether `file_path` matches any compiled matcher. Mirrors
/// `_matches_compiled_pattern`.
fn matches_compiled(file_path: &str, matchers: &[Matcher]) -> bool {
    if matchers.is_empty() {
        return false;
    }
    let path_str = file_path.replace('\\', "/");
    let parts = path_parts(&path_str);
    let name = path_name(&path_str);
    for m in matchers {
        if let Some(re) = &m.dir_re {
            if parts.iter().any(|p| re.is_match(p)) {
                return true;
            }
            continue;
        }
        if let Some(re) = &m.recursive_re {
            if re.is_match(&path_str) {
                return true;
            }
            continue;
        }
        if let Some(re) = &m.filename_re {
            if re.is_match(name) {
                return true;
            }
        }
        if let Some(re) = &m.slash_re {
            if re.is_match(&path_str) {
                return true;
            }
        }
        if let Some(re) = &m.bare_re {
            if parts.iter().any(|p| re.is_match(p)) {
                return true;
            }
        }
    }
    false
}

/// Compatibility wrapper: compile `patterns` and test `file_path`. Mirrors
/// `_matches_any_pattern`.
pub fn matches_any_pattern(file_path: &str, patterns: &[String]) -> bool {
    matches_compiled(file_path, &compile_ignore_patterns(patterns))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;

    fn golden() -> Value {
        serde_json::from_str(include_str!("../tests/parity/rank.json")).expect("valid golden json")
    }

    #[test]
    fn fnmatch_char_class_matches_cpython() {
        // A leading '^' inside a class is a literal in Python fnmatch, NOT a
        // negation: fnmatch.translate('[^py]') -> '[\^py]'. Regression for a
        // port bug that rendered it as a negated class.
        assert!(fnmatch_match("^", "[^py]"));
        assert!(fnmatch_match("p", "[^py]"));
        assert!(!fnmatch_match("a", "[^py]"));
        // '!' still negates, as in fnmatch.
        assert!(!fnmatch_match("p", "[!py]"));
        assert!(fnmatch_match("a", "[!py]"));
    }

    #[test]
    fn language_for_path_matches_python() {
        let g = golden();
        for (path, expected) in g["language_for_path"].as_object().unwrap() {
            let got = language_for_path(path);
            match expected {
                Value::Null => assert_eq!(got, None, "path={path}"),
                Value::String(s) => assert_eq!(got, Some(s.as_str()), "path={path}"),
                _ => unreachable!(),
            }
        }
    }

    #[test]
    fn shebang_matches_python() {
        let g = golden();
        for (line, expected) in g["shebang"].as_object().unwrap() {
            let got = language_from_shebang(line);
            match expected {
                Value::Null => assert_eq!(got, None, "line={line:?}"),
                Value::String(s) => assert_eq!(got, Some(s.as_str()), "line={line:?}"),
                _ => unreachable!(),
            }
        }
    }

    #[test]
    fn score_file_matches_python() {
        let g = golden();
        for case in g["score_file"].as_array().unwrap() {
            let path = case["path"].as_str().unwrap();
            let content = case["content"].as_str().unwrap();
            let language = language_for_path(path).map(|s| s.to_string()).or_else(|| {
                if content.is_empty() {
                    None
                } else {
                    let fl = content.split('\n').next().unwrap_or(content);
                    language_from_shebang(fl).map(|s| s.to_string())
                }
            });
            let kw = build_keyword_list(language.as_deref(), None);
            let (priority, reasons) = score_file(path, content, &kw);
            let exp_priority = case["priority"].as_i64().unwrap();
            let exp_reasons: Vec<String> = case["reasons"]
                .as_array()
                .unwrap()
                .iter()
                .map(|r| r.as_str().unwrap().to_string())
                .collect();
            assert_eq!(priority, exp_priority, "path={path}");
            assert_eq!(reasons, exp_reasons, "path={path}");
        }
    }

    #[test]
    fn score_file_extra_matches_python() {
        let g = golden();
        let extra = vec![(5i64, vec!["csrf".to_string(), "jwt".to_string()])];
        let kw = build_keyword_list(Some("python"), Some(&extra));
        let (priority, reasons) = score_file("a.py", "csrf check here", &kw);
        assert_eq!(
            priority,
            g["score_file_extra"]["priority"].as_i64().unwrap()
        );
        let exp: Vec<String> = g["score_file_extra"]["reasons"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r.as_str().unwrap().to_string())
            .collect();
        assert_eq!(reasons, exp);
    }

    #[test]
    fn is_test_path_matches_python() {
        let g = golden();
        for (path, expected) in g["is_test_path"].as_object().unwrap() {
            assert_eq!(
                is_test_path(path),
                expected.as_bool().unwrap(),
                "path={path}"
            );
        }
    }

    fn files_fixture() -> HashMap<String, String> {
        [
            ("src/auth.py", "def login(password): token=1"),
            ("src/util.py", "def helper(): pass"),
            ("tests/test_auth.py", "password='hunter2'"),
            ("api/routes.py", "@route\ndef handler(): query(sql)"),
            ("skip/node_modules/x.py", "auth"),
            ("img.png", "auth"),
        ]
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect()
    }

    #[test]
    fn rank_files_matches_python() {
        let g = golden();
        let files = files_fixture();
        let paths: Vec<String> = files.keys().cloned().collect();
        let read = |p: &str| files.get(p).cloned();
        let ranked = rank_files(&paths, Some(&read), &[], None, None, None, 90);
        let got: Vec<Value> = ranked
            .iter()
            .map(|r| {
                serde_json::json!({
                    "path": r.path,
                    "priority": r.priority,
                    "reasons": r.reasons,
                })
            })
            .collect();
        assert_eq!(Value::Array(got), g["rank_files"]);
        assert_eq!(
            rank_to_prompt(&ranked, 3),
            g["rank_to_prompt"].as_str().unwrap()
        );
    }

    #[test]
    fn rank_files_history_matches_python() {
        let g = golden();
        let files = files_fixture();
        let read = |p: &str| Some(files.get(p).cloned().unwrap_or_default());
        let mut scores = HashMap::new();
        scores.insert("src/util.py".to_string(), 2.0);
        let mut counts = HashMap::new();
        counts.insert("src/util.py".to_string(), 3);
        let paths = vec!["src/util.py".to_string(), "src/auth.py".to_string()];
        let ranked = rank_files(
            &paths,
            Some(&read),
            &[],
            None,
            Some(&scores),
            Some(&counts),
            90,
        );
        let got: Vec<Value> = ranked
            .iter()
            .map(|r| serde_json::json!({"path": r.path, "priority": r.priority, "reasons": r.reasons}))
            .collect();
        assert_eq!(Value::Array(got), g["rank_files_history"]);
    }

    #[test]
    fn ignore_matches_python() {
        let g = golden();
        for case in g["ignore"].as_array().unwrap() {
            let path = case["path"].as_str().unwrap();
            let patterns: Vec<String> = case["patterns"]
                .as_array()
                .unwrap()
                .iter()
                .map(|p| p.as_str().unwrap().to_string())
                .collect();
            let expected = case["match"].as_bool().unwrap();
            assert_eq!(
                matches_any_pattern(path, &patterns),
                expected,
                "case={case}"
            );
        }
    }

    #[test]
    fn slash_advisorignore_matches_absolute_paths_relative_to_target() {
        let tmp = tempfile::TempDir::new().unwrap();
        let src = tmp.path().join("src");
        std::fs::create_dir_all(&src).unwrap();
        let secret = src.join("secret.py");
        let public = src.join("public.py");
        std::fs::write(&secret, "password = 'x'").unwrap();
        std::fs::write(&public, "password = 'x'").unwrap();

        let paths = vec![
            secret.to_string_lossy().to_string(),
            public.to_string_lossy().to_string(),
        ];
        let read = |p: &str| std::fs::read_to_string(p).ok();
        let ranked = rank_files_with_base(
            &paths,
            Some(&read),
            &["src/secret.py".to_string()],
            None,
            None,
            None,
            90,
            tmp.path(),
        );

        let ranked_paths: Vec<String> = ranked.iter().map(|r| r.path.replace('\\', "/")).collect();
        assert!(ranked_paths.iter().any(|p| p.ends_with("src/public.py")));
        assert!(!ranked_paths.iter().any(|p| p.ends_with("src/secret.py")));
    }
}
