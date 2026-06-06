"""Tests for the three-tier agent architecture (Opus → Haiku → Sonnet).

Covers unit tests and property-based tests for requirements 1–11 and
correctness properties 1–17 from the tiered-agent-architecture design.
"""

from __future__ import annotations

import inspect
import os
import tempfile
from unittest.mock import patch

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings
from hypothesis import strategies as st

from advisor.cost import CostEstimate, estimate_cost, format_estimate
from advisor.focus import FocusTask
from advisor.orchestrate import (
    build_advisor_prompt,
    build_coder_prompt,
    build_explorer_pool_agents,
    build_explorer_prompt,
    build_runner_pool_prompt,
    default_team_config,
    render_pipeline,
)
from advisor.orchestrate._fence import sanitize_inline
from advisor.orchestrate.config import (
    DEFAULT_EXPLORER_FILE_READ_CEILING,
    DEFAULT_EXPLORER_MODEL,
    DEFAULT_EXPLORER_OUTPUT_CHAR_CEILING,
    POOL_SIZE_CEILING,
)
from advisor.orchestrate.runner_prompts import build_fix_assignment_message as _fix_msg
from advisor.presets import PRESETS, RulePack

_FUZZ = settings(max_examples=80, deadline=None, derandomize=True)

_FIX_KWARGS = dict(
    runner_id=1,
    file_path="src/a.py",
    problem="null deref",
    change="add guard",
    acceptance="tests pass",
    fix_number=1,
    max_fixes=5,
    large_file_max_fixes=3,
)


def _task(path: str = "/tmp/f.py") -> FocusTask:
    return FocusTask(file_path=path, priority=3, prompt="")


def _temp_py_file(content: str = "x" * 500) -> str:
    fd, path = tempfile.mkstemp(suffix=".py")
    os.write(fd, content.encode())
    os.close(fd)
    return path


def _push_env(updates: dict[str, str]) -> dict[str, str | None]:
    saved = {k: os.environ.get(k) for k in updates}
    os.environ.update(updates)
    return saved


def _pop_env(saved: dict[str, str | None]) -> None:
    for key, prior in saved.items():
        if prior is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = prior


# ── Unit tests: TeamConfig explorer fields (Task 1.3) ─────────────────


class TestTeamConfigExplorerFields:
    def test_default_explorer_fields(self) -> None:
        cfg = default_team_config("/src", warn_unknown_model=False)
        assert cfg.explorer_model == DEFAULT_EXPLORER_MODEL
        assert cfg.max_explorers == cfg.max_runners
        assert cfg.explorer_output_char_ceiling == DEFAULT_EXPLORER_OUTPUT_CHAR_CEILING
        assert cfg.explorer_file_read_ceiling == DEFAULT_EXPLORER_FILE_READ_CEILING

    def test_max_explorers_zero_legacy_mode(self) -> None:
        cfg = default_team_config("/src", max_explorers=0, warn_unknown_model=False)
        assert cfg.max_explorers == 0

    def test_env_var_explorer_model(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADVISOR_EXPLORER_MODEL", "haiku")
        cfg = default_team_config("/src", warn_unknown_model=False)
        assert cfg.explorer_model == "haiku"

    def test_env_var_max_explorers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADVISOR_MAX_EXPLORERS", "3")
        cfg = default_team_config("/src", warn_unknown_model=False)
        assert cfg.max_explorers == 3

    def test_env_var_explorer_output_char_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADVISOR_EXPLORER_OUTPUT_CHAR_CEILING", "50000")
        cfg = default_team_config("/src", warn_unknown_model=False)
        assert cfg.explorer_output_char_ceiling == 50_000

    def test_env_var_explorer_file_read_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ADVISOR_EXPLORER_FILE_READ_CEILING", "30")
        cfg = default_team_config("/src", warn_unknown_model=False)
        assert cfg.explorer_file_read_ceiling == 30

    def test_env_var_invalid_max_explorers_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("ADVISOR_MAX_EXPLORERS", "not-int")
        cfg = default_team_config("/src", max_runners=4, warn_unknown_model=False)
        assert cfg.max_explorers == 4
        assert "not an integer" in capsys.readouterr().err

    def test_env_var_invalid_explorer_char_ceiling_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("ADVISOR_EXPLORER_OUTPUT_CHAR_CEILING", "bad")
        cfg = default_team_config("/src", warn_unknown_model=False)
        assert cfg.explorer_output_char_ceiling == DEFAULT_EXPLORER_OUTPUT_CHAR_CEILING
        assert "not an integer" in capsys.readouterr().err

    def test_max_explorers_clamped_to_ceiling(self, capsys: pytest.CaptureFixture[str]) -> None:
        cfg = default_team_config(
            "/src", max_explorers=POOL_SIZE_CEILING + 5, warn_unknown_model=False
        )
        assert cfg.max_explorers == POOL_SIZE_CEILING
        assert "exceeds ceiling" in capsys.readouterr().err

    def test_negative_max_explorers_clamped_to_zero(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg = default_team_config("/src", max_explorers=-2, warn_unknown_model=False)
        assert cfg.max_explorers == 0
        assert "< 0" in capsys.readouterr().err

    def test_explorer_budget_floored_at_one(self) -> None:
        cfg = default_team_config(
            "/src",
            explorer_output_char_ceiling=0,
            explorer_file_read_ceiling=-5,
            warn_unknown_model=False,
        )
        assert cfg.explorer_output_char_ceiling == 1
        assert cfg.explorer_file_read_ceiling == 1

    def test_warn_unknown_explorer_model(self, capsys: pytest.CaptureFixture[str]) -> None:
        default_team_config("/src", explorer_model="totally-unknown-model-xyz")
        err = capsys.readouterr().err
        assert "explorer_model" in err

    def test_explicit_max_explorers_wins_over_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ADVISOR_MAX_EXPLORERS", "9")
        cfg = default_team_config("/src", max_explorers=2, warn_unknown_model=False)
        assert cfg.max_explorers == 2


# ── Unit tests: presets (Task 2.3) ───────────────────────────────────


class TestPresetExplorerModel:
    def test_none_leaves_default(self) -> None:
        cfg = default_team_config("/src", preset="general-python", warn_unknown_model=False)
        assert cfg.explorer_model == DEFAULT_EXPLORER_MODEL

    def test_explicit_preset_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from advisor import presets

        monkeypatch.setitem(
            presets.PRESETS,
            "test-explorer",
            RulePack(
                name="test-explorer",
                description="test",
                file_types="*.py",
                min_priority=3,
                extra_keywords_by_tier={},
                test_command=None,
                notes=(),
                explorer_model="claude-haiku-4-5-20251001",
            ),
        )
        cfg = default_team_config("/src", preset="test-explorer", warn_unknown_model=False)
        assert cfg.explorer_model == "claude-haiku-4-5-20251001"

    def test_existing_presets_backward_compatible(self) -> None:
        for name in PRESETS:
            cfg = default_team_config("/src", preset=name, warn_unknown_model=False)
            assert cfg.preset == name
            pack_model = PRESETS[name].explorer_model
            assert cfg.explorer_model == (pack_model or DEFAULT_EXPLORER_MODEL)


# ── Unit tests: explorer prompts (Task 4.4) ──────────────────────────


class TestExplorerPrompts:
    def test_read_only_invariants(self) -> None:
        cfg = default_team_config("/src", warn_unknown_model=False)
        prompt = build_explorer_prompt(cfg, ["src/a.py"], {"src/a.py": "check auth"})
        assert "Read, Glob, Grep" in prompt
        assert "MUST NOT" in prompt and "Write" in prompt
        assert "SCOPE:" in prompt
        assert "SendMessage" in prompt and "team-lead" in prompt
        assert "Exploration_Report" in prompt
        assert "unreadable" in prompt.lower() or "permission" in prompt.lower()
        assert "check auth" in prompt

    def test_agent_spec_structure(self) -> None:
        cfg = default_team_config("/src", max_explorers=2, warn_unknown_model=False)
        agents = build_explorer_pool_agents(cfg)
        assert len(agents) == 2
        assert agents[0]["name"] == "explorer-1"
        assert agents[1]["name"] == "explorer-2"
        assert agents[0]["subagent_type"] == "explorer"
        assert agents[0]["model"] == cfg.explorer_model
        assert agents[0]["run_in_background"] is True
        assert agents[0]["team_name"] == cfg.team_name

    def test_zero_explorers_empty_pool(self) -> None:
        cfg = default_team_config("/src", max_explorers=0, warn_unknown_model=False)
        assert build_explorer_pool_agents(cfg) == []

    def test_pool_respects_max_explorers_ceiling(self) -> None:
        cfg = default_team_config("/src", max_explorers=2, warn_unknown_model=False)
        agents = build_explorer_pool_agents(cfg, pool_size=99)
        assert len(agents) == 2


# ── Unit tests: coder prompts (Task 5.4) ─────────────────────────────


class TestCoderPrompts:
    def test_coder_emphasizes_modification(self) -> None:
        cfg = default_team_config("/src", warn_unknown_model=False)
        prompt = build_coder_prompt(1, cfg)
        assert "code modification" in prompt.lower() or "implement fixes" in prompt.lower()
        assert "embedded exploration context" in prompt.lower()
        assert "## Explore assignment" not in prompt

    def test_runner_pool_prompt_alias(self) -> None:
        cfg = default_team_config("/src", warn_unknown_model=False)
        assert build_runner_pool_prompt(1, cfg) == build_coder_prompt(1, cfg)

    def test_fix_assignment_none_matches_legacy(self) -> None:
        legacy = _fix_msg(**_FIX_KWARGS)
        with_none = _fix_msg(**_FIX_KWARGS, exploration_context=None)
        assert legacy == with_none

    def test_fix_assignment_empty_string_matches_legacy(self) -> None:
        legacy = _fix_msg(**_FIX_KWARGS)
        with_empty = _fix_msg(**_FIX_KWARGS, exploration_context="   ")
        assert legacy == with_empty

    def test_fix_assignment_embeds_context_before_problem(self) -> None:
        with_ctx = _fix_msg(**_FIX_KWARGS, exploration_context="lines 10-20: risky parse")
        assert "## Exploration context" in with_ctx["message"]
        assert "risky parse" in with_ctx["message"]
        assert with_ctx["message"].index("Exploration context") < with_ctx["message"].index(
            "Problem:"
        )


# ── Unit tests: advisor prompt (Task 7.4) ────────────────────────────


class TestAdvisorThreeTier:
    def test_three_tier_description(self) -> None:
        cfg = default_team_config("/src", max_explorers=3, warn_unknown_model=False)
        prompt = build_advisor_prompt(cfg)
        assert "Haiku" in prompt or "explorers" in prompt
        assert "Sonnet" in prompt or "coders" in prompt

    def test_explorer_dispatch_step(self) -> None:
        cfg = default_team_config("/src", max_explorers=3, warn_unknown_model=False)
        prompt = build_advisor_prompt(cfg)
        assert "build_explorer_prompt" in prompt
        assert "explore wave" in prompt.lower() or "Dispatch explore" in prompt

    def test_exploration_report_synthesis(self) -> None:
        cfg = default_team_config("/src", max_explorers=3, warn_unknown_model=False)
        prompt = build_advisor_prompt(cfg)
        assert "Exploration_Report" in prompt
        assert "exploration_context" in prompt

    def test_retains_structural_discovery(self) -> None:
        cfg = default_team_config("/src", max_explorers=3, warn_unknown_model=False)
        prompt = build_advisor_prompt(cfg)
        assert "Glob" in prompt and "Grep" in prompt
        assert "Step 1" in prompt

    def test_legacy_mode_content(self) -> None:
        cfg = default_team_config("/src", max_explorers=0, warn_unknown_model=False)
        prompt = build_advisor_prompt(cfg)
        assert "Legacy two-tier" in prompt or "max_explorers=0" in prompt
        assert "build_explorer_prompt" not in prompt

    def test_three_tier_budget_placeholders(self) -> None:
        cfg = default_team_config(
            "/src",
            max_explorers=4,
            explorer_output_char_ceiling=35_000,
            explorer_file_read_ceiling=25,
            warn_unknown_model=False,
        )
        prompt = build_advisor_prompt(cfg)
        assert "35000" in prompt
        assert "25" in prompt
        assert cfg.explorer_model in prompt


# ── Unit tests: cost (Task 8.5) ──────────────────────────────────────


class TestCostThreeTier:
    def test_explorer_costs_when_active(self, tmp_path) -> None:
        p = tmp_path / "a.py"
        p.write_text("x" * 2000)
        est = estimate_cost(
            [_task(str(p))],
            None,
            advisor_model="claude-opus-4-7",
            runner_model="claude-sonnet-4-6",
            explorer_model="claude-haiku-4-5",
            max_explorers=2,
            max_fixes_per_runner=5,
        )
        assert est.explorer_cost_usd_max > 0
        assert est.explorer_model == "claude-haiku-4-5"

    def test_zero_explorer_fields_when_disabled(self, tmp_path) -> None:
        p = tmp_path / "a.py"
        p.write_text("x" * 2000)
        est = estimate_cost(
            [_task(str(p))],
            None,
            advisor_model="opus",
            runner_model="sonnet",
            max_explorers=0,
            max_fixes_per_runner=5,
        )
        assert est.explorer_cost_usd_max == 0.0
        assert est.explorer_input_tokens_max == 0
        assert est.explorer_output_tokens_max == 0

    def test_format_includes_tier_lines(self, tmp_path) -> None:
        p = tmp_path / "a.py"
        p.write_text("x" * 500)
        est = estimate_cost(
            [_task(str(p))],
            None,
            advisor_model="opus",
            runner_model="sonnet",
            explorer_model="haiku",
            max_explorers=1,
            max_fixes_per_runner=3,
        )
        text = format_estimate(est)
        assert "Advisor (Opus)" in text
        assert "Explorers (Haiku)" in text
        assert "Coders (Sonnet)" in text

    def test_format_omits_explorer_when_zero(self, tmp_path) -> None:
        p = tmp_path / "a.py"
        p.write_text("x" * 500)
        est = estimate_cost(
            [_task(str(p))],
            None,
            advisor_model="opus",
            runner_model="sonnet",
            max_explorers=0,
            max_fixes_per_runner=3,
        )
        text = format_estimate(est)
        assert "Explorers (Haiku)" not in text

    def test_to_dict_all_explorer_keys(self, tmp_path) -> None:
        p = tmp_path / "a.py"
        p.write_text("x" * 500)
        est = estimate_cost(
            [_task(str(p))],
            None,
            advisor_model="opus",
            runner_model="sonnet",
            explorer_model="haiku",
            max_explorers=1,
            max_fixes_per_runner=3,
        )
        d = est.to_dict()
        for key in (
            "explorer_model",
            "explorer_input_tokens_min",
            "explorer_input_tokens_max",
            "explorer_output_tokens_min",
            "explorer_output_tokens_max",
            "explorer_cost_usd_min",
            "explorer_cost_usd_max",
            "advisor_cost_usd_min",
            "coder_cost_usd_min",
        ):
            assert key in d


# ── Unit tests: pipeline (Task 9.3) ──────────────────────────────────


class TestPipelineRendering:
    def test_three_tier_pipeline(self) -> None:
        cfg = default_team_config("/src", max_explorers=2, warn_unknown_model=False)
        out = render_pipeline(cfg)
        assert cfg.explorer_model in out
        assert "explorer-N" in out
        assert "Explorer discovers" in out

    def test_shows_max_explorers_count(self) -> None:
        cfg = default_team_config("/src", max_explorers=7, warn_unknown_model=False)
        out = render_pipeline(cfg)
        assert "~7" in out or "7" in out

    def test_legacy_pipeline_omits_explorer(self) -> None:
        cfg = default_team_config("/src", max_explorers=0, warn_unknown_model=False)
        out = render_pipeline(cfg)
        assert "explorer-N" not in out
        assert "Legacy two-tier" in out


# ── Unit tests: backward compatibility (Task 11.3) ───────────────────


class TestBackwardCompatibility:
    def test_default_enables_three_tier(self) -> None:
        cfg = default_team_config("/src", warn_unknown_model=False)
        assert cfg.max_explorers == cfg.max_runners
        assert cfg.explorer_model == DEFAULT_EXPLORER_MODEL
        assert cfg.max_explorers > 0

    def test_legacy_two_tier_via_zero_explorers(self) -> None:
        cfg = default_team_config("/src", max_explorers=0, warn_unknown_model=False)
        assert cfg.max_explorers == 0
        assert "Legacy two-tier" in render_pipeline(cfg)

    def test_cli_flags_do_not_affect_explorer_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ADVISOR_MODEL", "sonnet")
        monkeypatch.setenv("ADVISOR_RUNNER_MODEL", "opus")
        cfg = default_team_config(
            "/src",
            advisor_model="haiku",
            runner_model="opus",
            warn_unknown_model=False,
        )
        assert cfg.explorer_model == DEFAULT_EXPLORER_MODEL

    def test_no_new_mandatory_default_team_config_args(self) -> None:
        sig = inspect.signature(default_team_config)
        assert "target_dir" in sig.parameters
        # Only target_dir has no default — all explorer fields are optional.
        required = [
            name
            for name, p in sig.parameters.items()
            if p.default is inspect.Parameter.empty and name != "target_dir"
        ]
        assert required == []


# ── Property tests (Tasks 1.2, 2.2, 4.3, 5.3, 7.3, 8.4, 9.2, 11.2) ──


@_FUZZ
@given(
    st.sampled_from(["haiku", "claude-haiku-4-5", "claude-haiku-4-5-20251001"]),
    st.integers(min_value=0, max_value=POOL_SIZE_CEILING + 5),
    st.integers(min_value=1, max_value=100_000),
    st.integers(min_value=1, max_value=100),
)
def test_property_1_explorer_env_propagation(
    model: str,
    max_exp: int,
    char_ceil: int,
    read_ceil: int,
) -> None:
    """Property 1: Explorer environment variable propagation."""
    saved = _push_env(
        {
            "ADVISOR_EXPLORER_MODEL": model,
            "ADVISOR_MAX_EXPLORERS": str(max_exp),
            "ADVISOR_EXPLORER_OUTPUT_CHAR_CEILING": str(char_ceil),
            "ADVISOR_EXPLORER_FILE_READ_CEILING": str(read_ceil),
        }
    )
    try:
        cfg = default_team_config("/src", warn_unknown_model=False)
    finally:
        _pop_env(saved)
    assert cfg.explorer_model == model
    expected_max = min(max(0, max_exp), POOL_SIZE_CEILING)
    assert cfg.max_explorers == expected_max
    assert cfg.explorer_output_char_ceiling == char_ceil
    assert cfg.explorer_file_read_ceiling == read_ceil


_INVALID_INT = st.sampled_from(["abc", "1.5", "1e100", "Inf", "not-int", "12.3", ""])


@_FUZZ
@given(_INVALID_INT, _INVALID_INT)
def test_property_2_explorer_env_invalid_fallback(
    bad_max: str,
    bad_ceil: str,
) -> None:
    """Property 2: Explorer environment variable fallback on invalid input."""
    saved = _push_env(
        {
            "ADVISOR_MAX_EXPLORERS": bad_max,
            "ADVISOR_EXPLORER_OUTPUT_CHAR_CEILING": bad_ceil,
        }
    )
    os.environ.pop("ADVISOR_MAX_RUNNERS", None)
    try:
        cfg = default_team_config("/src", max_runners=6, warn_unknown_model=False)
    finally:
        _pop_env(saved)
    assert cfg.max_explorers == 6
    assert cfg.explorer_output_char_ceiling == DEFAULT_EXPLORER_OUTPUT_CHAR_CEILING


@_FUZZ
@given(st.integers(min_value=-50, max_value=POOL_SIZE_CEILING + 50))
def test_property_3_explorer_config_clamping(raw: int) -> None:
    """Property 3: Explorer config field clamping invariant."""
    cfg = default_team_config("/src", max_explorers=raw, warn_unknown_model=False)
    assert 0 <= cfg.max_explorers <= POOL_SIZE_CEILING


@_FUZZ
@given(st.integers(min_value=1, max_value=POOL_SIZE_CEILING + 10))
def test_property_4_default_max_explorers_mirrors_runners(max_runners: int) -> None:
    """Property 4: Default max_explorers mirrors max_runners."""
    cfg = default_team_config("/src", max_runners=max_runners, warn_unknown_model=False)
    expected = min(max(1, max_runners), POOL_SIZE_CEILING)
    assert cfg.max_runners == expected
    assert cfg.max_explorers == expected


def test_property_5_unknown_explorer_model_warning(capsys: pytest.CaptureFixture[str]) -> None:
    """Property 5: Unknown explorer model warning."""
    default_team_config("/src", explorer_model="not-a-real-model-id-xyz")
    assert "explorer_model" in capsys.readouterr().err.lower()


@_FUZZ
@given(st.integers(min_value=1, max_value=POOL_SIZE_CEILING))
def test_property_6_explorer_agent_spec_structure(pool_size: int) -> None:
    """Property 6: Explorer agent spec structure."""
    cfg = default_team_config(
        "/src", max_explorers=pool_size, warn_unknown_model=False
    )
    agents = build_explorer_pool_agents(cfg, pool_size=pool_size)
    assert len(agents) == pool_size
    for i, spec in enumerate(agents, start=1):
        assert spec["model"] == cfg.explorer_model
        assert spec["run_in_background"] is True
        assert spec["team_name"] == cfg.team_name
        assert spec["subagent_type"] == "explorer"
        assert spec["name"] == f"explorer-{i}"


@_FUZZ
@given(
    st.lists(st.text(min_size=1, max_size=40), min_size=1, max_size=5, unique=True),
    st.dictionaries(st.text(min_size=1, max_size=20), st.text(min_size=1, max_size=60)),
)
def test_property_7_explorer_prompt_read_only_invariants(
    paths: list[str],
    guidance: dict[str, str],
) -> None:
    """Property 7: Explorer prompt read-only invariants."""
    cfg = default_team_config("/src", warn_unknown_model=False)
    prompt = build_explorer_prompt(cfg, paths, guidance)
    assert prompt
    lowered = prompt.lower()
    assert "read" in lowered and "glob" in lowered and "grep" in lowered
    assert "must not" in lowered
    assert "write" in lowered or "edit" in lowered
    assert "scope:" in lowered
    assert "team-lead" in lowered


@_FUZZ
@given(st.text(min_size=1, max_size=80).filter(lambda s: s.strip()))
def test_property_8_explorer_guidance_embedding(guidance: str) -> None:
    """Property 8: Explorer prompt guidance embedding."""
    path = "src/module.py"
    cfg = default_team_config("/src", warn_unknown_model=False)
    prompt = build_explorer_prompt(cfg, [path], {path: guidance})
    embedded = sanitize_inline(guidance.strip())
    assert embedded in prompt


@_FUZZ
@given(st.integers(min_value=1, max_value=20))
def test_property_9_coder_focuses_on_modification(runner_id: int) -> None:
    """Property 9: Coder prompt focuses on code modification."""
    cfg = default_team_config("/src", warn_unknown_model=False)
    prompt = build_coder_prompt(runner_id, cfg).lower()
    assert "fix" in prompt or "modif" in prompt
    assert "embedded exploration context" in prompt
    assert "## explore assignment" not in prompt


@_FUZZ
@given(st.text(min_size=1, max_size=200))
def test_property_10_fix_assignment_context_embedding(ctx: str) -> None:
    """Property 10: Fix assignment exploration context embedding."""
    msg = _fix_msg(**_FIX_KWARGS, exploration_context=ctx)
    assert ctx.strip() in msg["message"]
    assert msg["message"].index("Exploration context") < msg["message"].index("Problem:")


@_FUZZ
@given(st.integers(min_value=1, max_value=POOL_SIZE_CEILING))
def test_property_11_legacy_mode_prompt_and_rendering(_: int) -> None:
    """Property 11: Legacy mode prompt and rendering (max_explorers=0)."""
    cfg = default_team_config("/src", max_explorers=0, warn_unknown_model=False)
    prompt = build_advisor_prompt(cfg)
    pipeline = render_pipeline(cfg)
    assert "build_explorer_prompt" not in prompt
    assert "explorer-N" not in pipeline
    assert "legacy" in prompt.lower() or "max_explorers=0" in prompt.lower()


@_FUZZ
@given(st.integers(min_value=1, max_value=POOL_SIZE_CEILING))
def test_property_12_three_tier_cost_estimation(max_exp: int) -> None:
    """Property 12: Three-tier cost estimation."""
    path = _temp_py_file("code " * 100)
    try:
        active = estimate_cost(
            [_task(path)],
            None,
            advisor_model="opus",
            runner_model="sonnet",
            explorer_model="haiku",
            max_explorers=max_exp,
            max_fixes_per_runner=3,
        )
        assert active.explorer_input_tokens_max > 0
        assert active.explorer_cost_usd_max > 0
        zero = estimate_cost(
            [_task(path)],
            None,
            advisor_model="opus",
            runner_model="sonnet",
            max_explorers=0,
            max_fixes_per_runner=3,
        )
        assert zero.explorer_input_tokens_max == 0
        assert zero.explorer_cost_usd_max == 0.0
    finally:
        os.unlink(path)


@_FUZZ
@given(st.booleans())
def test_property_13_cost_format_per_tier_breakdown(three_tier: bool) -> None:
    """Property 13: Cost estimate format per-tier breakdown."""
    path = _temp_py_file("x" * 300)
    try:
        est = estimate_cost(
            [_task(path)],
            None,
            advisor_model="opus",
            runner_model="sonnet",
            explorer_model="haiku",
            max_explorers=2 if three_tier else 0,
            max_fixes_per_runner=2,
        )
        text = format_estimate(est)
        assert "Advisor (Opus)" in text
        assert "Coders (Sonnet)" in text
        if three_tier:
            assert "Explorers (Haiku)" in text
        else:
            assert "Explorers (Haiku)" not in text
    finally:
        os.unlink(path)


@_FUZZ
@given(st.integers(min_value=0, max_value=POOL_SIZE_CEILING))
def test_property_14_cost_estimate_serialization(max_exp: int) -> None:
    """Property 14: CostEstimate serialization includes explorer fields."""
    path = _temp_py_file("y" * 200)
    try:
        est = estimate_cost(
            [_task(path)],
            None,
            advisor_model="opus",
            runner_model="sonnet",
            explorer_model="haiku",
            max_explorers=max_exp,
            max_fixes_per_runner=1,
        )
        d = est.to_dict()
        assert isinstance(d, dict)
        for key in (
            "explorer_model",
            "explorer_input_tokens_min",
            "explorer_output_tokens_max",
            "explorer_cost_usd_min",
            "explorer_cost_usd_max",
        ):
            assert key in d
        assert isinstance(est, CostEstimate)
    finally:
        os.unlink(path)


@_FUZZ
@given(st.integers(min_value=1, max_value=POOL_SIZE_CEILING))
def test_property_15_pipeline_includes_explorer_tier(max_exp: int) -> None:
    """Property 15: Pipeline rendering includes explorer tier."""
    cfg = default_team_config("/src", max_explorers=max_exp, warn_unknown_model=False)
    out = render_pipeline(cfg)
    assert cfg.explorer_model in out
    assert "explorer" in out.lower()
    assert str(max_exp) in out or f"~{max_exp}" in out


@_FUZZ
@given(
    st.sampled_from(["opus", "sonnet", "haiku", "claude-opus-4-7", "claude-sonnet-4-6"]),
    st.sampled_from(["opus", "sonnet", "haiku", "claude-sonnet-4-6"]),
)
def test_property_16_model_flag_isolation(adv: str, run: str) -> None:
    """Property 16: Model flag isolation."""
    saved = _push_env({"ADVISOR_MODEL": adv, "ADVISOR_RUNNER_MODEL": run})
    try:
        cfg = default_team_config("/src", warn_unknown_model=False)
    finally:
        _pop_env(saved)
    assert cfg.explorer_model == DEFAULT_EXPLORER_MODEL


@_FUZZ
@given(st.sampled_from(["haiku", "claude-haiku-4-5-20251001"]))
def test_property_17_preset_explorer_model_override(model: str) -> None:
    """Property 17: Preset explorer_model override."""
    preset_name = f"p-{abs(hash(model)) % 100_000}"
    pack = RulePack(
        name=preset_name,
        description="t",
        file_types="*.py",
        min_priority=3,
        extra_keywords_by_tier={},
        test_command=None,
        notes=(),
        explorer_model=model,
    )
    with patch.dict(PRESETS, {preset_name: pack}):
        cfg = default_team_config("/src", preset=preset_name, warn_unknown_model=False)
    assert cfg.explorer_model == model


@_FUZZ
@given(st.integers(min_value=-100, max_value=0))
def test_property_3_budget_floor(char_ceil: int) -> None:
    """Property 3 (budget): explorer ceiling fields floor at 1."""
    cfg = default_team_config(
        "/src",
        explorer_output_char_ceiling=char_ceil,
        explorer_file_read_ceiling=char_ceil,
        warn_unknown_model=False,
    )
    assert cfg.explorer_output_char_ceiling >= 1
    assert cfg.explorer_file_read_ceiling >= 1
