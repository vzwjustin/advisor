//! Port of `advisor/orchestrate/` — team config + prompt/message builders.
//!
//! `config` is ported at the crate root (`crate::config`). This module hosts
//! the prompt builders. So far: the finding-report schema and the verify
//! dispatch prompt/message; the advisor/runner prompt builders (snapshot-parity
//! gated) are tracked in PORT_NOTES.

pub mod advisor_prompt;
pub mod runner_prompts;
pub mod verify_dispatch;

/// Canonical finding-report schema for runner output. Mirrors `_schema.FINDING_SCHEMA`.
pub const FINDING_SCHEMA: &str = "- **File**: path:line_number\n- **Severity**: CRITICAL / HIGH / MEDIUM / LOW\n- **Description**: what the issue is\n- **Evidence**: the code path or proof\n- **Expected \u{2192} Actual**: *(MEDIUM+ only)* what you expected before reading this file \u{00b7} what you actually found — the divergence is the finding\n- **Fix**: suggested remediation";

pub use verify_dispatch::{build_verify_dispatch_prompt, build_verify_message};
