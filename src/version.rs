//! Port of `advisor/_version.py` version resolution.
//!
//! The Rust binary's authoritative version is its compile-time crate version
//! (`CARGO_PKG_VERSION`), which is kept in lockstep with `pyproject.toml` via
//! the release process. This mirrors the *result* of Python's
//! `resolve_version()` for an installed package; the local-pyproject-override
//! branch is a source-checkout convenience that the compiled binary does not
//! need.

/// Return the advisor version string. Mirrors the effective result of Python
/// `resolve_version()`.
pub fn resolve_version() -> &'static str {
    env!("CARGO_PKG_VERSION")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_is_nonempty_and_dotted() {
        let v = resolve_version();
        assert!(!v.is_empty());
        assert!(
            v.split('.').count() >= 2,
            "expected dotted version, got {v}"
        );
    }
}
