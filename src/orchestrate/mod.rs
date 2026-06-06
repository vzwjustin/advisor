//! Port of `advisor/orchestrate/` — team config + prompt/message builders.
//!
//! `config` is ported at the crate root (`crate::config`). This module hosts
//! the prompt builders. So far: the finding-report schema and the verify
//! dispatch prompt/message; the advisor/runner prompt builders (snapshot-parity
//! gated) are tracked in PORT_NOTES.

pub mod advisor_prompt;
pub mod explorer_prompts;
pub mod pipeline;
pub mod runner_prompts;
pub mod verify_dispatch;

/// Canonical finding-report schema for runner output. Mirrors `_schema.FINDING_SCHEMA`.
pub const FINDING_SCHEMA: &str = "- **File**: path:line_number\n- **Severity**: CRITICAL / HIGH / MEDIUM / LOW\n- **Description**: what the issue is\n- **Evidence**: the code path or proof\n- **Expected \u{2192} Actual**: *(MEDIUM+ only)* what you expected before reading this file \u{00b7} what you actually found — the divergence is the finding\n- **Fix**: suggested remediation";

pub use advisor_prompt::build_advisor_prompt;
pub use explorer_prompts::{build_explorer_pool_agents, build_explorer_prompt};
pub use pipeline::render_pipeline;
pub use runner_prompts::{
    build_coder_prompt, build_fix_assignment_message, build_runner_batch_message,
    build_runner_dispatch_messages, build_runner_handoff_message, build_runner_pool_prompt,
    build_runner_prompt,
};
pub use verify_dispatch::{build_verify_dispatch_prompt, build_verify_message};
