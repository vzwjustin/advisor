"""Runner (Sonnet) prompts, agent specs, and dispatch message builders."""

from __future__ import annotations

from ..focus import FocusBatch, FocusTask
from .config import TeamConfig

# ── Helpers ──────────────────────────────────────────────────────


def _format_batch_files(batch: FocusBatch, guidance: dict[str, str] | None = None) -> str:
    g_map = guidance or {}
    lines: list[str] = []
    for t in batch.tasks:
        g = g_map.get(t.file_path, "").strip()
        suffix = f" — {g}" if g else ""
        lines.append(f"- `{t.file_path}` (P{t.priority}){suffix}")
    return "\n".join(lines)


def _coerce_batch(target: FocusBatch | FocusTask) -> FocusBatch:
    """Wrap a single FocusTask into a one-file FocusBatch for legacy callers."""
    if isinstance(target, FocusBatch):
        return target
    return FocusBatch(batch_id=1, tasks=(target,), complexity="medium")


# ── Per-batch runner prompt (used by legacy build_runner_agents path) ─


def build_runner_prompt(
    target: FocusBatch | FocusTask,
    guidance: dict[str, str] | None = None,
) -> str:
    """Sonnet runner prompt — focused on one batch of files, mid-flight checkpoint.

    Accepts either a :class:`FocusBatch` (new pipeline) or a single
    :class:`FocusTask` (legacy — auto-wrapped into a single-file batch).
    """
    batch = _coerce_batch(target)
    files_block = _format_batch_files(batch, guidance)
    return (
        "You are a focused analysis agent. Review ONLY these files:\n\n"
        f"{files_block}\n\n"
        f"Batch complexity: **{batch.complexity}**. "
        "The advisor grouped these together because it judged them reviewable "
        "as a unit — respect the scope.\n\n"
        "## Process\n"
        "1. Read every listed file fully\n"
        "2. For each file, hypothesize issues (bugs, security, logic, edge cases)\n"
        "3. Trace call paths and data flow to confirm or reject each hypothesis\n"
        "4. **Checkpoint with the advisor** before writing your final report.\n"
        "   Send a short draft via `SendMessage(to='advisor')` listing each\n"
        "   candidate finding as `file:line — confidence (HIGH|MED|LOW) — one-line reason`.\n"
        "   Wait for the advisor's reply (CONFIRM / NARROW / REDIRECT) and\n"
        "   incorporate it before finalizing.\n"
        "5. For each confirmed issue, report:\n"
        "   - **File**: path:line_number\n"
        "   - **Severity**: CRITICAL / HIGH / MEDIUM / LOW\n"
        "   - **Description**: what the issue is\n"
        "   - **Evidence**: the code path or proof\n"
        "   - **Fix**: suggested remediation\n"
        "6. If a file is clean, say so explicitly for that file\n\n"
        "Do NOT review other files. Do NOT review files outside this batch. "
        "If you hit a cross-reference, note it but stay scoped.\n\n"
        "When done, send your complete output to the advisor via "
        "SendMessage(to='advisor')."
    )


# ── Pool-runner spawn prompt ─────────────────────────────────────


def build_runner_pool_prompt(runner_id: int, config: TeamConfig) -> str:
    """Spawn prompt for a pool runner — live dialogue with Opus advisor."""
    return (
        f"You are `runner-{runner_id}`, a Sonnet engineer on team "
        f"`{config.team_name}`. The advisor (Opus) runs the review — you are "
        "their hands. They think and plan; you read, find, and fix. And "
        "while you work, you are in constant conversation with them — they "
        "are watching you live and expect you to talk.\n\n"
        "# This is a live dialogue, not batch work\n\n"
        "The advisor is online the whole time you are working. Talk to them "
        "continuously, not just at the end:\n\n"
        "- **Ask when you are stuck or confused.** Hit something ambiguous? "
        "  A convention you don't recognize, a call site you can't find, a "
        "  file you need but don't have, a design decision you don't "
        "  understand? Stop and SendMessage the advisor. Do not guess. Do "
        "  not invent context. They would rather answer a two-second "
        "  question than watch you chase a wrong assumption for ten minutes.\n"
        "- **Ask for context from other runners.** Your peers are reviewing "
        "  other files. If you need to know what they've seen (did runner-2 "
        "  find auth uses JWT or sessions?), ask the advisor — they have "
        "  the whole picture and will answer or route your question.\n"
        "- **Send progress pings — at least every 5 minutes.** Short status "
        "  updates as you work: `'finished reading auth.py, now tracing "
        "  session handling'`. Heartbeat is mandatory: if you have done more "
        "  than ~5 min of work since your last message, ping the advisor "
        "  before you do the next tool call — even if the ping is just "
        "  `'still reading file X, no findings yet'`. Silence longer than "
        "  that is treated as a stall and the advisor may pivot without you.\n"
        "- **Expect interruptions.** The advisor may SendMessage you "
        "  mid-work with context from another runner, or a redirect because "
        "  a finding elsewhere changed your scope. Read their messages "
        "  between tool calls. Incorporate and keep going.\n\n"
        "Treat this like pair-programming with a senior engineer watching "
        "your screen. Chatty is correct.\n\n"
        "# You work ONLY on what the advisor hands you\n\n"
        "This is strict. You do not go looking at files outside your "
        "assignment. You do not expand scope because something looks "
        "interesting. If you notice something beyond your batch, flag it "
        "to the advisor and let them decide — do not chase it. The advisor "
        "sees the whole codebase and makes the scope calls.\n\n"
        "# You live across multiple assignments\n\n"
        "You are long-lived on purpose. As you handle assignment after "
        "assignment, you build a working mental model of this codebase — "
        "which modules import which, which invariants hold, what patterns "
        "repeat. A fresh runner per batch would throw all of that away. "
        "When a later assignment touches something you have already seen, "
        "**use what you know**. Don't re-derive. Bring it up when it helps "
        "the advisor see the whole picture.\n\n"
        "# Your loop\n\n"
        "Right now, announce yourself:\n"
        f"    SendMessage(to='advisor', message='runner-{runner_id} ready')\n"
        "Then idle until your first assignment arrives. The advisor will "
        "SendMessage you with one of two kinds of assignment:\n\n"
        "## Explore assignment\n"
        "A list of files with one-line guidance on what to look for. Your "
        "job:\n\n"
        "1. **Read every file in the batch end-to-end.** No skimming. You "
        "   are the one person who will actually look at these.\n"
        "2. **Hypothesize.** What could go wrong? Bugs, security, logic "
        "   errors, edge cases, bad defaults, silent failures, race "
        "   conditions.\n"
        "3. **Trace to confirm or kill each hypothesis.** Follow the data "
        "   flow. Check call sites. Report a specific `file:line` and a "
        "   repro — not a vibe.\n"
        "4. **Ping findings as you find them.** Don't hoard them for a "
        "   final dump. Send each hot one to the advisor as you confirm it: "
        "   `'file:line — severity — one-line'`. They will CONFIRM, NARROW, "
        "   or REDIRECT in real time.\n"
        "5. **Draft checkpoint before finalizing.** Send your full draft "
        "   findings list to the advisor. Wait for:\n"
        "   - **CONFIRM** — ship it\n"
        "   - **NARROW** — drop the specific items they name\n"
        "   - **REDIRECT** — re-focus where they point\n"
        "   Push back once with file:line evidence only if they missed "
        "   something primary-source.\n"
        "6. **Send the final report to the advisor** (the advisor "
        "   verifies and relays to team-lead):\n"
        "       SendMessage(to='advisor', message=<structured findings>)\n"
        "   Each issue: File, Severity, Description, Evidence, Fix.\n\n"
        "## Fix assignment\n"
        "A specific file, the problem, the required change, and an "
        "acceptance criterion. Your job:\n\n"
        "1. **Confirm you understand the change.** One-line reply to the "
        "   advisor if anything is ambiguous.\n"
        "2. **Make the edit** with Edit or Write. Keep the diff minimal "
        "   and scoped. Don't drift into unrelated refactors.\n"
        "3. **Send the draft diff to the advisor for review** before you "
        "   consider yourself done. They'll CONFIRM or REVISE.\n"
        "4. **On REVISE**, apply the requested change and resubmit.\n"
        "5. **On CONFIRM**, report the final diff to the advisor with a "
        "   one-line note on what the change does and why it satisfies the "
        "   acceptance criterion.\n\n"
        "## Between assignments\n"
        "Do not shut down. The advisor may queue more work to you — your "
        "accumulated context is exactly why they are routing it to you. "
        "Only exit on an explicit shutdown_request.\n\n"
        "## Flag context pressure before you stall\n"
        f"Hard cap: {config.max_fixes_per_runner} fix assignments per "
        "runner. Track your own fix count. As you approach the cap — or "
        "if you notice your replies getting slower, your recall of earlier "
        "files getting hazy, or you're unsure about a file you reviewed "
        "earlier in the session — proactively ping the advisor: "
        "`SendMessage(to='advisor', message='CONTEXT_PRESSURE — N fixes "
        "deep, recommend rotation')`. The advisor will spawn a fresh "
        "runner and hand off. Flagging early is cheaper than stalling "
        "silently mid-fix.\n\n"
        "# Rules\n\n"
        "- Talk to the advisor constantly. Silence looks like drift.\n"
        "- Work only on what the advisor hands you. Notice but do not "
        "  chase anything outside your assignment.\n"
        "- Severity inflation is worse than missing issues. Be honest.\n"
        "- No hedging. If you're not sure, mark it MED or LOW and say why.\n"
        "- Primary sources beat confidence. If the code says X and you "
        "  wrote Y, the code is right.\n"
        "- **Pre-finding verification is mandatory.** Before you report "
        "  anything of the form `X is missing from Y` or `Z is undefined` "
        "  or `no caller exists for W`, you MUST grep for the name in the "
        "  relevant file and include the grep command + result (or the "
        "  explicit `file:line` + surrounding snippet) as the evidence "
        "  line. An unverified absence-claim is a bug in your report, not "
        "  a finding. If grep contradicts your mental model, the grep wins.\n"
        "- When unsure, ask. Always ask."
    )


# ── Pool / batch agent + message specs ───────────────────────────


def build_runner_pool_agents(
    config: TeamConfig,
    pool_size: int | None = None,
) -> list[dict[str, object]]:
    """Agent specs for the initial runner pool.

    Pool size defaults to ``config.max_runners``. Pool runners are spawned
    once upfront; the advisor assigns work to them via SendMessage rather
    than spawning fresh agents per batch.
    """
    size = pool_size if pool_size is not None else config.max_runners
    return [
        {
            "description": f"Pool runner {i} — waits for advisor dispatch",
            "name": f"runner-{i}",
            "subagent_type": "code-review",
            "model": config.runner_model,
            "team_name": config.team_name,
            "run_in_background": True,
            "prompt": build_runner_pool_prompt(i, config),
        }
        for i in range(1, size + 1)
    ]


def build_runner_batch_message(
    batch: FocusBatch,
    guidance: dict[str, str] | None = None,
) -> str:
    """SendMessage payload assigning a batch of files to a pool runner.

    The advisor should call ``SendMessage(to='runner-<N>', message=<this>)``
    for each batch in its dispatch plan.
    """
    files_block = _format_batch_files(batch, guidance)
    return (
        f"## New batch assignment (batch {batch.batch_id}, "
        f"complexity: {batch.complexity})\n\n"
        "Review ONLY these files:\n\n"
        f"{files_block}\n\n"
        "Process:\n"
        "1. Read every listed file fully\n"
        "2. Hypothesize issues (bugs, security, logic, edge cases)\n"
        "3. Trace call paths to confirm or reject each\n"
        "4. Checkpoint draft findings with the advisor via "
        "`SendMessage(to='advisor')` before finalizing\n"
        "5. Wait for CONFIRM / NARROW / REDIRECT and incorporate\n"
        "6. For each confirmed issue, report File/Severity/Description/"
        "Evidence/Fix\n"
        "7. Send your complete output to the advisor via "
        "`SendMessage(to='advisor')`\n"
        "8. Then wait for your next batch\n\n"
        "Do NOT review files outside this batch."
    )


def build_runner_dispatch_messages(
    batches: list[FocusBatch],
    pool_size: int,
    guidance: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """SendMessage specs to hand each batch to its pool runner.

    Args:
        batches: Batches to dispatch; each batch's ``batch_id`` determines
            runner routing.
        pool_size: Number of runners in the pool. Raises :class:`ValueError`
            if any ``batch_id`` exceeds this — that dispatch would silently
            target a never-spawned runner.
        guidance: Optional dict mapping file_path -> one-line guidance.

    Returns:
        A list of dicts shaped like
        ``{"to": "runner-N", "message": <batch assignment text>}`` — pass each
        one to ``SendMessage`` to route work to the existing runner pool.
    """
    if batches:
        ids = [b.batch_id for b in batches]
        bad = [i for i in ids if i < 1]
        if bad:
            raise ValueError(
                f"batch_id must be >= 1; got {bad}: dispatch would route to a non-existent runner"
            )
        empty = [b.batch_id for b in batches if not b.tasks]
        if empty:
            raise ValueError(
                f"batch_id(s) {empty} have no tasks: dispatch would send an empty assignment"
            )
        if len(set(ids)) != len(ids):
            raise ValueError(
                f"duplicate batch_id in dispatch list {ids}: "
                f"two batches would collide on the same runner"
            )
        max_batch_id = max(ids)
        if max_batch_id > pool_size:
            raise ValueError(
                f"batch_id {max_batch_id} exceeds pool_size {pool_size}: "
                f"dispatch would route to a never-spawned runner"
            )
    return [
        {
            "to": f"runner-{batch.batch_id}",
            "message": build_runner_batch_message(batch, guidance),
        }
        for batch in batches
    ]


def build_runner_agents(
    items: list[FocusBatch] | list[FocusTask],
    config: TeamConfig,
    guidance: dict[str, str] | None = None,
) -> list[dict[str, object]]:
    """Claude Code Agent call specs for all runners (dispatch in parallel).

    Args:
        items: Either a list of :class:`FocusBatch` (new pipeline) or a list
            of :class:`FocusTask` (legacy, one runner per file). FocusTask
            items are auto-wrapped into single-file batches.
        config: Team configuration.
        guidance: Optional dict mapping file_path -> one-line guidance.

    Returns:
        A new list of Agent call specs, one per runner.
    """
    batches: list[FocusBatch] = []
    for i, item in enumerate(items, 1):
        if isinstance(item, FocusBatch):
            batches.append(item)
        else:
            # At this point ``item`` must be a FocusTask — the union type
            # of ``items`` forbids any other concrete value.
            assert isinstance(item, FocusTask)
            batches.append(FocusBatch(batch_id=i, tasks=(item,), complexity="medium"))

    agents: list[dict[str, object]] = []
    for batch in batches:
        file_count = len(batch.tasks)
        first = batch.tasks[0].file_path if batch.tasks else "empty"
        desc = (
            f"Analyze {first}"
            if file_count == 1
            else f"Analyze batch of {file_count} files (top P{batch.top_priority})"
        )
        agents.append(
            {
                "description": desc,
                "name": f"runner-{batch.batch_id}",
                "subagent_type": "code-review",
                "model": config.runner_model,
                "team_name": config.team_name,
                "run_in_background": True,
                "prompt": build_runner_prompt(batch, guidance=guidance),
            }
        )
    return agents


# ── Handoff message (saturated runner → fresh runner) ────────────


def build_runner_handoff_message(
    new_runner_id: int,
    outgoing_runner_id: int,
    files_touched: list[str],
    invariants: list[str],
    remaining_fixes: list[str],
    extra_context: str = "",
) -> dict[str, str]:
    """SendMessage spec handing the fix wave off from a saturated runner to a fresh one.

    Used when a runner hits ``max_fixes_per_runner`` or pings
    ``CONTEXT_PRESSURE``. The brief gives the incoming runner the minimum
    context it needs without replaying the full conversation.
    """
    files_block = "\n".join(f"- {p}" for p in files_touched) if files_touched else "- (none yet)"
    invariants_block = "\n".join(f"- {inv}" for inv in invariants) if invariants else "- (none)"
    remaining_block = (
        "\n".join(f"- {fx}" for fx in remaining_fixes)
        if remaining_fixes
        else "- (none — you're taking the verify pass)"
    )
    extra = f"\n\n## Extra context\n{extra_context.strip()}" if extra_context.strip() else ""
    body = (
        f"## Handoff from runner-{outgoing_runner_id}\n\n"
        f"You are runner-{new_runner_id}. runner-{outgoing_runner_id} is "
        "saturating context and is being rotated out. You are picking up "
        "mid-fix-wave. No need to re-read the full conversation.\n\n"
        f"## Files already touched\n{files_block}\n\n"
        f"## Invariants to preserve\n{invariants_block}\n\n"
        f"## Remaining fixes queued for you\n{remaining_block}"
        f"{extra}\n\n"
        "Acknowledge and wait for the first fix assignment."
    )
    return {"to": f"runner-{new_runner_id}", "message": body}
