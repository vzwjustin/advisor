//! Port of path/IO helpers from `advisor/_fs.py`.
//!
//! This slice ports the two pure functions exercised across the codebase:
//! [`normalize_path`] (batch/drift comparison key) and [`validate_file_types`]
//! (reject path-traversal / absolute / NUL globs). The atomic-write and
//! capped-read helpers are tracked in PORT_NOTES.md.

use std::sync::atomic::{AtomicBool, Ordering};

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
    use std::io::Read as _;
    let file = match std::fs::File::open(path) {
        Ok(f) => f,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(ReadCappedError::NotFound)
        }
        Err(e) => return Err(ReadCappedError::Io(e)),
    };
    let mut bytes = Vec::new();
    file.take(max_bytes + 1)
        .read_to_end(&mut bytes)
        .map_err(ReadCappedError::Io)?;
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
static LOCK_UNAVAILABLE_WARNED: AtomicBool = AtomicBool::new(false);

/// Whether an advisory-lock failure is tolerable (NFS, Windows CI temp dirs, etc.).
/// Mirrors Python `_lock_exclusive` continuing unlocked after `ENOLCK`/`ENOSYS`.
fn lock_error_tolerable(err: &std::io::Error) -> bool {
    if let Some(code) = err.raw_os_error() {
        #[cfg(windows)]
        {
            // ERROR_ACCESS_DENIED, ERROR_LOCK_VIOLATION, ERROR_NOT_SUPPORTED
            return matches!(code, 5 | 33 | 50);
        }
        #[cfg(unix)]
        {
            // EAGAIN/EWOULDBLOCK, ENOSYS, EOPNOTSUPP, ENOLCK, ENOTSUP
            return matches!(code, 11 | 38 | 45 | 95 | 122);
        }
        #[cfg(not(any(windows, unix)))]
        {
            let _ = code;
        }
    }
    matches!(
        err.kind(),
        std::io::ErrorKind::Unsupported | std::io::ErrorKind::PermissionDenied
    )
}

fn warn_lock_unavailable(err: &std::io::Error) {
    if !LOCK_UNAVAILABLE_WARNED.swap(true, Ordering::Relaxed) {
        eprintln!("⚠ advisory file lock unavailable ({err}); continuing without lock");
    }
}

/// Exclusive advisory lock for append-heavy JSONL writers (history, live events).
/// Best-effort: tolerates platform/NFS lock failures with a one-shot stderr warning.
pub fn lock_exclusive(file: &std::fs::File) -> std::io::Result<()> {
    match fs2::FileExt::lock_exclusive(file) {
        Ok(()) => Ok(()),
        Err(e) if lock_error_tolerable(&e) => {
            warn_lock_unavailable(&e);
            Ok(())
        }
        Err(e) => Err(e),
    }
}

/// Release an advisory lock acquired via [`lock_exclusive`].
pub fn unlock(file: &std::fs::File) -> std::io::Result<()> {
    match fs2::FileExt::unlock(file) {
        Ok(()) => Ok(()),
        Err(e) if lock_error_tolerable(&e) => Ok(()),
        Err(e) => Err(e),
    }
}

pub fn atomic_write_text(target: &std::path::Path, text: &str) -> std::io::Result<()> {
    use std::io::Write;
    if target.is_symlink() {
        return Err(std::io::Error::other(format!(
            "refusing to write through symlink: {}",
            target.display()
        )));
    }
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

const INFER_SKIP_DIRS: &[&str] = &[
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    ".advisor",
    ".cursor",
    "vendor",
    "tests",
    "test",
    "__tests__",
];

const INFER_SOURCE_EXTS: &[&str] = &[
    "py", "rs", "go", "js", "ts", "tsx", "jsx", "java", "kt", "cs", "rb", "php", "swift", "cpp",
    "c",
];

/// When the caller left `file_types` at the `*.py` sentinel, infer a better
/// default from project manifests and dominant source extensions under `root`.
/// Returns `None` when the tree is empty or unreadable. Skips common
/// vendor/build dirs and test trees so legacy `tests/*.py` does not mask a
/// Rust/JS primary codebase.
pub fn infer_default_file_types(root: &std::path::Path) -> Option<String> {
    let root = root.canonicalize().ok()?;
    if !root.is_dir() {
        return None;
    }
    if root.join("Cargo.toml").is_file() {
        return Some("*.rs".to_string());
    }
    if root.join("package.json").is_file() {
        return Some("*.js,*.ts,*.tsx,*.jsx".to_string());
    }
    if root.join("go.mod").is_file() {
        return Some("*.go".to_string());
    }
    if root.join("pyproject.toml").is_file() || root.join("setup.py").is_file() {
        return Some("*.py".to_string());
    }
    let mut counts: std::collections::HashMap<&'static str, usize> =
        INFER_SOURCE_EXTS.iter().copied().map(|e| (e, 0)).collect();
    infer_count_extensions(&root, &mut counts);
    let total: usize = counts.values().sum();
    if total == 0 {
        return None;
    }

    let js = counts["js"] + counts["jsx"];
    let ts = counts["ts"] + counts["tsx"];
    let js_ecosystem = js + ts;
    if js_ecosystem > 0 {
        let max_other = counts
            .iter()
            .filter(|(ext, _)| !matches!(**ext, "js" | "jsx" | "ts" | "tsx"))
            .map(|(_, c)| *c)
            .max()
            .unwrap_or(0);
        if js_ecosystem >= max_other {
            let mut patterns = Vec::new();
            if js > 0 {
                patterns.push("*.js");
            }
            if ts > 0 {
                patterns.push("*.ts");
            }
            if counts["jsx"] > 0 {
                patterns.push("*.jsx");
            }
            if counts["tsx"] > 0 {
                patterns.push("*.tsx");
            }
            if patterns.is_empty() {
                patterns.push("*.js");
            }
            return Some(patterns.join(","));
        }
    }

    let (ext, count) = counts.iter().max_by_key(|(_, count)| *count)?;
    if *count == 0 {
        return None;
    }
    Some(format!("*.{ext}"))
}

fn infer_count_extensions(
    dir: &std::path::Path,
    counts: &mut std::collections::HashMap<&str, usize>,
) {
    let Ok(rd) = std::fs::read_dir(dir) else {
        return;
    };
    for entry in rd.flatten() {
        let Ok(ft) = entry.file_type() else {
            continue;
        };
        if ft.is_symlink() {
            continue;
        }
        let path = entry.path();
        if ft.is_dir() {
            let name = path.file_name().and_then(|n| n.to_str()).unwrap_or("");
            if name.starts_with('.') || INFER_SKIP_DIRS.contains(&name) {
                continue;
            }
            infer_count_extensions(&path, counts);
        } else if ft.is_file() {
            if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
                let ext_lower = ext.to_ascii_lowercase();
                if let Some(slot) = counts.get_mut(ext_lower.as_str()) {
                    *slot += 1;
                }
            }
        }
    }
}

/// Read up to `max_bytes` from the end of a file (for bounded JSONL tail scans).
pub fn read_tail_bytes(path: &std::path::Path, max_bytes: u64) -> std::io::Result<Vec<u8>> {
    use std::io::{Read, Seek, SeekFrom};
    let mut file = std::fs::File::open(path)?;
    let size = file.metadata()?.len();
    if size == 0 {
        return Ok(Vec::new());
    }
    let chunk = size.min(max_bytes);
    file.seek(SeekFrom::End(-(chunk as i64)))?;
    let mut buf = vec![0u8; chunk as usize];
    file.read_exact(&mut buf)?;
    Ok(buf)
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
        assert_eq!(
            CONTENT_SCAN_LIMIT,
            v["CONTENT_SCAN_LIMIT"].as_u64().unwrap() as usize
        );
        assert_eq!(
            normalize_path("foo/bar.py"),
            v["normalize_no_dot"].as_str().unwrap()
        );
        assert_eq!(
            normalize_path("./foo/bar.py"),
            v["normalize_dotslash"].as_str().unwrap()
        );
        assert_eq!(
            normalize_path("/abs/path.py"),
            v["normalize_abs_passthrough"].as_str().unwrap()
        );
        assert_eq!(
            validate_file_types("*.py").is_ok(),
            v["validate_ok_noerr"].as_bool().unwrap()
        );
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
    fn atomic_write_rejects_symlink_target() {
        let dir = std::env::temp_dir().join(format!("advisor_fs_symlink_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let target = dir.join("out.txt");
        let payload = dir.join("payload.txt");
        std::fs::write(&payload, "secret").unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::symlink;
            symlink(&payload, &target).unwrap();
            let err = atomic_write_text(&target, "overwrite").unwrap_err();
            assert!(err.to_string().contains("symlink"));
        }
        let _ = std::fs::remove_dir_all(&dir);
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

    #[test]
    fn lock_error_tolerable_permission_denied() {
        let err = std::io::Error::from(std::io::ErrorKind::PermissionDenied);
        assert!(lock_error_tolerable(&err));
    }

    #[cfg(windows)]
    #[test]
    fn lock_error_tolerable_windows_access_denied_code() {
        let err = std::io::Error::from_raw_os_error(5);
        assert!(lock_error_tolerable(&err));
    }

    #[test]
    fn infer_default_file_types_detects_rust_and_js() {
        let dir = std::env::temp_dir().join(format!("advisor_infer_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(dir.join("src")).unwrap();
        std::fs::create_dir_all(dir.join("tests")).unwrap();
        std::fs::write(dir.join("src/lib.rs"), "fn main() {}").unwrap();
        std::fs::write(dir.join("tests/legacy.py"), "def test_x(): pass").unwrap();
        std::fs::write(dir.join("Cargo.toml"), "[package]\nname = \"x\"\n").unwrap();
        assert_eq!(infer_default_file_types(&dir).as_deref(), Some("*.rs"));

        let js_dir = dir.join("jsproj");
        std::fs::create_dir_all(js_dir.join("src")).unwrap();
        std::fs::write(js_dir.join("package.json"), "{}\n").unwrap();
        std::fs::write(js_dir.join("src/index.js"), "export {}").unwrap();
        std::fs::write(js_dir.join("src/util.ts"), "export {}").unwrap();
        assert_eq!(
            infer_default_file_types(&js_dir).as_deref(),
            Some("*.js,*.ts,*.tsx,*.jsx")
        );

        let react_dir = dir.join("reactproj");
        std::fs::create_dir_all(react_dir.join("src")).unwrap();
        std::fs::write(react_dir.join("package.json"), "{}\n").unwrap();
        std::fs::write(react_dir.join("src/App.tsx"), "export {}").unwrap();
        assert_eq!(
            infer_default_file_types(&react_dir).as_deref(),
            Some("*.js,*.ts,*.tsx,*.jsx")
        );

        let upper_dir = dir.join("upper");
        std::fs::create_dir_all(upper_dir.join("src")).unwrap();
        std::fs::write(upper_dir.join("src/lib.RS"), "fn main() {}").unwrap();
        assert_eq!(
            infer_default_file_types(&upper_dir).as_deref(),
            Some("*.rs")
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn infer_count_extensions_skips_symlink_loops() {
        let dir = std::env::temp_dir().join(format!("advisor_infer_loop_{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("main.rs"), "fn main() {}").unwrap();
        use std::os::unix::fs::symlink;
        symlink(".", dir.join("loop")).unwrap();

        let mut counts: std::collections::HashMap<&str, usize> =
            INFER_SOURCE_EXTS.iter().copied().map(|e| (e, 0)).collect();
        infer_count_extensions(&dir, &mut counts);
        assert_eq!(counts["rs"], 1);

        let _ = std::fs::remove_dir_all(&dir);
    }
}
