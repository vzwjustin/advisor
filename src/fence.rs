//! Port of `advisor/orchestrate/_fence.py` — fence-escape helpers for
//! embedding untrusted payloads in prompts.
//!
//! Two public functions, ported verbatim in behavior:
//! - [`sanitize_inline`] neutralizes markdown-fence breakers in an inline
//!   backtick span (backtick -> typographic single quote, linebreaks ->
//!   space, invisibles dropped).
//! - [`fence`] wraps a payload in a code fence it provably cannot escape.

/// Canonical line-break code points. `str.splitlines()` in Python treats all
/// of these as line breaks; each is replaced with a single space. The list and
/// order match `_LINEBREAK_TO_SPACE` in the Python source. (Note: a literal
/// ASCII space and U+00A0 NBSP are included in the Python tuple and replaced
/// with a space — preserved here for byte parity.)
const LINEBREAK_TO_SPACE: &[char] = &[
    '\u{000D}', // \r  (note: the "\r\n" pair is handled char-by-char; result is identical)
    '\u{000A}', // \n
    '\u{000B}', // VT
    '\u{000C}', // FF
    '\u{0020}', // space (present in the Python tuple)
    '\u{00A0}', // NBSP
    '\u{0085}', // NEL
];

/// Zero-width / invisible code points dropped entirely (bidi controls, ZWSP,
/// BOM, soft hyphen, word joiner, marks/isolates). Matches `_INVISIBLE_TO_DROP`.
const INVISIBLE_TO_DROP: &[char] = &[
    '\u{0000}', // NUL
    '\u{200B}', // ZWSP
    '\u{200C}', // ZWNJ
    '\u{200D}', // ZWJ
    '\u{FEFF}', // BOM / ZWNBSP
    '\u{00AD}', // soft hyphen
    '\u{202A}', // LRE
    '\u{202B}', // RLE
    '\u{202C}', // PDF
    '\u{202D}', // LRO
    '\u{202E}', // RLO
    '\u{2060}', // WJ
    '\u{200E}', // LRM
    '\u{200F}', // RLM
    '\u{2066}', // LRI
    '\u{2067}', // RLI
    '\u{2068}', // FSI
    '\u{2069}', // PDI
];

/// Replace every canonical line-break with a space and drop invisibles.
/// Single source of truth for the strip used by both [`sanitize_inline`] and
/// [`fence`]'s `lang` path — mirrors Python's `_strip_linebreaks`.
fn strip_linebreaks(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        if INVISIBLE_TO_DROP.contains(&ch) {
            continue;
        }
        if LINEBREAK_TO_SPACE.contains(&ch) {
            out.push(' ');
        } else {
            out.push(ch);
        }
    }
    out
}

/// Neutralize markdown-fence breakers in a value rendered inline.
///
/// Swaps backticks for a typographic single quote (U+2019), then routes through
/// the canonical line-break / zero-width strip. Mirrors Python `sanitize_inline`.
pub fn sanitize_inline(value: &str) -> String {
    // Python: _strip_linebreaks(value.replace("`", "'")) where "'" is the
    // typographic single quote U+2019. Folded into a single pass: the
    // substituted quote is neither a linebreak nor an invisible, so the order
    // of the two Python steps does not affect the result.
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        if ch == '`' {
            out.push('\u{2019}');
        } else if INVISIBLE_TO_DROP.contains(&ch) {
            continue;
        } else if LINEBREAK_TO_SPACE.contains(&ch) {
            out.push(' ');
        } else {
            out.push(ch);
        }
    }
    out
}

/// Wrap `payload` in a code fence it provably cannot escape.
///
/// Picks the shortest fence of backticks (>= 3) longer than the longest run of
/// backticks inside `payload`. Mirrors Python `fence`.
pub fn fence(payload: &str, lang: &str) -> String {
    let mut longest = 0usize;
    let mut run = 0usize;
    for ch in payload.chars() {
        if ch == '`' {
            run += 1;
            if run > longest {
                longest = run;
            }
        } else {
            run = 0;
        }
    }
    let fence_len = std::cmp::max(3, longest + 1);
    let bar: String = "`".repeat(fence_len);
    // Python: _strip_linebreaks(lang.replace("`", "")) — strip backticks first.
    let lang_no_backtick: String = lang.chars().filter(|&c| c != '`').collect();
    let safe_lang = strip_linebreaks(&lang_no_backtick);
    format!("{bar}{safe_lang}\n{payload}\n{bar}")
}

#[cfg(test)]
mod tests {
    use super::*;

    // Reference values captured from the Python implementation.
    #[test]
    fn fence_basic() {
        assert_eq!(fence("hello", ""), "```\nhello\n```");
    }

    #[test]
    fn fence_escapes_collision() {
        assert_eq!(fence("a ``` b", ""), "````\na ``` b\n````");
    }

    #[test]
    fn fence_with_lang() {
        assert_eq!(fence("x", "py"), "```py\nx\n```");
    }

    #[test]
    fn sanitize_inline_backtick_and_newline() {
        // Python: "a`b\nc" -> "a'b c" with the single quote being U+2019.
        assert_eq!(sanitize_inline("a`b\nc"), "a\u{2019}b c");
    }

    #[test]
    fn sanitize_inline_drops_invisible_and_bidi() {
        // ZWSP (U+200B) and RLO (U+202E) are dropped: "p<ZWSP>q<RLO>R" -> "pqR"
        assert_eq!(sanitize_inline("p\u{200b}q\u{202e}R"), "pqR");
    }
}
