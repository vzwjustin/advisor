//! Port of `advisor/orchestrate/runner_prompts.py` (incremental).
//!
//! So far: the legacy per-batch [`build_runner_prompt`] and its helpers. The
//! pool-spawn prompt, dispatch/fix/handoff message builders, and agent specs
//! are tracked in PORT_NOTES (each snapshot-gated).

use std::collections::HashMap;

use crate::fence::{fence, sanitize_inline};
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

/// SendMessage payload assigning a batch to a pool runner. Mirrors
/// `build_runner_batch_message` (errors on an empty batch).
pub fn build_runner_batch_message(
    batch: &FocusBatch,
    guidance: &HashMap<String, String>,
) -> Result<String, String> {
    if batch.tasks.is_empty() {
        return Err(format!(
            "batch {} has no tasks: cannot assign an empty batch to a runner",
            batch.batch_id
        ));
    }
    let files_block = format_batch_files(batch, guidance);
    Ok(format!(
        "## New batch assignment (batch {}, complexity: {})\n\nReview ONLY these files:\n\n{files_block}\n\nProcess:\n1. Read every listed file fully\n2. Hypothesize issues (bugs, security, logic, edge cases)\n3. Trace call paths to confirm or reject each\n4. Checkpoint draft findings with team-lead via `SendMessage(to='team-lead')` before finalizing — team-lead relays to the advisor\n5. Wait for CONFIRM / NARROW / REDIRECT from the advisor and incorporate\n6. For each confirmed issue, report:\n{FINDING_SCHEMA}\n7. Send your complete output to team-lead via `SendMessage(to='team-lead')`\n8. Then wait for your next batch\n\nDo NOT review files outside this batch.",
        batch.batch_id,
        batch.complexity.as_str()
    ))
}

/// SendMessage specs `(to, message)` to hand each batch to its pool runner.
/// Mirrors `build_runner_dispatch_messages` (validates batch ids vs pool size).
pub fn build_runner_dispatch_messages(
    batches: &[FocusBatch],
    pool_size: i64,
    guidance: &HashMap<String, String>,
) -> Result<Vec<(String, String)>, String> {
    if !batches.is_empty() {
        let ids: Vec<i64> = batches.iter().map(|b| b.batch_id as i64).collect();
        let bad: Vec<i64> = ids.iter().copied().filter(|i| *i < 1).collect();
        if !bad.is_empty() {
            return Err(format!(
                "batch_id must be >= 1; got {bad:?}: dispatch would route to a non-existent runner"
            ));
        }
        let empty: Vec<i64> = batches
            .iter()
            .filter(|b| b.tasks.is_empty())
            .map(|b| b.batch_id as i64)
            .collect();
        if !empty.is_empty() {
            let (label, verb) = if empty.len() == 1 {
                ("batch_id", "has")
            } else {
                ("batch_ids", "have")
            };
            return Err(format!(
                "{label} {empty:?} {verb} no tasks: dispatch would send an empty assignment"
            ));
        }
        let mut uniq = ids.clone();
        uniq.sort_unstable();
        uniq.dedup();
        if uniq.len() != ids.len() {
            return Err(format!(
                "duplicate batch_id in dispatch list {ids:?}: two batches would collide on the same runner"
            ));
        }
        let max_id = *ids.iter().max().unwrap_or(&0);
        if max_id > pool_size {
            return Err(format!(
                "batch_id {max_id} exceeds pool_size {pool_size}: dispatch would route to a never-spawned runner"
            ));
        }
    }
    let mut out = Vec::with_capacity(batches.len());
    for batch in batches {
        out.push((
            format!("runner-{}", batch.batch_id),
            build_runner_batch_message(batch, guidance)?,
        ));
    }
    Ok(out)
}

/// SendMessage spec `(to, message)` for a budget-stamped fix assignment.
/// Mirrors `build_fix_assignment_message`.
#[allow(clippy::too_many_arguments)]
pub fn build_fix_assignment_message(
    runner_id: i64,
    file_path: &str,
    problem: &str,
    change: &str,
    acceptance: &str,
    fix_number: i64,
    max_fixes: i64,
    large_file_max_fixes: i64,
    is_large_file: bool,
) -> Result<(String, String), String> {
    if fix_number < 1 {
        return Err(format!(
            "fix_number must be >= 1 (got {fix_number}); fix numbering is 1-indexed"
        ));
    }
    for (label, value) in [
        ("problem", problem),
        ("change", change),
        ("acceptance", acceptance),
    ] {
        if value.trim().is_empty() {
            return Err(format!(
                "{label} must not be empty or whitespace-only — a fix assignment with no {label:?} text would render as an empty fenced block and leave runner-{runner_id} with nothing to act on."
            ));
        }
    }
    let effective_cap = if is_large_file {
        large_file_max_fixes
    } else {
        max_fixes
    };
    let cap_label = if is_large_file {
        "large-file cap"
    } else {
        "cap"
    };
    if fix_number > effective_cap {
        return Err(format!(
            "fix_number={fix_number} exceeds {cap_label}={effective_cap} for runner-{runner_id}: rotate to a fresh runner before dispatching this fix. Use build_runner_handoff_message to hand off the remaining fix queue."
        ));
    }
    let budget_note = if fix_number == effective_cap {
        format!(
            "**LAST FIX** ({fix_number} of {effective_cap}). Report the diff, then stand by for rotation — do not accept further fix assignments."
        )
    } else if fix_number == effective_cap - 1 {
        format!(
            "fix {fix_number} of {effective_cap} — after this one, send `CONTEXT_PRESSURE` BEFORE accepting the next assignment (not after). The advisor needs one fix of runway to rotate."
        )
    } else {
        format!("fix {fix_number} of {effective_cap}")
    };
    let body = format!(
        "## Fix assignment ({budget_note})\n\nFile: `{}`\nProblem:\n{}\nChange:\n{}\nAcceptance:\n{}\n\nMake the edit, send the draft diff back for review, and await CONFIRM / REVISE. Do not drift into unrelated refactors.",
        sanitize_inline(file_path),
        fence(problem.trim(), ""),
        fence(change.trim(), ""),
        fence(acceptance.trim(), "")
    );
    Ok((format!("runner-{runner_id}"), body))
}

/// SendMessage spec `(to, message)` handing a fix wave from a saturated runner
/// to a fresh one. Mirrors `build_runner_handoff_message`.
pub fn build_runner_handoff_message(
    new_runner_id: i64,
    outgoing_runner_id: i64,
    files_touched: &[String],
    invariants: &[String],
    remaining_fixes: &[String],
    extra_context: &str,
) -> (String, String) {
    fn nonempty(v: &[String]) -> Vec<&str> {
        v.iter()
            .map(|s| s.as_str())
            .filter(|s| !s.trim().is_empty())
            .collect()
    }
    let files = nonempty(files_touched);
    let invs = nonempty(invariants);
    let rem = nonempty(remaining_fixes);
    let files_block = if files.is_empty() {
        "- (none yet)".to_string()
    } else {
        fence(&files.join("\n"), "")
    };
    let invariants_block = if invs.is_empty() {
        "- (none)".to_string()
    } else {
        fence(&invs.join("\n"), "")
    };
    let remaining_block = if rem.is_empty() {
        "- (none — you're taking the verify pass)".to_string()
    } else {
        fence(&rem.join("\n"), "")
    };
    let extra = if extra_context.trim().is_empty() {
        String::new()
    } else {
        format!("\n\n## Extra context\n{}", fence(extra_context.trim(), ""))
    };
    let body = format!(
        "## Handoff from runner-{outgoing_runner_id}\n\nYou are runner-{new_runner_id}. runner-{outgoing_runner_id} is saturating context and is being rotated out. You are picking up mid-fix-wave. No need to re-read the full conversation.\n\n## Files already touched\n{files_block}\n\n## Invariants to preserve\n{invariants_block}\n\n## Remaining fixes queued for you\n{remaining_block}{extra}\n\nAcknowledge and wait for the first fix assignment."
    );
    (format!("runner-{new_runner_id}"), body)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn spec(s: &(String, String)) -> String {
        format!("to: {}\n---\n{}", s.0, s.1)
    }

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

    #[test]
    fn batch_message_matches_snapshots() {
        let got = build_runner_batch_message(&two_file_batch(), &HashMap::new()).unwrap();
        assert_eq!(
            got,
            include_str!("../../tests/snapshots/runner_batch_message.txt")
        );
        let g: HashMap<String, String> = [(
            "src/auth.py".to_string(),
            "watch the redirect loop".to_string(),
        )]
        .into_iter()
        .collect();
        let got_g = build_runner_batch_message(&two_file_batch(), &g).unwrap();
        assert_eq!(
            got_g,
            include_str!("../../tests/snapshots/runner_batch_message_with_guidance.txt")
        );
    }

    #[test]
    fn dispatch_messages_match_snapshot() {
        let msgs = build_runner_dispatch_messages(&[two_file_batch()], 3, &HashMap::new()).unwrap();
        let rendered = msgs.iter().map(spec).collect::<Vec<_>>().join("\n===\n");
        assert_eq!(
            rendered,
            include_str!("../../tests/snapshots/runner_dispatch_messages.txt")
        );
    }

    #[test]
    fn fix_assignment_matches_snapshots() {
        let normal = build_fix_assignment_message(
            1,
            "src/auth.py",
            "login() does not rate-limit failed attempts",
            "wrap login() body in `rate_limit(by='ip', per='minute')`",
            "11th failed login in 60s returns 429",
            1,
            5,
            3,
            false,
        )
        .unwrap();
        assert_eq!(
            spec(&normal),
            include_str!("../../tests/snapshots/fix_assignment_normal.txt")
        );

        let penult =
            build_fix_assignment_message(1, "src/auth.py", "x", "y", "z", 4, 5, 3, false).unwrap();
        assert_eq!(
            spec(&penult),
            include_str!("../../tests/snapshots/fix_assignment_penultimate.txt")
        );

        let last =
            build_fix_assignment_message(1, "src/auth.py", "x", "y", "z", 5, 5, 3, false).unwrap();
        assert_eq!(
            spec(&last),
            include_str!("../../tests/snapshots/fix_assignment_last.txt")
        );

        let large =
            build_fix_assignment_message(2, "src/giant.py", "x", "y", "z", 3, 5, 3, true).unwrap();
        assert_eq!(
            spec(&large),
            include_str!("../../tests/snapshots/fix_assignment_large_file_last.txt")
        );
    }

    #[test]
    fn handoff_matches_snapshots() {
        let populated = build_runner_handoff_message(
            2,
            1,
            &["src/auth.py".into(), "src/session.py".into()],
            &[
                "sessions are server-side only".into(),
                "tokens rotate on login".into(),
            ],
            &["src/api.py: add CSRF token check".into()],
            "runner-1 left a partial diff in src/auth.py:120",
        );
        assert_eq!(
            spec(&populated),
            include_str!("../../tests/snapshots/runner_handoff_populated.txt")
        );

        let empty = build_runner_handoff_message(2, 1, &[], &[], &[], "");
        assert_eq!(
            spec(&empty),
            include_str!("../../tests/snapshots/runner_handoff_empty.txt")
        );
    }
}
