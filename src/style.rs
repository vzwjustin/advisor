//! Port of the terminal-styling helpers in `advisor/_style.py`.
//!
//! This first slice ports [`strip_ansi`] — the function used to sanitize
//! untrusted text (e.g. paths echoed from a target repo) before it is printed.
//! The remaining color/glyph/box helpers are tracked in PORT_NOTES.md.

use once_cell::sync::Lazy;
use regex::Regex;

// Matches CSI sequences (`\x1b[ ... letter`) and OSC sequences
// (`\x1b] ... BEL|ESC\`). Mirrors `_ANSI_STRIP_RE` in the Python source:
//   r"\x1b\[[\d;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
static ANSI_STRIP_RE: Lazy<Regex> = Lazy::new(|| {
    Regex::new(r"\x1b\[[\d;?]*[a-zA-Z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
        .expect("ANSI strip regex is a valid compile-time constant")
});

/// Remove ANSI CSI and OSC escape sequences from `text`.
///
/// Safe for untrusted input copied from a target repository. Mirrors Python
/// `strip_ansi`.
pub fn strip_ansi(text: &str) -> String {
    ANSI_STRIP_RE.replace_all(text, "").into_owned()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn strips_sgr_color() {
        assert_eq!(strip_ansi("\x1b[31mred\x1b[0m"), "red");
    }

    #[test]
    fn strips_osc_hyperlink() {
        // OSC 8 hyperlink wrapper, terminated by BEL.
        let s = "\x1b]8;;https://example.com\x07link\x1b]8;;\x07";
        assert_eq!(strip_ansi(s), "link");
    }

    #[test]
    fn leaves_plain_text() {
        assert_eq!(strip_ansi("plain text"), "plain text");
    }
}
