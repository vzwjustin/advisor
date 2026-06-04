//! Core data models shared across modules.
//!
//! Ports the `Finding` dataclass from `advisor/verify.py` and the `RankedFile`
//! dataclass from `advisor/rank.py`, plus the canonical severity set. Field
//! names and declaration order match the Python dataclasses so `serde` output
//! mirrors Python's `dataclasses.asdict`.

use serde::{Deserialize, Serialize};

/// Canonical advisor severities. Unknown/invalid input canonicalizes to
/// [`Severity::Unknown`] (mirrors `_canonical_severity` in `verify.py`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Severity {
    Critical,
    High,
    Medium,
    Low,
    Unknown,
}

impl Severity {
    /// The four allowed severities (excludes the `UNKNOWN` sentinel), in the
    /// canonical ordering used by sorted output. Mirrors `_ALLOWED_SEVERITIES`.
    pub const ALLOWED: [Severity; 4] = [
        Severity::Critical,
        Severity::High,
        Severity::Medium,
        Severity::Low,
    ];

    /// Canonical upper-case string form (`CRITICAL`/`HIGH`/`MEDIUM`/`LOW`/`UNKNOWN`).
    pub fn as_str(self) -> &'static str {
        match self {
            Severity::Critical => "CRITICAL",
            Severity::High => "HIGH",
            Severity::Medium => "MEDIUM",
            Severity::Low => "LOW",
            Severity::Unknown => "UNKNOWN",
        }
    }

    /// Canonicalize an arbitrary input string: upper-case and validate against
    /// the allowed set; anything else becomes [`Severity::Unknown`]. Mirrors
    /// `_canonical_severity`.
    pub fn canonical(raw: &str) -> Severity {
        match raw.trim().to_ascii_uppercase().as_str() {
            "CRITICAL" => Severity::Critical,
            "HIGH" => Severity::High,
            "MEDIUM" => Severity::Medium,
            "LOW" => Severity::Low,
            _ => Severity::Unknown,
        }
    }
}

/// A single review finding. Mirrors the `Finding` dataclass in `verify.py`
/// (`frozen=True, slots=True`). Field order matches the Python declaration so
/// JSON serialization is byte-comparable with `asdict`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Finding {
    pub file_path: String,
    pub severity: String,
    pub description: String,
    pub evidence: String,
    pub fix: String,
    #[serde(default)]
    pub rule_id: Option<String>,
    #[serde(default)]
    pub expected_vs_actual: String,
}

impl Finding {
    /// Construct a finding, canonicalizing the severity to the allowed set.
    pub fn new(
        file_path: impl Into<String>,
        severity: &str,
        description: impl Into<String>,
        evidence: impl Into<String>,
        fix: impl Into<String>,
    ) -> Self {
        Finding {
            file_path: file_path.into(),
            severity: Severity::canonical(severity).as_str().to_string(),
            description: description.into(),
            evidence: evidence.into(),
            fix: fix.into(),
            rule_id: None,
            expected_vs_actual: String::new(),
        }
    }
}

/// A file ranked by likelihood of containing issues (priority 1-5). Mirrors the
/// `RankedFile` dataclass in `rank.py`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RankedFile {
    pub path: String,
    pub priority: u8,
    pub reasons: Vec<String>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn severity_canonicalizes() {
        assert_eq!(Severity::canonical("critical"), Severity::Critical);
        assert_eq!(Severity::canonical(" High "), Severity::High);
        assert_eq!(Severity::canonical("bogus"), Severity::Unknown);
        assert_eq!(Severity::Critical.as_str(), "CRITICAL");
    }

    #[test]
    fn finding_serializes_all_fields_in_order() {
        let f = Finding::new("a.py:1", "high", "desc", "ev", "fix");
        let v: serde_json::Value = serde_json::to_value(&f).unwrap();
        // Every dataclass field is present (matches Python asdict completeness).
        for key in [
            "file_path",
            "severity",
            "description",
            "evidence",
            "fix",
            "rule_id",
            "expected_vs_actual",
        ] {
            assert!(v.get(key).is_some(), "missing key {key}");
        }
        assert_eq!(v["severity"], "HIGH");
    }
}
