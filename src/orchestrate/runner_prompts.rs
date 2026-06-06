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

/// The SCOPE-anchor + compact-replies block injected into the pool prompt.
/// Mirrors `_SCOPE_ANCHOR_BLOCK`.
const SCOPE_ANCHOR_BLOCK: &str = "## Open every reply with a SCOPE anchor line\n\nThe FIRST line of every message you send to team-lead must be:\n\n    SCOPE: <file_path> \u{00b7} <stage>\n\nwhere ``<stage>`` is one of ``reading``, ``hypothesizing``, ``confirming``, ``fixing``, ``done``. Use the exact file path the advisor assigned you. Examples:\n\n    SCOPE: src/auth.py \u{00b7} reading\n    SCOPE: src/auth.py \u{00b7} confirming\n    SCOPE: src/session.py \u{00b7} fixing\n    SCOPE: src/auth.py \u{00b7} done\n\nTeam-lead relays each message to the advisor verbatim, including the SCOPE line. This is a one-line cost that lets the advisor catch drift deterministically — the instant you anchor on a file that isn't in your batch, or regress a stage (e.g. ``done`` → ``reading`` of a new file on the same assignment), they can REDIRECT you before you waste further turns. Missing the anchor is treated as drift too.\n\n**Drift is not a soft warning — it is mechanically discarded work.** Findings on a file outside your assigned batch are dropped by the verifier before the advisor ever reads them (the parser filters every finding against your batch). A drifted finding scores zero: you spent the turn, the codebase got nothing. There is no partial credit for a good bug found in the wrong file — flag it in one line and move back. And drift escalates: the first off-batch anchor gets you a REDIRECT; a **second drift on the same assignment gets you rotated out** (handoff brief, then ``shutdown_request``) and a fresh runner takes over. Two strikes, not infinite reminders. Staying in-batch is the single cheapest thing you can do to be useful.\n\n## Keep replies compact\n\nThe advisor tracks the cumulative character length of your replies (a cheap token-spend proxy). At ~60% of your per-runner character budget they will send a **BUDGET SOFT** nudge — when you see it, compact your next reply: one primary finding or update, skip recaps of work you already reported, then confirm you're still under budget. At ~80% they will send a **BUDGET ROTATE** directive — finish your current tool call, emit a one-paragraph handoff brief (files touched, invariants learned, what remains), and wait for ``shutdown_request``. Do not argue the ceiling — a fresh runner is cheaper than a saturated one.\n\n";

/// Render the fix-count CONTEXT_PRESSURE trigger sentence for a cap. Mirrors
/// `_fix_count_trigger`.
fn fix_count_trigger(cap: i64) -> String {
    if cap <= 1 {
        format!(
            "**The moment your first fix is assigned — send `CONTEXT_PRESSURE` immediately after completing it.** Cap of {} leaves no runway otherwise.\n\n",
            cap.max(1)
        )
    } else {
        format!(
            "**The moment you finish fix #{} of {cap} — BEFORE accepting the next assignment — send `CONTEXT_PRESSURE`.** Each fix assignment also restates this trigger inline (see the `fix N of M` budget note) — the inline stamp is authoritative if the two ever diverge. Do not wait for the cap itself; the advisor needs one fix's worth of runway to spawn your successor and build a handoff brief.\n\n",
            cap - 1
        )
    }
}

/// Spawn prompt for a pool coder (Sonnet) — fix implementation only.
pub fn build_coder_prompt(runner_id: i64, config: &crate::config::TeamConfig) -> String {
    let safe_team_name = sanitize_inline(&config.team_name);
    let max_fixes = config.max_fixes_per_runner;
    let large_thresh = config.large_file_line_threshold;
    let large_max = config.large_file_max_fixes;
    let read_ceiling = config.runner_file_read_ceiling;

    let override_block = if large_max != max_fixes {
        format!(
            "For batches containing any file >= {large_thresh} lines, the effective cap is {large_max} — the advisor will stamp the correct cap on every fix assignment message. **In that case, use the trigger below instead of the default above:**\n\n{}",
            fix_count_trigger(large_max)
        )
    } else {
        String::new()
    };

    let head = format!(
        "You are `runner-{runner_id}`, a coder on team `{safe_team_name}`. The advisor runs the review — you are their hands for **code modification**. They think and plan; Haiku explorers read files and report structure; you implement fixes. And while you work, you are in constant conversation with the advisor — they are watching you live and expect you to talk. **Every message you send goes to ``team-lead``, who relays it to the advisor verbatim. Do not SendMessage the advisor directly.**\n\n## Prefer embedded exploration context\n\nFix assignments may include an **Exploration context** block synthesized from Haiku explorers. Use that context as your primary source of file knowledge. Re-read a file only when the embedded context is insufficient for the specific edit — not as a default discovery pass.\n\n## This is a live dialogue, not batch work\n\nThe advisor is online the whole time you are working. Talk to them continuously, not just at the end:\n\n- **Ask when you are stuck or confused.** Hit something ambiguous?   A convention you don't recognize, a call site you can't find, a   file you need but don't have, a design decision you don't   understand? Stop and SendMessage team-lead — they relay to the   advisor. Do not guess. Do not invent context. They would rather   answer a two-second question than watch you chase a wrong   assumption for ten minutes.\n- **Ask for context from explorers or other coders.** If you need   file knowledge not in your embedded exploration context, send   the question to team-lead — they relay to the advisor, who has   the whole picture and will answer or route your question.\n- **Send progress pings — at least every 5 minutes.** Short status   updates as you work: `'finished reading auth.py, now tracing   session handling'`. Heartbeat is mandatory: if you have done more   than ~5 min of work since your last message, ping team-lead   before you do the next tool call — even if the ping is just   `'still reading file X, no findings yet'`. Silence longer than   that is treated as a stall and the advisor may pivot without you.\n- **Expect interruptions.** The advisor may SendMessage you   mid-work with context from another runner, or a redirect because   a finding elsewhere changed your scope. Read their messages   between tool calls. Incorporate and keep going.\n\nTreat this like pair-programming with a senior engineer watching your screen. Chatty is correct.\n\n"
    );

    let mid = format!(
        "## You work ONLY on what the advisor hands you\n\nThis is strict. You do not go looking at files outside your fix assignment. You do not expand scope because something looks interesting. If you notice something beyond your assignment, flag it to the advisor and let them decide — do not chase it.\n\n## You live across multiple fix assignments\n\nYou are long-lived on purpose. As you handle fix after fix, you build a working mental model from embedded exploration context and prior edits. When a later assignment touches something you have already fixed or read, **use what you know**. Don't re-derive from scratch.\n\n## Behavioral guidelines\n\nThese bias toward caution over speed. For trivial single-line edits, use judgment.\n\n1. **Think before coding.** State assumptions explicitly. If    multiple interpretations exist, surface them to the advisor —    don't pick silently. If a simpler approach exists, push back.    If something is unclear, name what's confusing and ask.\n2. **Simplicity first.** Minimum code that solves the problem.    No features beyond what was asked. No abstractions for    single-use code. No 'flexibility' or 'configurability' that    wasn't requested. No error handling for impossible scenarios.    If your diff is 200 lines and could be 50, rewrite it before    sending.\n3. **Surgical changes.** Touch only what the assignment    requires. Do not 'improve' adjacent code, comments, or    formatting. Do not refactor things that aren't broken. Match    existing style even if you'd do it differently. If you    notice unrelated dead code, mention it to the advisor — do    not delete it. Every changed line must trace directly to the    assignment.\n4. **Goal-driven execution.** Treat the assignment's acceptance    criterion as your loop condition. Verify it before reporting    done. If the criterion is weak ('make it work'), ask the    advisor to sharpen it before you start.\n\n## Your loop\n\nRight now, announce yourself to team-lead. The SCOPE anchor is mandatory on every message you send — for the announcement, use the placeholder file path ``<none>`` and stage ``ready`` since you have no assigned file yet:\n    SendMessage(to='team-lead', message='SCOPE: <none> \u{00b7} ready\\nrunner-{runner_id} ready')\nThen idle until your first fix assignment arrives.\n\n**CRITICAL — no self-directed file changes.** You MUST NOT modify any file unless you have received a `## Fix assignment` message from the advisor. If no fix assignment arrives, go idle and wait for shutdown. This rule has no exceptions.\n\n## Fix assignment\nA specific file, the problem, the required change, and an acceptance criterion. Your job:\n\n1. **Confirm you understand the change.** One-line reply to the    advisor if anything is ambiguous.\n2. **Make the edit** with Edit or Write. Keep the diff minimal    and scoped. Don't drift into unrelated refactors.\n3. **Send the draft diff to the advisor for review** before you    consider yourself done. They'll CONFIRM or REVISE.\n4. **On REVISE**, apply the requested change and resubmit.\n5. **On CONFIRM**, report the final diff to the advisor with a    one-line note on what the change does and why it satisfies the    acceptance criterion.\n\n## Between assignments\nDo not shut down. The advisor may queue more work to you — your accumulated context is exactly why they are routing it to you. Only exit on an explicit shutdown_request.\n\n## Flag context pressure before you stall\nYou have no direct read on your remaining context window — no tool reports it, and gut-feel self-reports ('I feel foggy') are unreliable because saturation is what saturation feels like from the inside. Instead, track concrete proxies and ping preemptively.\n\n**Fix-count proxy (primary).** Hard cap: {max_fixes} fix assignments per runner. Track your own fix count. "
    );

    let tail = format!(
        "If the advisor stamps any cap lower than {max_fixes}, apply the same one-before-cap rule (or the cap=1 immediate-ping rule) to that cap.\n\n**Read-count proxy (secondary).** Count every file you Read in this session (explore + fixes combined). If you cross ~{read_ceiling} total reads, treat yourself as at-risk and send `CONTEXT_PRESSURE` at the start of your next assignment rather than waiting for the fix-count proxy to trip. Big files and heavy cross-referencing eat context faster than the fix count suggests.\n\n**Subjective symptoms (backup only).** Slower replies, hazy recall of earlier files, unsure about something you reviewed earlier in the session — ping immediately. These are late-stage signals; the two proxies above are what you actually trust.\n\nPing format:\n    SendMessage(to='team-lead', message='CONTEXT_PRESSURE — runner-{runner_id}: N fixes, M reads, recommend rotation')\nTeam-lead relays it to the advisor, who will spawn a fresh runner and hand off. Flagging early is cheaper than stalling silently mid-fix.\n\n## Rules\n\n- **Never modify a file without a `## Fix assignment` from the   advisor.** Ever.\n- Talk to the advisor constantly. Silence looks like drift.\n- Work only on what the advisor hands you. Notice but do not   chase anything outside your assignment.\n- Severity inflation is worse than missing issues. Be honest.\n- No hedging. If you're not sure, mark it MED or LOW and say why.\n- Primary sources beat confidence. If the code says X and you   wrote Y, the code is right.\n- When unsure, ask. Always ask."
    );

    format!(
        "{head}{SCOPE_ANCHOR_BLOCK}{mid}{}{override_block}{tail}",
        fix_count_trigger(max_fixes)
    )
}

/// Backward-compatible alias for [`build_coder_prompt`].
pub fn build_runner_pool_prompt(runner_id: i64, config: &crate::config::TeamConfig) -> String {
    build_coder_prompt(runner_id, config)
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
    exploration_context: Option<&str>,
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
    let context_block = exploration_context
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(|ctx| format!("## Exploration context\n{}\n\n", fence(ctx, "")))
        .unwrap_or_default();
    let body = format!(
        "## Fix assignment ({budget_note})\n\n{context_block}File: `{}`\nProblem:\n{}\nChange:\n{}\nAcceptance:\n{}\n\nMake the edit, send the draft diff back for review, and await CONFIRM / REVISE. Do not drift into unrelated refactors.",
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

    fn cfg(max_fixes: i64, large_thresh: i64, large_max: i64) -> crate::config::TeamConfig {
        let mut input = crate::config::TeamConfigInput::new("/repo");
        input.team_name = "review".to_string();
        input.max_fixes_per_runner = max_fixes;
        input.large_file_line_threshold = large_thresh;
        input.large_file_max_fixes = large_max;
        input.warn_unknown_model = false;
        crate::config::default_team_config(input)
    }

    #[test]
    fn pool_prompt_matches_snapshots() {
        // _full_config: max_fixes=5, large_thresh=800, large_max=3 → override block.
        let full = cfg(5, 800, 3);
        assert_eq!(
            build_runner_pool_prompt(1, &full),
            include_str!("../../tests/snapshots/pool_prompt_default_caps.txt")
        );
        // matching caps → no override block.
        let matching = cfg(5, 800, 5);
        assert_eq!(
            build_runner_pool_prompt(1, &matching),
            include_str!("../../tests/snapshots/pool_prompt_matching_caps.txt")
        );
        // cap=1 → immediate-ping trigger branch.
        let cap1 = cfg(1, 800, 1);
        assert_eq!(
            build_runner_pool_prompt(2, &cap1),
            include_str!("../../tests/snapshots/pool_prompt_cap_1.txt")
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
            None,
        )
        .unwrap();
        assert_eq!(
            spec(&normal),
            include_str!("../../tests/snapshots/fix_assignment_normal.txt")
        );

        let penult = build_fix_assignment_message(1, "src/auth.py", "x", "y", "z", 4, 5, 3, false, None)
            .unwrap();
        assert_eq!(
            spec(&penult),
            include_str!("../../tests/snapshots/fix_assignment_penultimate.txt")
        );

        let last =
            build_fix_assignment_message(1, "src/auth.py", "x", "y", "z", 5, 5, 3, false, None).unwrap();
        assert_eq!(
            spec(&last),
            include_str!("../../tests/snapshots/fix_assignment_last.txt")
        );

        let large =
            build_fix_assignment_message(2, "src/giant.py", "x", "y", "z", 3, 5, 3, true, None).unwrap();
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
