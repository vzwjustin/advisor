"""Tests for advisor.orchestrate module."""

from advisor.orchestrate import (
    default_team_config,
    build_explore_agent,
    build_rank_agent,
    build_runner_agents,
    build_verify_message,
    render_pipeline,
    build_runner_prompt,
)
from advisor.focus import FocusTask


class TestDefaultTeamConfig:
    def test_creates_config_with_defaults(self):
        config = default_team_config("/src")

        assert config.target_dir == "/src"
        assert config.team_name == "glasswing"
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
        assert spec["team_name"] == "glasswing"
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
        agents = build_runner_agents(tasks, config, advisor_guidance=guidance)

        assert "hardcoded tokens" in agents[0]["prompt"].lower()


class TestBuildRunnerPrompt:
    def test_contains_file_path(self):
        task = FocusTask("src/auth.py", 5, "review src/auth.py")
        prompt = build_runner_prompt(task)

        assert "src/auth.py" in prompt
        assert "Do NOT review other files" in prompt

    def test_includes_guidance_when_provided(self):
        task = FocusTask("src/auth.py", 5, "review")
        prompt = build_runner_prompt(task, "Look for JWT issues")

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


class TestRenderPipeline:
    def test_renders_all_steps(self):
        config = default_team_config("/src")
        output = render_pipeline(config)

        assert "Step 1" in output
        assert "Step 2" in output
        assert "Step 3" in output
        assert "Step 4" in output
        assert "glasswing" in output
        assert "opus" in output
        assert "sonnet" in output
        assert "haiku" not in output
