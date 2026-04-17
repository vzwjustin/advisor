"""Tests for advisor.orchestrate module."""

import pytest

from advisor.orchestrate import (
    build_advisor_prompt,
    default_team_config,
    build_explore_agent,
    build_rank_agent,
    build_runner_agents,
    build_runner_batch_message,
    build_runner_dispatch_messages,
    build_runner_handoff_message,
    build_runner_pool_agents,
    build_runner_pool_prompt,
    build_verify_message,
    render_pipeline,
    build_runner_prompt,
)
from advisor.focus import FocusBatch, FocusTask


class TestDefaultTeamConfig:
    def test_creates_config_with_defaults(self):
        config = default_team_config("/src")

        assert config.target_dir == "/src"
        assert config.team_name == "review"
        assert config.max_runners == 5
        assert config.min_priority == 3

    def test_custom_values(self):
        config = default_team_config(
            "/app",
            team_name="myteam",
            max_runners=10,
            min_priority=2,
            context="security audit",
        )

        assert config.team_name == "myteam"
        assert config.max_runners == 10
        assert config.context == "security audit"

    def test_immutability(self):
        config = default_team_config("/src")
        try:
            config.team_name = "other"  # type: ignore
            assert False, "Should have raised"
        except AttributeError:
            pass


class TestBuildExploreAgent:
    def test_returns_agent_spec(self):
        config = default_team_config("/src")
        spec = build_explore_agent(config)

        assert spec["name"] == "explorer"
        assert spec["model"] == "sonnet"
        assert spec["subagent_type"] == "Explore"
        assert spec["team_name"] == "review"
        assert "/src" in spec["prompt"]

    def test_custom_file_types(self):
        config = default_team_config("/src", file_types="*.{py,js}")
        spec = build_explore_agent(config)

        assert "*.{py,js}" in spec["prompt"]


class TestBuildRankAgent:
    def test_returns_agent_spec(self):
        config = default_team_config("/src")
        spec = build_rank_agent("file1.py — auth\nfile2.py — utils", config)

        assert spec["name"] == "advisor"
        assert spec["model"] == "opus"
        assert spec["subagent_type"] == "deep-reasoning"
        assert "file1.py" in spec["prompt"]

    def test_includes_context(self):
        config = default_team_config("/src", context="security audit")
        spec = build_rank_agent("files...", config)

        assert "security audit" in spec["prompt"]

    def test_has_midflight_monitor_section(self):
        config = default_team_config("/src")
        spec = build_rank_agent("files...", config)

        assert "While runners work" in spec["prompt"]
        assert "do not go idle" in spec["prompt"].lower()
        assert "CONFIRM" in spec["prompt"]
        assert "NARROW" in spec["prompt"]
        assert "REDIRECT" in spec["prompt"]


class TestBuildRunnerAgents:
    def test_creates_one_per_task(self):
        config = default_team_config("/src")
        tasks = [
            FocusTask("src/auth.py", 5, "review auth"),
            FocusTask("src/api.py", 3, "review api"),
        ]
        agents = build_runner_agents(tasks, config)

        assert len(agents) == 2
        assert agents[0]["name"] == "runner-1"
        assert agents[1]["name"] == "runner-2"

    def test_all_use_sonnet(self):
        config = default_team_config("/src")
        tasks = [FocusTask("src/a.py", 5, "review")]
        agents = build_runner_agents(tasks, config)

        assert all(a["model"] == "sonnet" for a in agents)

    def test_all_run_in_background(self):
        config = default_team_config("/src")
        tasks = [FocusTask("src/a.py", 5, "review")]
        agents = build_runner_agents(tasks, config)

        assert all(a["run_in_background"] is True for a in agents)

    def test_includes_advisor_guidance(self):
        config = default_team_config("/src")
        tasks = [FocusTask("src/auth.py", 5, "review")]
        guidance = {"src/auth.py": "Check for hardcoded tokens"}
        agents = build_runner_agents(tasks, config, guidance=guidance)

        assert "hardcoded tokens" in agents[0]["prompt"].lower()


class TestBuildRunnerPrompt:
    def test_contains_file_path(self):
        task = FocusTask("src/auth.py", 5, "review src/auth.py")
        prompt = build_runner_prompt(task)

        assert "src/auth.py" in prompt
        assert "Do NOT review other files" in prompt

    def test_includes_guidance_when_provided(self):
        task = FocusTask("src/auth.py", 5, "review")
        prompt = build_runner_prompt(task, guidance={"src/auth.py": "Look for JWT issues"})

        assert "JWT issues" in prompt

    def test_has_midflight_advisor_checkpoint(self):
        task = FocusTask("src/auth.py", 5, "review")
        prompt = build_runner_prompt(task)

        assert "SendMessage(to='advisor')" in prompt
        assert "Checkpoint with the advisor" in prompt
        assert "CONFIRM" in prompt
        assert "NARROW" in prompt
        assert "REDIRECT" in prompt


class TestBuildVerifyMessage:
    def test_returns_sendmessage_spec(self):
        msg = build_verify_message("finding1\nfinding2", 3, 2)

        assert msg["to"] == "advisor"
        assert "2 analysis agents" in msg["message"] or "2 runners" in msg["message"]
        assert "CONFIRMED" in msg["message"]
        assert "REJECTED" in msg["message"]

    def test_includes_counts(self):
        msg = build_verify_message("stuff", 5, 3)

        assert "3" in msg["message"]
        assert "5" in msg["message"]


class TestRunnerPool:
    def test_pool_prompt_mentions_context_reuse(self):
        config = default_team_config("/src")
        prompt = build_runner_pool_prompt(1, config)

        assert "runner-1" in prompt
        assert "context" in prompt.lower()
        assert "wait" in prompt.lower()

    def test_pool_prompt_requires_heartbeat(self):
        config = default_team_config("/src")
        prompt = build_runner_pool_prompt(1, config)

        assert "5 min" in prompt or "5 minutes" in prompt.lower()
        assert "heartbeat" in prompt.lower()

    def test_pool_prompt_requires_pre_finding_verification(self):
        config = default_team_config("/src")
        prompt = build_runner_pool_prompt(1, config)

        assert "grep" in prompt.lower()
        lowered = prompt.lower()
        assert "missing" in lowered or "undefined" in lowered
        assert "pre-finding" in lowered or "unverified" in lowered

    def test_advisor_prompt_has_stall_pivot_clause(self):
        from advisor.orchestrate import build_advisor_prompt
        config = default_team_config("/src")
        prompt = build_advisor_prompt(config)

        lowered = prompt.lower()
        assert "stall" in lowered
        assert "pivot" in lowered
        assert "heartbeat" in lowered

    def test_advisor_prompt_folds_corrections_into_fixes(self):
        from advisor.orchestrate import build_advisor_prompt
        config = default_team_config("/src")
        prompt = build_advisor_prompt(config)

        lowered = prompt.lower()
        assert "correction" in lowered
        assert "regression test" in lowered

    def test_pool_agents_default_to_max_runners(self):
        config = default_team_config("/src", max_runners=4)
        agents = build_runner_pool_agents(config)

        assert len(agents) == 4
        assert [a["name"] for a in agents] == [
            "runner-1", "runner-2", "runner-3", "runner-4",
        ]
        assert all(a["run_in_background"] is True for a in agents)
        assert all(a["model"] == "sonnet" for a in agents)

    def test_pool_agents_explicit_size(self):
        config = default_team_config("/src", max_runners=10)
        agents = build_runner_pool_agents(config, pool_size=2)
        assert len(agents) == 2

    def test_batch_message_contains_files_and_guidance(self):
        batch = FocusBatch(
            batch_id=3,
            tasks=(FocusTask("src/auth.py", 5, "..."),),
            complexity="high",
        )
        msg = build_runner_batch_message(
            batch, guidance={"src/auth.py": "Check JWT handling"}
        )
        assert "batch 3" in msg
        assert "complexity: high" in msg
        assert "src/auth.py" in msg
        assert "JWT handling" in msg

    def test_dispatch_messages_route_by_batch_id(self):
        batches = [
            FocusBatch(
                batch_id=1,
                tasks=(FocusTask("a.py", 5, "..."),),
                complexity="low",
            ),
            FocusBatch(
                batch_id=2,
                tasks=(FocusTask("b.py", 3, "..."),),
                complexity="low",
            ),
        ]
        specs = build_runner_dispatch_messages(batches, pool_size=len(batches))

        assert [s["to"] for s in specs] == ["runner-1", "runner-2"]
        assert "a.py" in specs[0]["message"]
        assert "b.py" in specs[1]["message"]

    def test_dispatch_messages_raises_when_batch_id_exceeds_pool_size(self):
        batches = [
            FocusBatch(
                batch_id=3,
                tasks=(FocusTask("c.py", 2, "..."),),
                complexity="low",
            ),
        ]
        with pytest.raises(ValueError, match="batch_id 3 exceeds pool_size 2"):
            build_runner_dispatch_messages(batches, pool_size=2)

    def test_dispatch_messages_rejects_nonpositive_batch_id(self):
        batches = [
            FocusBatch(
                batch_id=0,
                tasks=(FocusTask("a.py", 3, "..."),),
                complexity="low",
            ),
        ]
        with pytest.raises(ValueError, match=r"batch_id must be >= 1"):
            build_runner_dispatch_messages(batches, pool_size=2)

    def test_dispatch_messages_rejects_duplicate_batch_ids(self):
        batches = [
            FocusBatch(
                batch_id=1,
                tasks=(FocusTask("a.py", 3, "..."),),
                complexity="low",
            ),
            FocusBatch(
                batch_id=1,
                tasks=(FocusTask("b.py", 3, "..."),),
                complexity="low",
            ),
        ]
        with pytest.raises(ValueError, match="duplicate batch_id"):
            build_runner_dispatch_messages(batches, pool_size=2)


class TestRenderPipeline:
    def test_renders_all_steps(self):
        config = default_team_config("/src")
        output = render_pipeline(config)

        assert "Step 1" in output
        assert "Step 2" in output
        assert "Step 3" in output
        assert "Step 4" in output
        assert "review" in output
        assert "opus" in output
        assert "sonnet" in output
        assert "haiku" not in output


class TestMaxFixesPerRunner:
    def test_default_is_five(self):
        config = default_team_config("/src")
        assert config.max_fixes_per_runner == 5

    def test_custom_value(self):
        config = default_team_config("/src", max_fixes_per_runner=8)
        assert config.max_fixes_per_runner == 8

    def test_advisor_prompt_names_cap(self):
        config = default_team_config("/src", max_fixes_per_runner=7)
        prompt = build_advisor_prompt(config)
        assert "HARD CAP: 7 fixes" in prompt
        assert "CONTEXT_PRESSURE" in prompt

    def test_runner_prompt_names_cap(self):
        config = default_team_config("/src", max_fixes_per_runner=3)
        prompt = build_runner_pool_prompt(1, config)
        assert "Hard cap: 3 fix assignments" in prompt
        assert "CONTEXT_PRESSURE" in prompt


class TestBuildRunnerHandoffMessage:
    def test_returns_sendmessage_spec_for_new_runner(self):
        msg = build_runner_handoff_message(
            new_runner_id=2,
            outgoing_runner_id=1,
            files_touched=["a.py", "b.py"],
            invariants=["parser must reject X"],
            remaining_fixes=["fix 6 — c.py", "fix 7 — d.py"],
        )
        assert msg["to"] == "runner-2"
        body = msg["message"]
        assert "Handoff from runner-1" in body
        assert "runner-2" in body
        assert "a.py" in body
        assert "b.py" in body
        assert "parser must reject X" in body
        assert "fix 6 — c.py" in body
        assert "fix 7 — d.py" in body

    def test_empty_lists_render_placeholders(self):
        msg = build_runner_handoff_message(
            new_runner_id=3,
            outgoing_runner_id=2,
            files_touched=[],
            invariants=[],
            remaining_fixes=[],
        )
        body = msg["message"]
        assert "(none yet)" in body
        assert "(none)" in body
        assert "verify pass" in body

    def test_extra_context_included_when_provided(self):
        msg = build_runner_handoff_message(
            new_runner_id=2,
            outgoing_runner_id=1,
            files_touched=["x.py"],
            invariants=["y"],
            remaining_fixes=["z"],
            extra_context="Watch for circular import in module q.",
        )
        assert "Extra context" in msg["message"]
        assert "circular import" in msg["message"]

    def test_no_triple_newline_before_acknowledge_with_extra_context(self):
        """Regression: earlier form produced '\\n\\n\\n' before 'Acknowledge'."""
        msg = build_runner_handoff_message(
            new_runner_id=2,
            outgoing_runner_id=1,
            files_touched=["x.py"],
            invariants=["y"],
            remaining_fixes=["z"],
            extra_context="some extra context",
        )
        body = msg["message"]
        assert "\n\n\nAcknowledge" not in body
        assert "\n\nAcknowledge and wait for the first fix assignment." in body

    def test_extra_context_omitted_when_empty(self):
        msg = build_runner_handoff_message(
            new_runner_id=2,
            outgoing_runner_id=1,
            files_touched=["x.py"],
            invariants=["y"],
            remaining_fixes=["z"],
            extra_context="   ",
        )
        assert "Extra context" not in msg["message"]
