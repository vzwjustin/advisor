"""Tests for advisor.runner_budget — scope anchors + output-byte budget."""

from __future__ import annotations

import pytest

from advisor.runner_budget import (
    ROTATE_FRACTION,
    SOFT_WARN_FRACTION,
    budget_status,
    format_budget_nudge,
    new_budget,
    out_of_batch,
    parse_scope_anchor,
    stage_regressed,
    update_budget,
)


class TestParseScopeAnchor:
    def test_middle_dot_separator(self) -> None:
        anchor = parse_scope_anchor("SCOPE: src/auth.py · reading\n...body...")
        assert anchor is not None
        assert anchor.file_path == "src/auth.py"
        assert anchor.stage == "reading"

    def test_pipe_separator(self) -> None:
        anchor = parse_scope_anchor("SCOPE: src/x.py | confirming")
        assert anchor is not None
        assert anchor.stage == "confirming"

    def test_dash_separator(self) -> None:
        anchor = parse_scope_anchor("SCOPE: src/x.py - fixing")
        assert anchor is not None
        assert anchor.stage == "fixing"

    def test_missing_anchor_returns_none(self) -> None:
        assert parse_scope_anchor("no anchor here") is None

    def test_anchor_not_first_line_still_found(self) -> None:
        # Multiline search — anchor anywhere at line start is acceptable
        # so a runner that prepends a greeting still parses.
        anchor = parse_scope_anchor("hello\nSCOPE: a.py · reading\n")
        assert anchor is not None
        assert anchor.file_path == "a.py"

    def test_case_insensitive_keyword(self) -> None:
        anchor = parse_scope_anchor("scope: a.py · reading")
        assert anchor is not None

    def test_strips_backticks_from_path(self) -> None:
        anchor = parse_scope_anchor("SCOPE: `src/x.py` · reading")
        assert anchor is not None
        assert anchor.file_path == "src/x.py"


class TestUpdateBudget:
    def test_bytes_accumulate(self) -> None:
        b = new_budget("runner-1")
        b = update_budget(b, message_text="hello")
        assert b.output_bytes == 5
        b = update_budget(b, message_text="world!")
        assert b.output_bytes == 11

    def test_files_read_deduped(self) -> None:
        b = new_budget("runner-1")
        b = update_budget(b, message_text="SCOPE: a.py · reading\n...")
        b = update_budget(b, message_text="SCOPE: a.py · confirming\n...")
        b = update_budget(b, message_text="SCOPE: b.py · reading\n...")
        assert b.files_read == ("a.py", "b.py")

    def test_fix_counter_bumps_only_on_flag(self) -> None:
        b = new_budget("runner-1")
        b = update_budget(b, message_text="done")
        assert b.fixes_done == 0
        b = update_budget(b, message_text="done", fix_completed=True)
        assert b.fixes_done == 1

    def test_stage_and_file_recorded(self) -> None:
        b = new_budget("runner-1")
        b = update_budget(b, message_text="SCOPE: x.py · hypothesizing")
        assert b.last_stage == "hypothesizing"
        assert b.last_file == "x.py"

    def test_soft_warned_sticks(self) -> None:
        b = new_budget("runner-1", byte_ceiling=100)
        # 60 bytes = 60% — soft threshold tripped
        b = update_budget(b, message_text="a" * 60)
        assert b.soft_warned is True
        # further short messages keep soft_warned true
        b = update_budget(b, message_text="x")
        assert b.soft_warned is True


class TestBudgetStatus:
    def test_ok_below_thresholds(self) -> None:
        b = new_budget("r", byte_ceiling=1000)
        assert budget_status(b) == "OK"

    def test_soft_warn_at_60_pct(self) -> None:
        b = new_budget("r", byte_ceiling=100)
        b = update_budget(b, message_text="x" * 60)
        assert budget_status(b) == "SOFT_WARN"

    def test_rotate_at_80_pct_bytes(self) -> None:
        b = new_budget("r", byte_ceiling=100)
        b = update_budget(b, message_text="x" * 80)
        assert budget_status(b) == "ROTATE"

    def test_rotate_at_file_ceiling(self) -> None:
        b = new_budget("r", file_read_ceiling=3)
        for f in ("a.py", "b.py", "c.py"):
            b = update_budget(b, message_text=f"SCOPE: {f} · reading")
        assert budget_status(b) == "ROTATE"

    def test_rotate_at_fix_ceiling(self) -> None:
        b = new_budget("r", fix_ceiling=2)
        b = update_budget(b, message_text="ok", fix_completed=True)
        b = update_budget(b, message_text="ok", fix_completed=True)
        assert budget_status(b) == "ROTATE"

    def test_rotate_takes_precedence_over_soft(self) -> None:
        # Both conditions true: rotate wins.
        b = new_budget("r", byte_ceiling=100, fix_ceiling=1)
        b = update_budget(b, message_text="x" * 60, fix_completed=True)
        assert budget_status(b) == "ROTATE"


class TestStageRegressed:
    def test_done_to_reading_is_regression(self) -> None:
        assert stage_regressed("done", "reading") is True

    def test_fixing_to_hypothesizing_is_regression(self) -> None:
        assert stage_regressed("fixing", "hypothesizing") is True

    def test_forward_progress_is_not_regression(self) -> None:
        assert stage_regressed("reading", "confirming") is False

    def test_same_stage_is_not_regression(self) -> None:
        assert stage_regressed("reading", "reading") is False

    def test_unknown_stage_is_not_regression(self) -> None:
        # Unknown tokens should not trip a false drift claim.
        assert stage_regressed("done", "daydreaming") is False

    def test_none_prev_is_not_regression(self) -> None:
        assert stage_regressed(None, "reading") is False


class TestOutOfBatch:
    def test_in_batch_returns_false(self) -> None:
        anchor = parse_scope_anchor("SCOPE: src/auth.py · reading")
        assert out_of_batch(anchor, {"src/auth.py", "src/session.py"}) is False

    def test_out_of_batch_returns_true(self) -> None:
        anchor = parse_scope_anchor("SCOPE: src/crypto.py · reading")
        assert out_of_batch(anchor, {"src/auth.py"}) is True

    def test_none_anchor_returns_false(self) -> None:
        # Missing anchor is a separate signal (advisor handles it) —
        # it does not count as drift for this helper.
        assert out_of_batch(None, {"src/auth.py"}) is False

    def test_leading_dot_slash_normalized(self) -> None:
        anchor = parse_scope_anchor("SCOPE: ./src/auth.py · reading")
        assert out_of_batch(anchor, {"src/auth.py"}) is False


class TestFormatBudgetNudge:
    def test_ok_returns_none(self) -> None:
        b = new_budget("r")
        assert format_budget_nudge(b) is None

    def test_soft_warn_message(self) -> None:
        b = new_budget("r", byte_ceiling=100)
        b = update_budget(b, message_text="x" * 60)
        msg = format_budget_nudge(b)
        assert msg is not None
        assert "BUDGET SOFT" in msg

    def test_rotate_message_has_handoff_cue(self) -> None:
        b = new_budget("r", byte_ceiling=100)
        b = update_budget(b, message_text="x" * 85)
        msg = format_budget_nudge(b)
        assert msg is not None
        assert "BUDGET ROTATE" in msg
        assert "handoff" in msg.lower()


class TestNewBudgetValidation:
    @pytest.mark.parametrize("kwarg", ["byte_ceiling", "file_read_ceiling", "fix_ceiling"])
    def test_non_positive_rejected(self, kwarg: str) -> None:
        with pytest.raises(ValueError):
            new_budget("r", **{kwarg: 0})


class TestFractionConstants:
    def test_soft_below_rotate(self) -> None:
        assert 0 < SOFT_WARN_FRACTION < ROTATE_FRACTION < 1.0


class TestPromptWiring:
    def test_advisor_prompt_includes_scope_anchor_clause(self) -> None:
        from advisor.orchestrate import build_advisor_prompt, default_team_config

        cfg = default_team_config(target_dir=".", warn_unknown_model=False)
        text = build_advisor_prompt(cfg)
        assert "SCOPE:" in text
        # Budget thresholds should appear as concrete numbers from the config.
        assert str(cfg.runner_output_byte_ceiling) in text
        assert str(cfg.runner_file_read_ceiling) in text

    def test_runner_prompt_includes_scope_anchor_clause(self) -> None:
        from advisor.orchestrate import build_runner_pool_prompt, default_team_config

        cfg = default_team_config(target_dir=".", warn_unknown_model=False)
        text = build_runner_pool_prompt(1, cfg)
        assert "SCOPE:" in text
        assert "BUDGET SOFT" in text
        assert "BUDGET ROTATE" in text
