//! Port of `advisor/orchestrate/runner_prompts.py` (incremental).
//!
//! So far: the legacy per-batch [`build_runner_prompt`] and its helpers. The
//! pool-spawn prompt, dispatch/fix/handoff message builders, and agent specs
//! are tracked in PORT_NOTES (each snapshot-gated).

use std::collections::HashMap;

use crate::fence::sanitize_inline;
use crate::focus::{Complexity, FocusBatch, FocusTask};

use super::FINDING_SCHEMA;

/// Render the batch's file list with priority + optional guidance. Mirrors
/// `_format_batch_files`.
fn format_batch_files(batch: &FocusBatch, guidance: &HashMap<String, String>) -> String {
    let mut lines = Vec::with_capacity(batch.tasks.len());
    for t in &batch.tasks {
        let g = sanitize_inline(guidance.get(&t.file_path).map(|s| s.trim()).unwrap_or(""));
        let suffix = if g.is_empty() {
            String::new()
        } else {
            format!(" — {g}")
        };
        lines.push(format!(
            "- `{}` (P{}){suffix}",
            sanitize_inline(&t.file_path),
            t.priority
        ));
    }
    lines.join("\n")
}

/// Wrap a single [`FocusTask`] into a one-file `medium`-complexity batch.
/// Mirrors `_coerce_batch`.
pub fn coerce_batch(task: &FocusTask) -> FocusBatch {
    FocusBatch {
        batch_id: 1,
        tasks: vec![task.clone()],
        complexity: Complexity::Medium,
    }
}

/// Legacy per-batch runner prompt. Mirrors `build_runner_prompt`.
pub fn build_runner_prompt(batch: &FocusBatch, guidance: &HashMap<String, String>) -> String {
    let files_block = format_batch_files(batch, guidance);
    format!(
        "You are a runner. Review ONLY these files:\n\n{files_block}\n\nBatch complexity: **{}**. The advisor grouped these together because it judged them reviewable as a unit — respect the scope.\n\n## Process\n1. Read every listed file fully\n2. For each file, hypothesize issues (bugs, security, logic, edge cases)\n3. Trace call paths and data flow to confirm or reject each hypothesis\n4. **Checkpoint with the advisor** before writing your final report.\n   Send a short draft via `SendMessage(to='team-lead')` listing each\n   candidate finding as `file:line — confidence (HIGH|MED|LOW) — one-line reason`.\n   Team-lead relays it to the advisor. Wait for the advisor's reply\n   (CONFIRM / NARROW / REDIRECT) and incorporate it before finalizing.\n5. For each confirmed issue, report:\n{FINDING_SCHEMA}\n6. If a file is clean, say so explicitly for that file\n\nDo NOT review other files. Do NOT review files outside this batch. If you hit a cross-reference, note it but stay scoped.\n\nWhen done, send your complete output to team-lead via SendMessage(to='team-lead'). Team-lead relays it to the advisor.",
        batch.complexity.as_str()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn two_file_batch() -> FocusBatch {
        FocusBatch {
            batch_id: 1,
            tasks: vec![
                FocusTask {
                    file_path: "src/auth.py".into(),
                    priority: 1,
                    prompt: String::new(),
                },
                FocusTask {
                    file_path: "src/session.py".into(),
                    priority: 2,
                    prompt: String::new(),
                },
            ],
            complexity: Complexity::High,
        }
    }

    #[test]
    fn runner_prompt_batch_matches_snapshot() {
        let got = build_runner_prompt(&two_file_batch(), &HashMap::new());
        assert_eq!(
            got,
            include_str!("../../tests/snapshots/runner_prompt_batch.txt")
        );
    }

    #[test]
    fn runner_prompt_single_task_matches_snapshot() {
        let task = FocusTask {
            file_path: "src/util.py".into(),
            priority: 3,
            prompt: String::new(),
        };
        let got = build_runner_prompt(&coerce_batch(&task), &HashMap::new());
        assert_eq!(
            got,
            include_str!("../../tests/snapshots/runner_prompt_single_task.txt")
        );
    }

    #[test]
    fn runner_prompt_with_guidance_matches_snapshot() {
        let guidance: HashMap<String, String> = [
            ("src/auth.py", "check token-refresh race"),
            ("src/session.py", "verify cookie SameSite"),
        ]
        .iter()
        .map(|(k, v)| (k.to_string(), v.to_string()))
        .collect();
        let got = build_runner_prompt(&two_file_batch(), &guidance);
        assert_eq!(
            got,
            include_str!("../../tests/snapshots/runner_prompt_with_guidance.txt")
        );
    }
}
