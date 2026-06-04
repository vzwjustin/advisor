//! Port of pure helpers from `advisor/sarif.py` (SARIF 2.1.0 emitter).
//!
//! This slice ports the stable constants, [`synthesize_rule_id`], and
//! [`level_for`]. The full `findings_to_sarif` document builder (path
//! containment, control stripping, regions) is tracked in PORT_NOTES.md.

use sha1::{Digest, Sha1};

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
    // Hex-encode, take first 16 chars (8 bytes).
    let mut slug = String::with_capacity(16);
    for byte in &digest[..8] {
        slug.push_str(&format!("{byte:02x}"));
    }
    format!("{prefix}/{}/{slug}", severity.to_ascii_lowercase())
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
}
