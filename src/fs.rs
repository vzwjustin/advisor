//! Port of path/IO helpers from `advisor/_fs.py`.
//!
//! This slice ports the two pure functions exercised across the codebase:
//! [`normalize_path`] (batch/drift comparison key) and [`validate_file_types`]
//! (reject path-traversal / absolute / NUL globs). The atomic-write and
//! capped-read helpers are tracked in PORT_NOTES.md.

/// Number of bytes scanned per file for keyword ranking (`CONTENT_SCAN_LIMIT`).
pub const CONTENT_SCAN_LIMIT: usize = 1024;

/// Error returned by [`validate_file_types`] — mirrors Python `ValueError`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileTypesError(pub String);

impl std::fmt::Display for FileTypesError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for FileTypesError {}

/// Reject path-traversal, NUL bytes, or absolute-path patterns in a
/// user-supplied `file_types` glob. Each comma-separated sub-pattern is
/// validated independently. Mirrors Python `validate_file_types`.
pub fn validate_file_types(pattern: &str) -> Result<(), FileTypesError> {
    for raw in pattern.split(',') {
        let piece = raw.trim();
        if piece.is_empty() {
            continue;
        }
        if piece.contains('\u{0000}') {
            return Err(FileTypesError(format!(
                "file_types pattern contains NUL byte: {piece:?}"
            )));
        }
        // A `..` segment delimited by either separator (single pass over both).
        let has_dotdot_segment = piece.split(['/', '\\']).any(|seg| seg == "..");
        let starts_absolute = piece.starts_with('/') || piece.starts_with('\\');
        // Leading Windows drive letter, e.g. `C:`.
        let drive_letter = {
            let bytes = piece.as_bytes();
            bytes.len() >= 2 && bytes[1] == b':' && (bytes[0] as char).is_ascii_alphabetic()
        };
        if has_dotdot_segment || starts_absolute || drive_letter {
            return Err(FileTypesError(format!(
                "unsafe file_types pattern: {piece:?}"
            )));
        }
    }
    Ok(())
}

/// Normalize a file path for batch/drift-detection comparison.
///
/// Strips a leading BOM, surrounding whitespace, backticks, leading `./`,
/// converts backslashes to forward slashes, strips a trailing `:line[:col]`
/// suffix (capped at 2 iterations), and lexically collapses `..`/`.`. Does NOT
/// resolve symlinks, make the path absolute, or case-fold. Mirrors Python
/// `normalize_path`.
pub fn normalize_path(path: &str) -> String {
    // p = path.lstrip("﻿").strip().strip("`").strip().replace("\\", "/")
    // Chain the trims on the &str slice so only the final replace allocates.
    let mut p = path
        .trim_start_matches('\u{FEFF}')
        .trim()
        .trim_matches('`')
        .trim()
        .replace('\\', "/");

    // Strip all leading "./" segments.
    while let Some(rest) = p.strip_prefix("./") {
        p = rest.to_string();
    }

    // Strip a trailing :line[:col] suffix, capped at 2 iterations.
    for _ in 0..2 {
        if !p.contains(':') {
            break;
        }
        // rpartition(':') -> (head, sep, tail)
        match p.rsplit_once(':') {
            Some((head, tail))
                if !tail.is_empty()
                    && tail.bytes().all(|b| b.is_ascii_digit())
                    && !head.is_empty() =>
            {
                p = head.to_string();
            }
            _ => break,
        }
    }

    // Lexically collapse "..". and redundant "." — POSIX semantics.
    if !p.is_empty() && p != "." {
        let collapsed = posix_normpath(&p);
        p = if collapsed == "." {
            String::new()
        } else {
            collapsed
        };
    }
    p
}

/// Shared 10 MiB ceiling for the user-controlled `.advisor/` loaders
/// (baseline / checkpoint / suppressions), matching the Python constants.
pub const MAX_ADVISOR_FILE_BYTES: u64 = 10 * 1024 * 1024;

/// Error from [`read_text_capped`] — mirrors the Python exceptions the callers
/// distinguish (missing file, oversized, decode/IO failure).
#[derive(Debug)]
pub enum ReadCappedError {
    NotFound,
    TooLarge(u64),
    Io(std::io::Error),
    Utf8,
}

impl std::fmt::Display for ReadCappedError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ReadCappedError::NotFound => write!(f, "file not found"),
            ReadCappedError::TooLarge(max) => {
                write!(f, "file size exceeds {max} bytes — refusing to load")
            }
            ReadCappedError::Io(e) => write!(f, "{e}"),
            ReadCappedError::Utf8 => write!(f, "invalid UTF-8"),
        }
    }
}

/// Read a text file with a hard byte cap (measured in bytes, not chars).
/// Strips a leading UTF-8 BOM (`utf-8-sig`). Mirrors `_fs.read_text_capped`.
pub fn read_text_capped(path: &std::path::Path, max_bytes: u64) -> Result<String, ReadCappedError> {
    let bytes = match std::fs::read(path) {
        Ok(b) => b,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(ReadCappedError::NotFound)
        }
        Err(e) => return Err(ReadCappedError::Io(e)),
    };
    if bytes.len() as u64 > max_bytes {
        return Err(ReadCappedError::TooLarge(max_bytes));
    }
    let s = String::from_utf8(bytes).map_err(|_| ReadCappedError::Utf8)?;
    Ok(s.strip_prefix('\u{FEFF}')
        .map(|r| r.to_string())
        .unwrap_or(s))
}

/// Atomically write `text` to `target` (temp file in the same directory, then
/// rename). Mirrors `_fs.atomic_write_text` for the default (no symlink/mode)
/// path; the fsync/symlink-rejection nuances are tracked in PORT_NOTES.
pub fn atomic_write_text(target: &std::path::Path, text: &str) -> std::io::Result<()> {
    use std::io::Write;
    let parent = target.parent().unwrap_or_else(|| std::path::Path::new("."));
    std::fs::create_dir_all(parent)?;
    let name = target
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_default();
    let tmp = parent.join(format!(
        ".{name}.{}.{}.tmp",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    {
        let mut fh = std::fs::File::create(&tmp)?;
        fh.write_all(text.as_bytes())?;
        fh.sync_all()?;
    }
    match std::fs::rename(&tmp, target) {
        Ok(()) => Ok(()),
        Err(e) => {
            let _ = std::fs::remove_file(&tmp);
            Err(e)
        }
    }
}

/// Lexical POSIX path normalization, matching Python's `posixpath.normpath`
/// for the inputs `normalize_path` produces (no NUL, already forward-slashed).
pub(crate) fn posix_normpath(path: &str) -> String {
    if path.is_empty() {
        return ".".to_string();
    }
    // Count leading slashes: POSIX normpath preserves exactly one leading
    // slash, or two (but not three+) per the standard.
    let initial_slashes = if path.starts_with('/') {
        if path.starts_with("//") && !path.starts_with("///") {
            2
        } else {
            1
        }
    } else {
        0
    };

    let mut new_comps: Vec<&str> = Vec::new();
    for comp in path.split('/') {
        if comp.is_empty() || comp == "." {
            continue;
        }
        if comp != ".."
            || (initial_slashes == 0 && new_comps.is_empty())
            || (!new_comps.is_empty() && new_comps.last() == Some(&".."))
        {
            new_comps.push(comp);
        } else if !new_comps.is_empty() {
            new_comps.pop();
        }
    }

    let mut result = new_comps.join("/");
    if initial_slashes > 0 {
        let prefix = "/".repeat(initial_slashes);
        result = format!("{prefix}{result}");
    }
    if result.is_empty() {
        ".".to_string()
    } else {
        result
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Reference table captured from the Python implementation.
    #[test]
    fn parity_fs_json() {
        use std::collections::HashMap;
        let raw = std::fs::read_to_string("tests/parity/fs.json").unwrap();
        let v: HashMap<String, serde_json::Value> = serde_json::from_str(&raw).unwrap();
        assert_eq!(CONTENT_SCAN_LIMIT, v["CONTENT_SCAN_LIMIT"].as_u64().unwrap() as usize);
        assert_eq!(normalize_path("foo/bar.py"), v["normalize_no_dot"].as_str().unwrap());
        assert_eq!(normalize_path("./foo/bar.py"), v["normalize_dotslash"].as_str().unwrap());
        assert_eq!(normalize_path("/abs/path.py"), v["normalize_abs_passthrough"].as_str().unwrap());
        assert_eq!(validate_file_types("*.py").is_ok(), v["validate_ok_noerr"].as_bool().unwrap());
        assert_eq!(
            validate_file_types("../evil").is_err(),
            v["validate_traversal_raises"].as_bool().unwrap()
        );
        assert_eq!(
            validate_file_types("/abs/path").is_err(),
            v["validate_absolute_raises"].as_bool().unwrap()
        );
    }

    #[test]
    fn normalize_path_reference_table() {
        let cases = [
            ("./foo.py", "foo.py"),
            ("foo.py:42", "foo.py"),
            ("foo.py:42:10", "foo.py"),
            ("src/../src/auth.py", "src/auth.py"),
            ("foo.py:42:43:44", "foo.py:42"),
            ("C:/Users/x", "C:/Users/x"),
            ("", ""),
            (".", "."),
            ("\u{FEFF}foo.py", "foo.py"),
            ("`a.py`", "a.py"),
        ];
        for (input, expected) in cases {
            assert_eq!(normalize_path(input), expected, "input={input:?}");
        }
    }

    #[test]
    fn validate_rejects_traversal_and_absolute() {
        assert!(validate_file_types("*.py").is_ok());
        assert!(validate_file_types("**/*.py").is_ok());
        assert!(validate_file_types("foo..bar.py").is_ok()); // not a standalone segment
        assert!(validate_file_types("*.py,../etc/*").is_err());
        assert!(validate_file_types("/abs/*.py").is_err());
        assert!(validate_file_types("C:/x.py").is_err());
        assert!(validate_file_types("a/../b").is_err());
    }
}
