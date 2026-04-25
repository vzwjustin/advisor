"""Runner prompts, agent specs, and dispatch message builders."""

from __future__ import annotations

from ..focus import FocusBatch, FocusTask
from ._fence import fence
from ._schema import FINDING_SCHEMA
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


def _fix_count_trigger(cap: int) -> str:
    """Render the fix-count CONTEXT_PRESSURE trigger sentence for a given cap.

    When ``cap <= 1`` there is no "one-before-cap" fix to anchor on, so the
    runner is told to ping immediately after the first fix instead.
    """
    if cap <= 1:
        return (
            "**The moment your first fix is assigned — send "
            "`CONTEXT_PRESSURE` immediately after completing it.** Cap of 1 "
            "leaves no runway otherwise.\n\n"
        )
    return (
        f"**The moment you finish fix #{cap - 1} of {cap} — BEFORE "
        "accepting the next assignment — send `CONTEXT_PRESSURE`.** Do "
        "not wait for the cap itself; the advisor needs one fix's worth "
        "of runway to spawn your successor and build a handoff brief. "
        "If you only flag at the cap, rotation happens mid-stall, which "
        "is the case this rule exists to prevent.\n\n"
    )


# ── Per-batch runner prompt (used by legacy build_runner_agents path) ─


def build_runner_prompt(
    target: FocusBatch | FocusTask,
    guidance: dict[str, str] | None = None,
) -> str:
    """Runner prompt — focused on one batch of files, mid-flight checkpoint.

    Accepts either a :class:`FocusBatch` (new pipeline) or a single
    :class:`FocusTask` (legacy — auto-wrapped into a single-file batch).
    """
    batch = _coerce_batch(target)
    files_block = _format_batch_files(batch, guidance)
    return (
        "You are a runner. Review ONLY these files:\n\n"
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
        f"5. For each confirmed issue, report:\n{FINDING_SCHEMA}\n"
        "6. If a file is clean, say so explicitly for that file\n\n"
        "Do NOT review other files. Do NOT review files outside this batch. "
        "If you hit a cross-reference, note it but stay scoped.\n\n"
        "When done, send your complete output to the advisor via "
        "SendMessage(to='advisor')."
    )


# ── Pool-runner spawn prompt ─────────────────────────────────────


_SCOPE_ANCHOR_BLOCK = (
    "## Open every reply with a SCOPE anchor line\n\n"
    "The FIRST line of every message you send to the advisor must be:\n\n"
    "    SCOPE: <file_path> · <stage>\n\n"
    "where ``<stage>`` is one of ``reading``, ``hypothesizing``, "
    "``confirming``, ``fixing``, ``done``. Use the exact file path the "
    "advisor assigned you. Examples:\n\n"
    "    SCOPE: src/auth.py · reading\n"
    "    SCOPE: src/auth.py · confirming\n"
    "    SCOPE: src/session.py · fixing\n"
    "    SCOPE: src/auth.py · done\n\n"
    "This is a one-line cost that lets the advisor catch drift "
    "deterministically — the instant you anchor on a file that isn't in "
    "your batch, or regress a stage (e.g. ``done`` → ``reading`` of a "
    "new file on the same assignment), they can REDIRECT you before you "
    "waste further turns. Missing the anchor is treated as drift too.\n\n"
    "## Keep replies compact\n\n"
    "The advisor tracks the cumulative character length of your replies "
    "(a cheap token-spend proxy). At ~60% of your per-runner character "
    "budget they will send a **BUDGET SOFT** nudge — when you see it, "
    "compact your next reply: one primary finding or update, skip "
    "recaps of work you already reported, then confirm you're still "
    "under budget. At ~80% they will send a **BUDGET ROTATE** directive "
    "— finish your current tool call, emit a one-paragraph handoff "
    "brief (files touched, invariants learned, what remains), and wait "
    "for ``shutdown_request``. Do not argue the ceiling — a fresh "
    "runner is cheaper than a saturated one.\n\n"
)


def build_runner_pool_prompt(runner_id: int, config: TeamConfig) -> str:
    """Spawn prompt for a pool runner — live dialogue with the advisor."""
    return (
        f"You are `runner-{runner_id}`, a runner on team "
        f"`{config.team_name}`. The advisor runs the review — you are "
        "their hands. They think and plan; you read, find, and fix. And "
        "while you work, you are in constant conversation with them — they "
        "are watching you live and expect you to talk.\n\n"
        "## This is a live dialogue, not batch work\n\n"
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
        + _SCOPE_ANCHOR_BLOCK
        + "## You work ONLY on what the advisor hands you\n\n"
        "This is strict. You do not go looking at files outside your "
        "assignment. You do not expand scope because something looks "
        "interesting. If you notice something beyond your batch, flag it "
        "to the advisor and let them decide — do not chase it. The advisor "
        "sees the whole codebase and makes the scope calls.\n\n"
        "## You live across multiple assignments\n\n"
        "You are long-lived on purpose. As you handle assignment after "
        "assignment, you build a working mental model of this codebase — "
        "which modules import which, which invariants hold, what patterns "
        "repeat. A fresh runner per batch would throw all of that away. "
        "When a later assignment touches something you have already seen, "
        "**use what you know**. Don't re-derive. Bring it up when it helps "
        "the advisor see the whole picture.\n\n"
        "## Your loop\n\n"
        "Right now, announce yourself:\n"
        f"    SendMessage(to='advisor', message='runner-{runner_id} ready')\n"
        "Then idle until your first assignment arrives. The advisor will "
        "SendMessage you with one of two kinds of assignment:\n\n"
        "**CRITICAL — no self-directed file changes.** You MUST NOT modify "
        "any file unless you have received a `## Fix assignment` message "
        "from the advisor. Completing your explore report and seeing obvious "
        "problems does NOT authorize you to fix them. If no fix assignment "
        "arrives, go idle and wait for shutdown. This rule has no exceptions.\n\n"
        "## Explore assignment\n"
        "A list of files with one-line guidance on what to look for. Your "
        "job:\n\n"
        "1. **Read every file in the batch end-to-end.** No skimming. You "
        "   are the one person who will actually look at these.\n"
        "2. **Hypothesize — think step by step.** List each candidate issue "
        "   explicitly before chasing any of them. What could go wrong? "
        "   Bugs, security, logic errors, edge cases, bad defaults, silent "
        "   failures, race conditions. Write the list first, then trace.\n"
        "3. **Trace to confirm or kill each hypothesis.** Follow the data "
        "   flow. Check call sites. Report a specific `file:line` and a "
        "   repro — not a vibe.\n"
        "   - **5 Whys:** when you confirm a bug, ask *why* five times — "
        "     why does this fail, why is that condition reachable, why was "
        "     the code written this way — until you hit root cause or a "
        "     deliberate design decision. Fix the cause, not the symptom.\n"
        "   - **What ifs:** before reporting a finding, flip it — what if "
        "     the code is actually correct and you're missing context? What "
        "     if there's a guard you haven't found yet? One challenge per "
        "     finding before you send it to the advisor.\n"
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
        f"   Each issue:\n{FINDING_SCHEMA}\n\n"
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
        "You have no direct read on your remaining context window — no "
        "tool reports it, and gut-feel self-reports ('I feel foggy') are "
        "unreliable because saturation is what saturation feels like from "
        "the inside. Instead, track concrete proxies and ping preemptively.\n\n"
        f"**Fix-count proxy (primary).** Hard cap: "
        f"{config.max_fixes_per_runner} fix assignments per runner. Track "
        "your own fix count. "
        + _fix_count_trigger(config.max_fixes_per_runner)
        + f"**Read-count proxy (secondary).** Count every file you Read in "
        f"this session (explore + fixes combined). If you cross "
        f"~{config.runner_file_read_ceiling} total reads, treat yourself "
        "as at-risk and send `CONTEXT_PRESSURE` at the start of your next "
        "assignment rather than waiting for the fix-count proxy to trip. "
        "Big files and heavy cross-referencing eat context faster than "
        "the fix count suggests.\n\n"
        "**Subjective symptoms (backup only).** Slower replies, hazy "
        "recall of earlier files, unsure about something you reviewed "
        "earlier in the session — ping immediately. These are late-stage "
        "signals; the two proxies above are what you actually trust.\n\n"
        "Ping format:\n"
        "    SendMessage(to='advisor', message='CONTEXT_PRESSURE — "
        "N fixes, M reads, recommend rotation')\n"
        "The advisor will spawn a fresh runner and hand off. Flagging "
        "early is cheaper than stalling silently mid-fix.\n\n"
        "## Rules\n\n"
        "- **Never modify a file without a `## Fix assignment` from the "
        "  advisor.** Explore reports do not authorize edits. Ever.\n"
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
        f"6. For each confirmed issue, report:\n{FINDING_SCHEMA}\n"
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
            label = "batch_id" if len(empty) == 1 else "batch_ids"
            verb = "has" if len(empty) == 1 else "have"
            raise ValueError(
                f"{label} {empty} {verb} no tasks: dispatch would send an empty assignment"
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
        if not batch.tasks:
            # Empty batches are a programmer error in the upstream
            # batcher — masking with an ``"empty"`` placeholder produced
            # downstream prompts that referenced a fake file. Fail loud
            # here so the bug surfaces at construction time.
            raise ValueError(f"runner pool batch {batch.batch_id} has no tasks")
        first = batch.tasks[0].file_path
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


# ── Fix assignment message (budget-stamped, mechanically capped) ─


def build_fix_assignment_message(
    *,
    runner_id: int,
    file_path: str,
    problem: str,
    change: str,
    acceptance: str,
    fix_number: int,
    max_fixes: int,
    is_large_file: bool = False,
    large_file_max_fixes: int = 3,
) -> dict[str, str]:
    """SendMessage spec for a fix assignment with the budget stamped in-band.

    The **effective cap** for this runner is ``large_file_max_fixes`` if
    ``is_large_file`` is True, else ``max_fixes``. When a batch mixes file
    sizes, the caller is responsible for passing the *lowest* applicable
    cap — this function does not try to guess.

    Budget enforcement:

    * ``fix_number < 1`` raises :class:`ValueError` — fix numbering is
      1-indexed, and ``0`` is almost always a bug in the caller's ledger.
    * ``fix_number > effective_cap`` raises :class:`ValueError` with an
      explicit rotation hint. This is a **hard invariant** — the advisor
      cannot dispatch an over-cap fix without the builder failing, so
      rotation decisions cannot be accidentally skipped.

    Budget messaging embedded in the message body:

    * ``fix_number == effective_cap - 1`` — explicit reminder that the
      runner must ping ``CONTEXT_PRESSURE`` BEFORE accepting the next
      assignment.
    * ``fix_number == effective_cap`` — 'this is your last fix; stand by
      for rotation after reporting' banner.

    Every dispatched fix carries the current budget state so the runner
    sees it on every turn, not just once in its spawn prompt.

    Args:
        runner_id: Destination runner (``runner-{runner_id}``).
        file_path: File to edit.
        problem: One-line description of the issue being fixed.
        change: Exactly what edit / behavior change is required.
        acceptance: How the runner will know the fix is correct.
        fix_number: 1-indexed fix count for this runner's session.
        max_fixes: Hard cap from :class:`TeamConfig.max_fixes_per_runner`.
        is_large_file: Whether any file in this runner's current batch is
            at or above the large-file threshold. When True, the tighter
            ``large_file_max_fixes`` cap applies.
        large_file_max_fixes: Cap from
            :class:`TeamConfig.large_file_max_fixes`.

    Returns:
        ``{"to": "runner-N", "message": <budget-stamped body>}``.
    """
    if fix_number < 1:
        raise ValueError(f"fix_number must be >= 1 (got {fix_number}); fix numbering is 1-indexed")

    effective_cap = large_file_max_fixes if is_large_file else max_fixes
    cap_label = "large-file cap" if is_large_file else "cap"

    if fix_number > effective_cap:
        raise ValueError(
            f"fix_number={fix_number} exceeds {cap_label}={effective_cap} "
            f"for runner-{runner_id}: rotate to a fresh runner before "
            f"dispatching this fix. Use build_runner_handoff_message to "
            f"hand off the remaining fix queue."
        )

    # Budget status line baked into the header so the runner sees it on
    # every single message, not just in its spawn prompt. By the time a
    # runner is three fixes deep, the spawn prompt is far behind — the
    # reminder has to travel with each assignment.
    if fix_number == effective_cap:
        budget_note = (
            f"**LAST FIX** ({fix_number} of {effective_cap}). Report the "
            "diff, then stand by for rotation — do not accept further "
            "fix assignments."
        )
    elif fix_number == effective_cap - 1:
        budget_note = (
            f"fix {fix_number} of {effective_cap} — after this one, send "
            "`CONTEXT_PRESSURE` BEFORE accepting the next assignment "
            "(not after). The advisor needs one fix of runway to rotate."
        )
    else:
        budget_note = f"fix {fix_number} of {effective_cap}"

    # Fence untrusted fields — caller-supplied ``## `` or triple-backticks
    # would otherwise corrupt the assignment delimiters / escape into
    # markdown sections the runner treats as advisor instructions.
    body = (
        f"## Fix assignment ({budget_note})\n\n"
        f"File: `{file_path}`\n"
        f"Problem:\n{fence(problem.strip())}\n"
        f"Change:\n{fence(change.strip())}\n"
        f"Acceptance:\n{fence(acceptance.strip())}\n\n"
        "Make the edit, send the draft diff back for review, and await "
        "CONFIRM / REVISE. Do not drift into unrelated refactors."
    )
    return {"to": f"runner-{runner_id}", "message": body}


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


# ── Pre-flight budget validator ──────────────────────────────────


def check_batch_fix_budget(
    batches: list[FocusBatch],
    config: TeamConfig,
    file_line_counts: dict[str, int] | None = None,
) -> list[str]:
    """Warn about batches whose size could over-run per-runner fix caps.

    Not every file in a batch will require a fix — explore produces
    findings, and only a subset become fix assignments. So this is a
    *warning*, not a hard error: it flags dispatch plans where the
    worst-case fix load (every file → a fix) would exceed the cap
    without a mid-batch rotation.

    Checks performed per batch:

    * ``len(batch.tasks) > max_fixes_per_runner`` — worst-case overrun.
      Suggests either splitting the batch or planning a rotation.
    * If ``file_line_counts`` is provided and any file in the batch is
      at/above ``large_file_line_threshold``, the tighter
      ``large_file_max_fixes`` cap applies — the check compares
      ``len(batch.tasks)`` against that lower number instead.

    Args:
        batches: Planned batches from :func:`create_focus_batches`.
        config: Team configuration with the two caps and the threshold.
        file_line_counts: Optional mapping of file_path → line count.
            When absent, only the general cap is checked; large-file
            caps are not applied because we cannot know which files
            trip the threshold without reading them.

    Returns:
        A list of human-readable warning strings (one per over-cap
        batch). Empty list means every batch is within budget.
    """
    warnings: list[str] = []
    for batch in batches:
        file_count = len(batch.tasks)
        effective_cap = config.max_fixes_per_runner
        cap_reason = "max_fixes_per_runner"

        if file_line_counts is not None:
            large_paths = [
                t.file_path
                for t in batch.tasks
                if file_line_counts.get(t.file_path, 0) >= config.large_file_line_threshold
            ]
            if large_paths and config.large_file_max_fixes < effective_cap:
                effective_cap = config.large_file_max_fixes
                cap_reason = (
                    f"large_file_max_fixes (triggered by {large_paths[0]}"
                    + (f" +{len(large_paths) - 1} more" if len(large_paths) > 1 else "")
                    + ")"
                )

        if file_count > effective_cap:
            warnings.append(
                f"batch {batch.batch_id}: {file_count} tasks exceeds "
                f"{cap_reason}={effective_cap}. Worst-case (every file "
                f"needs a fix) would require mid-batch rotation. Consider "
                f"--batch-size {effective_cap} or splitting this batch."
            )
    return warnings
