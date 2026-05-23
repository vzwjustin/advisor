"""Snapshot tests pinning the exact byte output of every prompt/message builder.

These exist so that any future refactor of ``advisor.orchestrate`` — extracting
helpers, dedenting prompts, reordering sections — surfaces output drift as a
test failure on the exact line that moved. They are the safety net the
refactor proposal called for *before* any restructure starts.

## Updating

When an intentional prompt change lands, regenerate the baselines:

    ADVISOR_UPDATE_SNAPSHOTS=1 pytest tests/test_prompt_snapshots.py

Then re-run normally and commit both the code change and the updated
``tests/snapshots/*.txt`` files in the same commit. The diff on the snapshot
files is the actual prompt diff a reviewer needs to read.

## What is NOT covered

- ``build_advisor_agent`` / ``build_runner_pool_agents`` wrap a prompt in an
  Agent spec dict. The prompt body is already covered; the spec wrapping is
  asserted structurally below rather than as a snapshot, since it would
  duplicate the prompt-body snapshot byte-for-byte and double the update cost.
- ``check_batch_fix_budget`` returns a list of warnings, not a prompt — its
  string contents are pinned by ``test_runner_budget.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from advisor.focus import FocusBatch, FocusTask
from advisor.orchestrate import (
    build_advisor_agent,
    build_advisor_prompt,
    build_fix_assignment_message,
    build_runner_batch_message,
    build_runner_dispatch_messages,
    build_runner_handoff_message,
    build_runner_pool_agents,
    build_runner_pool_prompt,
    build_runner_prompt,
    build_verify_dispatch_prompt,
    build_verify_message,
    default_team_config,
)

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
UPDATE = os.environ.get("ADVISOR_UPDATE_SNAPSHOTS") == "1"


def render_message_spec(spec: dict[str, str]) -> str:
    """Render a ``{"to": ..., "message": ...}`` dict as a diff-friendly block.

    json.dumps escapes the message body's newlines into ``\\n``, collapsing
    the whole prompt onto one line — which means a one-word prompt change
    produces a single full-width diff line that reviewers have to mentally
    unescape. This helper writes the structure on one line and the body
    verbatim below it, so each prompt line gets its own diff line.
    """
    body = spec["message"] if isinstance(spec.get("message"), str) else ""
    return f"to: {spec['to']}\n---\n{body}"


def render_message_specs(specs: list[dict[str, str]]) -> str:
    """Render a list of message specs as a series of fenced blocks."""
    return "\n===\n".join(render_message_spec(s) for s in specs)


def assert_snapshot(name: str, actual: str) -> None:
    """Compare ``actual`` against the on-disk snapshot named ``{name}.txt``.

    With ``ADVISOR_UPDATE_SNAPSHOTS=1``, writes the baseline instead of
    comparing — used to (re)generate snapshots when prompt text is
    intentionally changed.
    """
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    path = SNAPSHOT_DIR / f"{name}.txt"
    if UPDATE:
        path.write_text(actual, encoding="utf-8")
        return
    if not path.exists():
        pytest.fail(f"Snapshot missing: {path}. Run with ADVISOR_UPDATE_SNAPSHOTS=1 to create it.")
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"Snapshot mismatch for {name!r}. "
        f"If the change is intentional, regenerate with "
        f"ADVISOR_UPDATE_SNAPSHOTS=1 pytest tests/test_prompt_snapshots.py"
    )


# ── Fixtures: deterministic inputs ────────────────────────────────────


def _minimal_config():
    """Config with all env-overridable knobs at their hardcoded defaults.

    ``warn_unknown_model=False`` keeps the constructor stderr-quiet for
    test runs. The env layering is exercised separately in
    ``test_orchestrate.py``.
    """
    return default_team_config(
        target_dir="/repo",
        team_name="review",
        warn_unknown_model=False,
    )


def _full_config():
    """Config exercising every optional field — context, test_command,
    distinct large-file caps."""
    return default_team_config(
        target_dir="/repo/src",
        team_name="review",
        file_types="*.py,*.ts",
        max_runners=4,
        min_priority=2,
        context="Audit auth flow for token-handling bugs.",
        max_fixes_per_runner=5,
        large_file_line_threshold=800,
        large_file_max_fixes=3,
        test_command="pytest -q tests/",
        warn_unknown_model=False,
    )


def _cap_1_config():
    """Config with ``max_fixes_per_runner=1`` to lock the
    ``_fix_count_trigger(cap<=1)`` branch."""
    return default_team_config(
        target_dir="/repo",
        team_name="review",
        max_fixes_per_runner=1,
        large_file_max_fixes=1,
        warn_unknown_model=False,
    )


def _matching_caps_config():
    """Config where general and large-file caps are equal — exercises the
    branch in ``build_runner_pool_prompt`` that omits the override block."""
    return default_team_config(
        target_dir="/repo",
        team_name="review",
        max_fixes_per_runner=5,
        large_file_max_fixes=5,
        warn_unknown_model=False,
    )


def _two_file_batch() -> FocusBatch:
    return FocusBatch(
        batch_id=1,
        tasks=(
            FocusTask(file_path="src/auth.py", priority=1, prompt=""),
            FocusTask(file_path="src/session.py", priority=2, prompt=""),
        ),
        complexity="high",
    )


def _single_task() -> FocusTask:
    return FocusTask(file_path="src/util.py", priority=3, prompt="")


# ── Advisor prompts ───────────────────────────────────────────────────


def test_snapshot_advisor_prompt_minimal():
    assert_snapshot("advisor_prompt_minimal", build_advisor_prompt(_minimal_config()))


def test_snapshot_advisor_prompt_full():
    actual = build_advisor_prompt(
        _full_config(),
        history_block="- 2026-05-01: SQL injection in login form (HIGH)\n- 2026-05-08: missing CSRF on /api/transfer (MED)",
    )
    assert_snapshot("advisor_prompt_full", actual)


# ── Per-batch runner prompt (legacy path) ─────────────────────────────


def test_snapshot_runner_prompt_batch():
    assert_snapshot("runner_prompt_batch", build_runner_prompt(_two_file_batch()))


def test_snapshot_runner_prompt_single_task():
    """FocusTask auto-wraps into a one-file FocusBatch."""
    assert_snapshot("runner_prompt_single_task", build_runner_prompt(_single_task()))


def test_snapshot_runner_prompt_with_guidance():
    guidance = {
        "src/auth.py": "check token-refresh race",
        "src/session.py": "verify cookie SameSite",
    }
    assert_snapshot(
        "runner_prompt_with_guidance",
        build_runner_prompt(_two_file_batch(), guidance=guidance),
    )


# ── Pool-runner spawn prompt ──────────────────────────────────────────


def test_snapshot_pool_prompt_default_caps():
    """Different caps for general vs large-file → override block renders."""
    assert_snapshot(
        "pool_prompt_default_caps",
        build_runner_pool_prompt(1, _full_config()),
    )


def test_snapshot_pool_prompt_matching_caps():
    """Matching caps → override block is omitted (different code path)."""
    assert_snapshot(
        "pool_prompt_matching_caps",
        build_runner_pool_prompt(1, _matching_caps_config()),
    )


def test_snapshot_pool_prompt_cap_1():
    """cap=1 triggers the ``immediate ping`` branch of ``_fix_count_trigger``."""
    assert_snapshot(
        "pool_prompt_cap_1",
        build_runner_pool_prompt(2, _cap_1_config()),
    )


# ── Batch dispatch / handoff messages ─────────────────────────────────


def test_snapshot_runner_batch_message():
    assert_snapshot(
        "runner_batch_message",
        build_runner_batch_message(_two_file_batch()),
    )


def test_snapshot_runner_batch_message_with_guidance():
    assert_snapshot(
        "runner_batch_message_with_guidance",
        build_runner_batch_message(
            _two_file_batch(),
            guidance={"src/auth.py": "watch the redirect loop"},
        ),
    )


def test_snapshot_runner_dispatch_messages():
    """Dispatch list serialized as JSON — small enough to read as a diff."""
    batches = [_two_file_batch()]
    msgs = build_runner_dispatch_messages(batches, pool_size=3)
    assert_snapshot("runner_dispatch_messages", render_message_specs(msgs))


# ── Fix assignment (budget banner has three branches) ─────────────────


def test_snapshot_fix_assignment_normal():
    """fix #1 of 5 — plain ``fix N of M`` banner."""
    actual = build_fix_assignment_message(
        runner_id=1,
        file_path="src/auth.py",
        problem="login() does not rate-limit failed attempts",
        change="wrap login() body in `rate_limit(by='ip', per='minute')`",
        acceptance="11th failed login in 60s returns 429",
        fix_number=1,
        max_fixes=5,
        large_file_max_fixes=3,
    )
    assert_snapshot("fix_assignment_normal", render_message_spec(actual))


def test_snapshot_fix_assignment_penultimate():
    """fix #(cap-1) — pre-rotation CONTEXT_PRESSURE warning."""
    actual = build_fix_assignment_message(
        runner_id=1,
        file_path="src/auth.py",
        problem="x",
        change="y",
        acceptance="z",
        fix_number=4,
        max_fixes=5,
        large_file_max_fixes=3,
    )
    assert_snapshot("fix_assignment_penultimate", render_message_spec(actual))


def test_snapshot_fix_assignment_last():
    """fix #cap — LAST FIX banner, rotate after report."""
    actual = build_fix_assignment_message(
        runner_id=1,
        file_path="src/auth.py",
        problem="x",
        change="y",
        acceptance="z",
        fix_number=5,
        max_fixes=5,
        large_file_max_fixes=3,
    )
    assert_snapshot("fix_assignment_last", render_message_spec(actual))


def test_snapshot_fix_assignment_large_file():
    """is_large_file=True → ``large-file cap`` label and tighter cap."""
    actual = build_fix_assignment_message(
        runner_id=2,
        file_path="src/giant.py",
        problem="x",
        change="y",
        acceptance="z",
        fix_number=3,
        max_fixes=5,
        is_large_file=True,
        large_file_max_fixes=3,
    )
    assert_snapshot("fix_assignment_large_file_last", render_message_spec(actual))


# ── Runner handoff (both populated and fallback paths) ────────────────


def test_snapshot_runner_handoff_populated():
    actual = build_runner_handoff_message(
        new_runner_id=2,
        outgoing_runner_id=1,
        files_touched=["src/auth.py", "src/session.py"],
        invariants=["sessions are server-side only", "tokens rotate on login"],
        remaining_fixes=["src/api.py: add CSRF token check"],
        extra_context="runner-1 left a partial diff in src/auth.py:120",
    )
    assert_snapshot("runner_handoff_populated", render_message_spec(actual))


def test_snapshot_runner_handoff_empty():
    """All lists empty — fallback strings render instead of empty fences."""
    actual = build_runner_handoff_message(
        new_runner_id=2,
        outgoing_runner_id=1,
        files_touched=[],
        invariants=[],
        remaining_fixes=[],
    )
    assert_snapshot("runner_handoff_empty", render_message_spec(actual))


# ── Verify dispatch ───────────────────────────────────────────────────


def test_snapshot_verify_dispatch_prompt():
    actual = build_verify_dispatch_prompt(
        all_findings="- src/auth.py:42 — HIGH — SQL injection in login()\n- src/api.py:88 — MED — missing CSRF",
        file_count=7,
        runner_count=3,
    )
    assert_snapshot("verify_dispatch_prompt", actual)


def test_snapshot_verify_message():
    actual = build_verify_message(
        all_findings="- src/auth.py:42 — HIGH — SQL injection in login()",
        file_count=1,
        runner_count=1,
    )
    assert_snapshot("verify_message", render_message_spec(actual))


# ── Agent-spec wrappers: structural assertions, not snapshots ─────────


def test_advisor_agent_wrapping_is_consistent():
    """The spec wraps ``build_advisor_prompt`` — assert structure, not text."""
    config = _minimal_config()
    spec = build_advisor_agent(config)
    assert spec["name"] == "advisor"
    assert spec["subagent_type"] == "advisor-executor"
    assert spec["model"] == config.advisor_model
    assert spec["team_name"] == config.team_name
    assert spec["prompt"] == build_advisor_prompt(config)


def test_pool_agents_wrapping_is_consistent():
    config = _minimal_config()
    specs = build_runner_pool_agents(config, pool_size=2)
    assert len(specs) == 2
    for i, spec in enumerate(specs, start=1):
        assert spec["name"] == f"runner-{i}"
        assert spec["subagent_type"] == "code-review"
        assert spec["model"] == config.runner_model
        assert spec["run_in_background"] is True
        assert spec["prompt"] == build_runner_pool_prompt(i, config)
