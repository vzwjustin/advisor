//! Port of the terminal-styling helpers in `advisor/_style.py`.
//!
//! This first slice ports [`strip_ansi`] — the function used to sanitize
//! untrusted text (e.g. paths echoed from a target repo) before it is printed.
//! The remaining color/glyph/box helpers are tracked in PORT_NOTES.md.

use std::io::{self, Write};

use once_cell::sync::Lazy;
use regex::Regex;

/// Ignore `SIGPIPE` on Unix so writes to a closed downstream pipe return
/// [`io::ErrorKind::BrokenPipe`] instead of terminating the process.
#[cfg(unix)]
pub fn ignore_sigpipe() {
    extern "C" {
        fn signal(signum: i32, handler: usize) -> usize;
    }
    const SIGPIPE: i32 = 13;
    const SIG_IGN: usize = 1;
    unsafe {
        signal(SIGPIPE, SIG_IGN);
    }
}

#[cfg(not(unix))]
pub fn ignore_sigpipe() {}

/// Result of a stdout write when the downstream reader may have closed early.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StdoutWrite {
    Ok,
    BrokenPipe,
}

/// Write `text` plus a newline to stdout. A broken pipe is not an error for the
/// CLI — mirrors Python `_print` / `BrokenPipeError` → exit 0.
pub fn writeln_stdout(text: &str) -> StdoutWrite {
    let mut out = io::stdout().lock();
    match out.write_all(text.as_bytes()) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::BrokenPipe => return StdoutWrite::BrokenPipe,
        Err(e) => {
            eprintln!("write error: {e}");
            return StdoutWrite::Ok;
        }
    }
    match out.write_all(b"\n") {
        Ok(()) => StdoutWrite::Ok,
        Err(e) if e.kind() == io::ErrorKind::BrokenPipe => StdoutWrite::BrokenPipe,
        Err(e) => {
            eprintln!("write error: {e}");
            StdoutWrite::Ok
        }
    }
}

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

    #[test]
    fn ignore_sigpipe_maps_closed_pipe_to_broken_pipe_error() {
        ignore_sigpipe();
        let (reader, mut writer) = std::io::pipe().expect("pipe");
        drop(reader);
        let err = writer.write_all(b"x").unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::BrokenPipe);
    }
}
