//! Port of `advisor/git_scope.py` — limit a plan to git-changed files via
//! `--since REF` / `--staged` / `--branch REF`. Returns absolute paths of
//! existing files. Errors surface as `GitScopeError` (a `String` here).

use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use once_cell::sync::Lazy;
use regex::Regex;

const GIT_TIMEOUT_SECS: u64 = 30;
const GIT_MAX_STDOUT_BYTES: usize = 50 * 1024 * 1024;
const GIT_MAX_STDERR_BYTES: usize = 1024 * 1024;

static REF_ALLOWED: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"^[A-Za-z0-9_./~^@{}\-]+$").expect("ref-allowed regex"));

/// Run `git <args>` in `cwd`, returning non-empty stdout lines. Mirrors `_run_git`.
fn run_git(cwd: &Path, args: &[&str]) -> Result<Vec<String>, String> {
    let mut child = match Command::new("git")
        .args(args)
        .current_dir(cwd)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
    {
        Ok(c) => c,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            return Err(
                "git is not on PATH; --since/--staged/--branch require a git checkout".to_string(),
            );
        }
        Err(e) => return Err(format!("failed to invoke git: {e}")),
    };

    // Poll for completion up to the timeout, then kill (hang protection).
    let deadline = Instant::now() + Duration::from_secs(GIT_TIMEOUT_SECS);
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return Err(format!(
                        "git {} timed out after {GIT_TIMEOUT_SECS}s",
                        args.join(" ")
                    ));
                }
                std::thread::sleep(Duration::from_millis(20));
            }
            Err(e) => return Err(format!("failed to invoke git: {e}")),
        }
    }
    let output = child
        .wait_with_output()
        .map_err(|e| format!("failed to invoke git: {e}"))?;

    if output.stderr.len() > GIT_MAX_STDERR_BYTES {
        return Err(format!(
            "git {} produced more than {} MiB of stderr — refusing to load",
            args.join(" "),
            GIT_MAX_STDERR_BYTES / (1024 * 1024)
        ));
    }
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr);
        let stderr = stderr.trim();
        let stderr = if stderr.is_empty() {
            "(no stderr)"
        } else {
            stderr
        };
        return Err(format!("git {} failed: {stderr}", args.join(" ")));
    }
    if output.stdout.len() > GIT_MAX_STDOUT_BYTES {
        return Err(format!(
            "git {} produced more than {} MiB of stdout — refusing to load",
            args.join(" "),
            GIT_MAX_STDOUT_BYTES / (1024 * 1024)
        ));
    }
    let stdout = String::from_utf8_lossy(&output.stdout);
    Ok(stdout
        .lines()
        .filter(|l| !l.trim().is_empty())
        .map(|l| l.to_string())
        .collect())
}

fn repo_root(cwd: &Path) -> Result<PathBuf, String> {
    let lines = run_git(cwd, &["rev-parse", "--show-toplevel"])?;
    match lines.first() {
        Some(l) => Ok(PathBuf::from(l)),
        None => Err(format!("{} is not inside a git repository", cwd.display())),
    }
}

/// Convert repo-relative paths to absolute, keeping only existing files under
/// the repo root. Mirrors `_resolve_files`.
fn resolve_files(repo: &Path, rel_paths: &[String]) -> Vec<String> {
    let resolved_root = repo.canonicalize().unwrap_or_else(|_| repo.to_path_buf());
    let mut out = Vec::new();
    for rel in rel_paths {
        let p = repo.join(rel);
        if !p.is_file() {
            continue;
        }
        match p.canonicalize() {
            Ok(rp) if rp.starts_with(&resolved_root) => out.push(p.to_string_lossy().to_string()),
            _ => continue,
        }
    }
    out
}

/// Files changed between `ref` and the working tree. Mirrors `files_since`.
pub fn files_since(target: &Path, git_ref: &str) -> Result<Vec<String>, String> {
    let repo = repo_root(target)?;
    let lines = run_git(&repo, &["diff", "--name-only", git_ref, "--"])?;
    Ok(resolve_files(&repo, &lines))
}

/// Files currently staged. Mirrors `files_staged`.
pub fn files_staged(target: &Path) -> Result<Vec<String>, String> {
    let repo = repo_root(target)?;
    let lines = run_git(&repo, &["diff", "--name-only", "--cached", "--"])?;
    Ok(resolve_files(&repo, &lines))
}

/// Files changed in the current branch vs `base_ref` (triple-dot). Mirrors `files_branch`.
pub fn files_branch(target: &Path, base_ref: &str) -> Result<Vec<String>, String> {
    let repo = repo_root(target)?;
    let spec = format!("{base_ref}...HEAD");
    let lines = run_git(&repo, &["diff", "--name-only", &spec, "--"])?;
    Ok(resolve_files(&repo, &lines))
}

fn validate_ref(label: &str, value: &str) -> Result<(), String> {
    if value.starts_with('-') {
        return Err(format!(
            "{label} ref {value:?} cannot begin with '-'; git would parse it as an option, not a ref"
        ));
    }
    if !REF_ALLOWED.is_match(value) {
        return Err(format!(
            "{label} ref {value:?} contains characters outside [A-Za-z0-9_./~^@{{}}-]; reject to prevent option-injection"
        ));
    }
    if value.contains("..") {
        return Err(format!(
            "{label} ref {value:?} contains '..'; pass a single ref, not a revrange"
        ));
    }
    Ok(())
}

/// Resolve the active git-scope selector. Returns `Ok(None)` when no selector
/// is set (caller falls back to the full scan). Mirrors `resolve_git_scope`.
pub fn resolve_git_scope(
    target: &Path,
    since: Option<&str>,
    staged: bool,
    branch: Option<&str>,
) -> Result<Option<Vec<String>>, String> {
    let count = since.is_some() as u8 + staged as u8 + branch.is_some() as u8;
    if count > 1 {
        return Err("--since, --staged and --branch are mutually exclusive; pick one".to_string());
    }
    if let Some(v) = since {
        validate_ref("--since", v)?;
    }
    if let Some(v) = branch {
        validate_ref("--branch", v)?;
    }
    if let Some(v) = since {
        return Ok(Some(files_since(target, v)?));
    }
    if staged {
        return Ok(Some(files_staged(target)?));
    }
    if let Some(v) = branch {
        return Ok(Some(files_branch(target, v)?));
    }
    Ok(None)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ref_validation() {
        assert!(validate_ref("--since", "main").is_ok());
        assert!(validate_ref("--since", "HEAD~3").is_ok());
        assert!(validate_ref("--branch", "origin/main").is_ok());
        assert!(validate_ref("--since", "-p").is_err());
        assert!(validate_ref("--since", "main --output=/tmp/x").is_err());
        assert!(validate_ref("--branch", "HEAD..main").is_err());
    }

    #[test]
    fn mutually_exclusive() {
        let t = Path::new(".");
        assert!(resolve_git_scope(t, Some("main"), true, None).is_err());
        assert!(resolve_git_scope(t, None, false, None).unwrap().is_none());
    }
}
