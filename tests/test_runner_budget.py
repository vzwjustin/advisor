"""Tests for advisor.runner_budget — scope anchors + output-char budget."""

from __future__ import annotations

import pytest

from advisor.runner_budget import (
    ROTATE_FRACTION,
    SOFT_WARN_FRACTION,
    budget_status,
    format_budget_nudge,
    new_budget,
    normalize_batch_files,
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

    # ── Regression: PR#9 / Gemini-HIGH / Codex-P1 ──────────────────
    # Hyphens inside the file path used to be consumed by the separator
    # group, turning `SCOPE: src/my-file.py · reading` into
    # file=`src/my`, stage=`file`. The regex now requires whitespace
    # around the separator so hyphenated filenames survive.

    def test_hyphen_in_filename_middle_dot(self) -> None:
        anchor = parse_scope_anchor("SCOPE: src/my-file.py · reading")
        assert anchor is not None
        assert anchor.file_path == "src/my-file.py"
        assert anchor.stage == "reading"

    def test_hyphen_in_filename_pipe(self) -> None:
        anchor = parse_scope_anchor("SCOPE: src/multi-word-name.py | fixing")
        assert anchor is not None
        assert anchor.file_path == "src/multi-word-name.py"
        assert anchor.stage == "fixing"

    def test_hyphen_in_directory_and_filename(self) -> None:
        anchor = parse_scope_anchor("SCOPE: my-pkg/sub-dir/my-mod.py · confirming")
        assert anchor is not None
        assert anchor.file_path == "my-pkg/sub-dir/my-mod.py"

    def test_hyphen_separator_still_works_without_path_hyphen(self) -> None:
        # Dash separator still parses when the filename is plain.
        anchor = parse_scope_anchor("SCOPE: auth.py - reading")
        assert anchor is not None
        assert anchor.file_path == "auth.py"
        assert anchor.stage == "reading"

    def test_hyphen_separator_with_hyphen_in_path(self) -> None:
        # Ambiguous case: hyphen in path AND hyphen as separator. The
        # regex is greedy up to the last ``\s+-\s+`` so the split lands
        # at the real separator.
        anchor = parse_scope_anchor("SCOPE: my-file.py - reading")
        assert anchor is not None
        assert anchor.file_path == "my-file.py"
        assert anchor.stage == "reading"


class TestUpdateBudget:
    def test_chars_accumulate(self) -> None:
        b = new_budget("runner-1")
        b = update_budget(b, message_text="hello")
        assert b.output_chars == 5
        b = update_budget(b, message_text="world!")
        assert b.output_chars == 11

    def test_files_read_deduped(self) -> None:
        b = new_budget("runner-1")
        b = update_budget(b, message_text="SCOPE: a.py · reading\n...")
        b = update_budget(b, message_text="SCOPE: a.py · confirming\n...")
        b = update_budget(b, message_text="SCOPE: b.py · reading\n...")
        assert b.files_read == ("a.py", "b.py")

    def test_files_read_deduped_after_path_normalization(self) -> None:
        b = new_budget("runner-1")
        b = update_budget(b, message_text="SCOPE: ./src\\auth.py · reading\n...")
        b = update_budget(b, message_text="SCOPE: src/auth.py · confirming\n...")
        b = update_budget(b, message_text="no anchor", file_read="src\\auth.py:42")
        assert b.files_read == ("src/auth.py",)

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


class TestBudgetStatus:
    def test_ok_below_thresholds(self) -> None:
        b = new_budget("r", char_ceiling=1000)
        assert budget_status(b) == "OK"

    def test_soft_warn_at_60_pct(self) -> None:
        b = new_budget("r", char_ceiling=100)
        b = update_budget(b, message_text="x" * 60)
        assert budget_status(b) == "SOFT_WARN"

    def test_rotate_at_80_pct_chars(self) -> None:
        b = new_budget("r", char_ceiling=100)
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
        b = new_budget("r", char_ceiling=100, fix_ceiling=1)
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
        assert out_of_batch(None, {"src/auth.py"}) is False

    def test_leading_dot_slash_normalized(self) -> None:
        anchor = parse_scope_anchor("SCOPE: ./src/auth.py · reading")
        assert out_of_batch(anchor, {"src/auth.py"}) is False

    def test_frozenset_fast_path(self) -> None:
        # frozenset input is assumed pre-normalized — output must be
        # identical to the plain-set path.
        anchor = parse_scope_anchor("SCOPE: ./src/auth.py · reading")
        normalized = normalize_batch_files({"src/auth.py"})
        assert isinstance(normalized, frozenset)
        assert out_of_batch(anchor, normalized) is False

    def test_frozenset_detects_drift(self) -> None:
        anchor = parse_scope_anchor("SCOPE: src/crypto.py · reading")
        normalized = normalize_batch_files({"src/auth.py"})
        assert out_of_batch(anchor, normalized) is True


class TestNormalizeBatchFiles:
    def test_strips_dot_slash(self) -> None:
        assert normalize_batch_files({"./a.py", "b.py"}) == frozenset({"a.py", "b.py"})

    def test_idempotent(self) -> None:
        once = normalize_batch_files({"./a.py"})
        twice = normalize_batch_files(once)
        assert once == twice

    def test_converts_backslashes(self) -> None:
        assert normalize_batch_files({r"src\auth.py"}) == frozenset({"src/auth.py"})


class TestFormatBudgetNudge:
    def test_ok_returns_none_and_unchanged_budget(self) -> None:
        b = new_budget("r")
        msg, b2 = format_budget_nudge(b)
        assert msg is None
        assert b2 == b

    def test_soft_warn_returns_message_and_flag_flipped(self) -> None:
        b = new_budget("r", char_ceiling=100)
        b = update_budget(b, message_text="x" * 60)
        msg, b2 = format_budget_nudge(b)
        assert msg is not None
        assert "BUDGET SOFT" in msg
        assert "chars" in msg
        assert b2.soft_nudge_sent is True

    # ── Regression: PR#9 / Gemini-HIGH / Codex-P2 ──────────────────
    # format_budget_nudge must fire exactly once per threshold
    # crossing. Previously it re-emitted BUDGET SOFT every call while
    # the budget stayed in the SOFT_WARN region because the gate on
    # soft_nudge_sent was never wired in.

    def test_soft_warn_fires_only_once_when_budget_adopted(self) -> None:
        b = new_budget("r", char_ceiling=100)
        b = update_budget(b, message_text="x" * 60)
        msg1, b = format_budget_nudge(b)
        msg2, b = format_budget_nudge(b)
        msg3, b = format_budget_nudge(b)
        assert msg1 is not None
        assert msg2 is None
        assert msg3 is None

    def test_rotate_fires_only_once_when_budget_adopted(self) -> None:
        b = new_budget("r", char_ceiling=100)
        b = update_budget(b, message_text="x" * 85)
        msg1, b = format_budget_nudge(b)
        msg2, b = format_budget_nudge(b)
        assert msg1 is not None
        assert "BUDGET ROTATE" in msg1
        assert msg2 is None

    def test_nudge_gate_re_fires_if_budget_not_adopted(self) -> None:
        # Adoption is the commit; if a caller drops the returned
        # budget, the gate should re-fire. Documents the opt-out.
        b = new_budget("r", char_ceiling=100)
        b = update_budget(b, message_text="x" * 60)
        msg1, _discarded = format_budget_nudge(b)
        msg2, _discarded = format_budget_nudge(b)
        assert msg1 is not None
        assert msg2 is not None

    def test_soft_then_rotate_progression(self) -> None:
        # Hit SOFT first, consume its nudge, then escalate to ROTATE
        # and confirm a fresh nudge fires once and only once.
        b = new_budget("r", char_ceiling=100)
        b = update_budget(b, message_text="x" * 60)
        soft_msg, b = format_budget_nudge(b)
        assert soft_msg is not None
        # Now escalate past 80%.
        b = update_budget(b, message_text="y" * 30)
        rotate_msg, b = format_budget_nudge(b)
        assert rotate_msg is not None
        assert "BUDGET ROTATE" in rotate_msg
        no_more, b = format_budget_nudge(b)
        assert no_more is None

    def test_rotate_message_has_handoff_cue(self) -> None:
        b = new_budget("r", char_ceiling=100)
        b = update_budget(b, message_text="x" * 85)
        msg, _ = format_budget_nudge(b)
        assert msg is not None
        assert "handoff" in msg.lower()
        assert "chars" in msg


class TestNewBudgetValidation:
    @pytest.mark.parametrize("kwarg", ["char_ceiling", "file_read_ceiling", "fix_ceiling"])
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
        # Threshold numbers should appear as concrete values from the config.
        assert str(cfg.runner_output_char_ceiling) in text
        assert str(cfg.runner_file_read_ceiling) in text
        # Ensure the renamed "char" terminology is in the prompt.
        assert "char" in text.lower()

    def test_runner_prompt_includes_scope_anchor_clause(self) -> None:
        from advisor.orchestrate import build_runner_pool_prompt, default_team_config

        cfg = default_team_config(target_dir=".", warn_unknown_model=False)
        text = build_runner_pool_prompt(1, cfg)
        assert "SCOPE:" in text
        assert "BUDGET SOFT" in text
        assert "BUDGET ROTATE" in text
