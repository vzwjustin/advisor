//! Port of the pricing model constants and family resolution from
//! `advisor/cost.py`.
//!
//! This slice ports the pricing table, token-overhead constants, and
//! [`family_of`]. The full `estimate_cost` / `load_pricing` / `format_estimate`
//! pipeline is tracked in PORT_NOTES.md.

/// Default pricing in **cents per million tokens**: `(input, output)` per
/// family. Mirrors `DEFAULT_PRICING_CENTS_PER_MTOK`.
pub const OPUS_CENTS_PER_MTOK: (i64, i64) = (1500, 7500);
pub const SONNET_CENTS_PER_MTOK: (i64, i64) = (300, 1500);
pub const HAIKU_CENTS_PER_MTOK: (i64, i64) = (25, 125);

/// Fixed token overheads used by the estimator.
pub const ADVISOR_SYSTEM_TOKENS: i64 = 4_500;
pub const RUNNER_SYSTEM_TOKENS: i64 = 2_000;
pub const PER_MESSAGE_OVERHEAD_TOKENS: i64 = 300;

/// Characters-per-token heuristic (English ~4, code ~3.5). Mirrors `CHARS_PER_TOKEN`.
pub const CHARS_PER_TOKEN: f64 = 3.5;

/// Pricing snapshot date `PRICING_AS_OF` (year, month, day).
pub const PRICING_AS_OF: (i32, u32, u32) = (2026, 5, 22);

/// Days after which the bundled default pricing is considered stale.
pub const PRICING_STALE_DAYS: i64 = 180;

/// Return the canonical family (`opus`, `sonnet`, `haiku`) for a model name,
/// substring-matched case-insensitively. Unknown names fall back to `sonnet`.
/// Mirrors Python `_family_of` (minus the one-shot stderr warning, which the
/// caller path will reintroduce when the estimator is ported).
pub fn family_of(model: &str) -> &'static str {
    let m = model.to_lowercase();
    if m.contains("opus") {
        "opus"
    } else if m.contains("sonnet") {
        "sonnet"
    } else if m.contains("haiku") {
        "haiku"
    } else {
        "sonnet"
    }
}

/// Default cents-per-Mtok `(input, output)` for a family name.
pub fn default_pricing_for(family: &str) -> (i64, i64) {
    match family {
        "opus" => OPUS_CENTS_PER_MTOK,
        "haiku" => HAIKU_CENTS_PER_MTOK,
        _ => SONNET_CENTS_PER_MTOK,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn family_resolution() {
        assert_eq!(family_of("claude-opus-4-7"), "opus");
        assert_eq!(family_of("claude-sonnet-4-6"), "sonnet");
        assert_eq!(family_of("claude-haiku-4-5"), "haiku");
        assert_eq!(family_of("gpt-4"), "sonnet"); // unknown -> sonnet fallback
        assert_eq!(family_of("OPUS"), "opus"); // case-insensitive
    }

    #[test]
    fn pricing_table() {
        assert_eq!(default_pricing_for("opus"), (1500, 7500));
        assert_eq!(default_pricing_for("sonnet"), (300, 1500));
        assert_eq!(default_pricing_for("haiku"), (25, 125));
        assert_eq!(default_pricing_for("unknown"), (300, 1500));
    }
}
