//! Port of constants and model-validation from `advisor/orchestrate/config.py`.
//!
//! This slice ports the public, pure pieces: the model-id constants and
//! [`is_known_model`]. The full `TeamConfig` struct and `default_team_config`
//! env/clamp assembler are tracked in PORT_NOTES.md.

use once_cell::sync::Lazy;
use regex::Regex;

/// Bare-family model aliases accepted by Claude Code / Codex (`KNOWN_MODEL_SHORTCUTS`).
pub const KNOWN_MODEL_SHORTCUTS: [&str; 3] = ["opus", "sonnet", "haiku"];

/// Hard ceiling on the runner pool size (`POOL_SIZE_CEILING`).
pub const POOL_SIZE_CEILING: i64 = 20;

/// Default advisor (Opus) model id.
pub const DEFAULT_ADVISOR_MODEL: &str = "claude-opus-4-7";

/// Default runner (Sonnet) model id.
pub const DEFAULT_RUNNER_MODEL: &str = "claude-sonnet-4-6";

// Long-form model id matcher, identical to `_LONG_FORM_MODEL_RE`.
static LONG_FORM_MODEL_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"^(?:Codex|claude)-(opus|sonnet|haiku)-\d+(?:[.-]\d+){0,3}(?:-\d{8})?$")
        .expect("model-id regex is a valid compile-time constant")
});

/// Return true if `name` looks like a valid Claude Code / Codex model string —
/// either a bare alias or a long-form `claude-`/`Codex-` family id. Mirrors
/// Python `is_known_model`.
pub fn is_known_model(name: &str) -> bool {
    if KNOWN_MODEL_SHORTCUTS.contains(&name) {
        return true;
    }
    LONG_FORM_MODEL_RE.is_match(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    // Reference values captured from the Python implementation.
    #[test]
    fn known_model_matrix() {
        assert!(is_known_model("opus"));
        assert!(is_known_model("claude-opus-4-7"));
        assert!(!is_known_model("opus-4-5"));
        assert!(is_known_model("claude-sonnet-4-6-20231015"));
        assert!(!is_known_model("gpt-4"));
        assert!(is_known_model("Codex-haiku-4-5"));
    }

    #[test]
    fn date_stamp_must_be_eight_digits() {
        // Bounded version segment must not swallow a bogus date stamp.
        assert!(!is_known_model("claude-opus-4-99999999-extra"));
    }
}
