//! JSON helpers that match Python's `json.dumps` byte output.
//!
//! The Python CLI emits JSON with the stdlib default `ensure_ascii=True`,
//! which escapes every non-ASCII scalar as `\uXXXX` (and astral-plane scalars
//! as a UTF-16 surrogate pair). `serde_json` instead emits raw UTF-8. To keep
//! byte-for-byte parity with the Python CLI's `--json` output (e.g. the em-dash
//! `—` in preset descriptions), we post-process serde's output and escape
//! all non-ASCII scalars exactly the way CPython does.

/// Escape every non-ASCII scalar in `s` as `\uXXXX`, matching CPython's
/// `json.dumps(..., ensure_ascii=True)`. ASCII bytes (including already-present
/// escapes produced by `serde_json`) are passed through untouched.
///
/// serde_json has already produced valid JSON with all the mandatory escapes
/// (`"`, `\`, control chars). The only remaining difference from CPython is the
/// raw non-ASCII scalars, so escaping those — and only those — yields identical
/// bytes.
pub fn ensure_ascii(s: &str) -> String {
    use std::fmt::Write;
    // Fast path: pure ASCII input needs no rewriting.
    if s.is_ascii() {
        return s.to_owned();
    }
    let mut out = String::with_capacity(s.len() + 8);
    for ch in s.chars() {
        if ch.is_ascii() {
            out.push(ch);
            continue;
        }
        let cp = ch as u32;
        // Write directly into `out` to avoid a temporary String per scalar.
        // Writing to a String is infallible, so the Result is intentionally
        // discarded (no unwrap).
        if cp <= 0xFFFF {
            let _ = write!(out, "\\u{cp:04x}");
        } else {
            // Encode as a UTF-16 surrogate pair, exactly like CPython.
            let v = cp - 0x10000;
            let high = 0xD800 + (v >> 10);
            let low = 0xDC00 + (v & 0x3FF);
            let _ = write!(out, "\\u{high:04x}\\u{low:04x}");
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ascii_passes_through() {
        assert_eq!(ensure_ascii("hello \"world\""), "hello \"world\"");
    }

    #[test]
    fn em_dash_escaped_like_cpython() {
        // CPython: json.dumps("a — b") -> "\"a \\u2014 b\""
        assert_eq!(ensure_ascii("a \u{2014} b"), "a \\u2014 b");
    }

    #[test]
    fn astral_scalar_is_surrogate_pair() {
        // U+1F600 GRINNING FACE -> 😀 (CPython ensure_ascii)
        assert_eq!(ensure_ascii("\u{1F600}"), "\\ud83d\\ude00");
    }
}
