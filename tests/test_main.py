"""Tests for advisor.__main__ CLI."""

import io
import sys
from pathlib import Path

import pytest

from advisor.__main__ import _NUDGE_SKIP_COMMANDS, build_parser


class TestCmdPromptVerifyEmptyStdin:
    def test_warns_on_empty_stdin(self, monkeypatch, capsys):
        """Empty stdin for --step verify prints a warning to stderr."""
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        parser = build_parser()
        args = parser.parse_args(["prompt", "verify", "."])
        from advisor.__main__ import cmd_prompt

        cmd_prompt(args)
        captured = capsys.readouterr()
        assert "no findings on stdin" in captured.err

    def test_no_warning_when_findings_present(self, monkeypatch, capsys):
        """Non-empty stdin must not trigger the warning."""
        monkeypatch.setattr(sys, "stdin", io.StringIO("- **File**: src/a.py\n"))
        parser = build_parser()
        args = parser.parse_args(["prompt", "verify", "."])
        from advisor.__main__ import cmd_prompt

        cmd_prompt(args)
        captured = capsys.readouterr()
        assert "no findings on stdin" not in captured.err

    def test_empty_pipe_uses_same_placeholder_as_tty(self, monkeypatch, capsys):
        """Regression: piped-empty stdin renders the same placeholder
        template as the TTY path. Previously the two paths diverged — TTY
        got '<paste findings here>' while an empty pipe built a prompt
        around an empty findings string."""
        from advisor.__main__ import cmd_prompt

        class _TTYStdin:
            def isatty(self) -> bool:
                return True

            def read(self) -> str:  # pragma: no cover — defensive
                return ""

        monkeypatch.setattr(sys, "stdin", _TTYStdin())
        parser = build_parser()
        args = parser.parse_args(["prompt", "verify", "."])
        cmd_prompt(args)
        tty_out = capsys.readouterr().out

        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        cmd_prompt(args)
        pipe_out = capsys.readouterr().out

        assert "<paste findings here>" in tty_out
        assert "<paste findings here>" in pipe_out


class TestLoadFindingsFromInput:
    def test_json_rule_id_must_be_string(self, tmp_path):
        import json

        from advisor.__main__ import _load_findings_from_input

        p = tmp_path / "findings.json"
        p.write_text(
            json.dumps(
                {
                    "findings_in_batch": [
                        {
                            "file_path": "a.py",
                            "severity": "HIGH",
                            "description": "d",
                            "evidence": "e",
                            "fix": "f",
                            "rule_id": ["not", "a", "string"],
                        },
                        {
                            "file_path": "b.py",
                            "severity": "LOW",
                            "description": "d",
                            "rule_id": "  custom/rule  ",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        findings, rc = _load_findings_from_input(p)
        assert rc is None
        assert findings[0].rule_id is None
        assert findings[1].rule_id == "custom/rule"


class TestNudgeSkipCommands:
    """Only commands that explicitly manage the nudge should skip ensure_nudge.

    The first-run auto-install behavior (documented in the README) relies on
    every OTHER subcommand triggering ``ensure_nudge()`` — including dry-run
    / preview commands like ``plan`` and ``status``. ``ensure_nudge`` is
    idempotent, so firing it on each command is harmless after the first.
    """

    @pytest.mark.parametrize("cmd", ["install", "uninstall", "version"])
    def test_explicit_management_commands_skip_nudge(self, cmd):
        assert cmd in _NUDGE_SKIP_COMMANDS

    @pytest.mark.parametrize(
        "cmd",
        ["plan", "pipeline", "prompt", "status", "doctor", "protocol", "history", "checkpoints"],
    )
    def test_other_commands_trigger_nudge(self, cmd):
        """Regression: previously every subcommand was in the skip set,
        which silently disabled the entire auto-install feature."""
        assert cmd not in _NUDGE_SKIP_COMMANDS


class TestSafeRglob:
    """``_safe_rglob`` must survive bad patterns and bad filesystems."""

    def test_happy_path_returns_files(self, tmp_path):
        from advisor.__main__ import _safe_rglob

        (tmp_path / "a.py").write_text("x")
        (tmp_path / "b.py").write_text("y")
        paths, err = _safe_rglob(tmp_path, "*.py")
        assert err is None
        assert paths is not None
        assert sorted(Path(p).name for p in paths) == ["a.py", "b.py"]

    def test_reports_oserror_as_string(self, tmp_path, monkeypatch):
        from advisor.__main__ import _safe_rglob

        def _boom(self, pattern):
            raise OSError("symlink loop")

        monkeypatch.setattr(Path, "rglob", _boom)
        paths, err = _safe_rglob(tmp_path, "*.py")
        assert paths is None
        assert err is not None and "symlink loop" in err

    def test_reports_absolute_drive_pattern_as_invalid(self, tmp_path):
        from advisor.__main__ import _safe_rglob

        paths, err = _safe_rglob(tmp_path, "C:\\*.py")
        assert paths is None
        assert err is not None and "pattern" in err.lower()


class TestConfigFromArgs:
    """Every CLI flag must thread into the TeamConfig."""

    def test_config_from_args_threads_all_flags(self):
        from advisor.__main__ import _config_from_args

        parser = build_parser()
        args = parser.parse_args(
            [
                "pipeline",
                "some/dir",
                "--team",
                "custom-team",
                "--file-types",
                "*.js",
                "--max-runners",
                "7",
                "--min-priority",
                "2",
                "--context",
                "audit the auth flow",
                "--advisor-model",
                "opus-4",
                "--runner-model",
                "sonnet-3.5",
            ]
        )
        cfg = _config_from_args(args)
        assert cfg.team_name == "custom-team"
        assert cfg.file_types == "*.js"
        assert cfg.max_runners == 7
        assert cfg.min_priority == 2
        assert cfg.context == "audit the auth flow"
        assert cfg.advisor_model == "opus-4"
        assert cfg.runner_model == "sonnet-3.5"

    def test_config_from_args_context_pressure_flags(self):
        """The three context-pressure knobs must thread through the CLI."""
        from advisor.__main__ import _config_from_args

        parser = build_parser()
        args = parser.parse_args(
            [
                "pipeline",
                "some/dir",
                "--max-fixes-per-runner",
                "3",
                "--large-file-line-threshold",
                "500",
                "--large-file-max-fixes",
                "1",
            ]
        )
        cfg = _config_from_args(args)
        assert cfg.max_fixes_per_runner == 3
        assert cfg.large_file_line_threshold == 500
        assert cfg.large_file_max_fixes == 1

    def test_config_from_args_context_pressure_defaults(self):
        """Without the flags, config falls back to the documented defaults."""
        from advisor.__main__ import _config_from_args

        parser = build_parser()
        args = parser.parse_args(["pipeline", "some/dir"])
        cfg = _config_from_args(args)
        assert cfg.max_fixes_per_runner == 5
        assert cfg.large_file_line_threshold == 800
        assert cfg.large_file_max_fixes == 3


class TestCmdPlanJson:
    """``advisor plan --json`` emits parseable JSON with the expected shape."""

    def test_plan_json_output(self, tmp_path, capsys):
        from advisor.__main__ import cmd_plan

        (tmp_path / "auth.py").write_text("login = True")
        parser = build_parser()
        args = parser.parse_args(
            [
                "plan",
                str(tmp_path),
                "--json",
                "--min-priority",
                "1",
            ]
        )
        assert cmd_plan(args) == 0
        import json

        data = json.loads(capsys.readouterr().out)
        assert data["target"] == str(tmp_path)
        assert data["task_count"] >= 1
        assert all("file_path" in t and "priority" in t for t in data["tasks"])

    def test_plan_json_respects_advisorignore(self, tmp_path, capsys):
        from advisor.__main__ import cmd_plan

        (tmp_path / "auth.py").write_text("password = 'x'\n")
        (tmp_path / "secrets.py").write_text("api_key = 'y'\n")
        (tmp_path / ".advisorignore").write_text("secrets.py\n")
        parser = build_parser()
        args = parser.parse_args(
            [
                "plan",
                str(tmp_path),
                "--json",
                "--min-priority",
                "1",
            ]
        )
        assert cmd_plan(args) == 0
        import json

        data = json.loads(capsys.readouterr().out)
        names = sorted(Path(t["file_path"]).name for t in data["tasks"])
        assert names == ["auth.py"]


class TestCmdStatusStrict:
    """``advisor status --strict`` returns 3 when install is unhealthy."""

    def test_strict_returns_3_when_missing(self, tmp_path, capsys):
        from advisor.__main__ import cmd_status

        parser = build_parser()
        args = parser.parse_args(
            [
                "status",
                "--path",
                str(tmp_path / "missing-CLAUDE.md"),
                "--skill-path",
                str(tmp_path / "missing-SKILL.md"),
                "--strict",
            ]
        )
        assert cmd_status(args) == 3

    def test_non_strict_returns_0_even_when_missing(self, tmp_path):
        from advisor.__main__ import cmd_status

        parser = build_parser()
        args = parser.parse_args(
            [
                "status",
                "--path",
                str(tmp_path / "missing-CLAUDE.md"),
                "--skill-path",
                str(tmp_path / "missing-SKILL.md"),
            ]
        )
        assert cmd_status(args) == 0


class TestCmdInstallUninstall:
    """End-to-end install/uninstall with overridden paths (safe tmp_path)."""

    def test_install_then_uninstall_round_trip(self, tmp_path, capsys):
        from advisor.__main__ import cmd_install, cmd_uninstall

        parser = build_parser()
        claude_md = tmp_path / "CLAUDE.md"
        skill_md = tmp_path / "skills" / "advisor" / "SKILL.md"

        install_args = parser.parse_args(
            [
                "install",
                "--path",
                str(claude_md),
                "--skill-path",
                str(skill_md),
                "--quiet",
            ]
        )
        assert cmd_install(install_args) == 0
        assert claude_md.exists()
        assert skill_md.exists()

        uninstall_args = parser.parse_args(
            [
                "uninstall",
                "--path",
                str(claude_md),
                "--skill-path",
                str(skill_md),
                "--quiet",
            ]
        )
        assert cmd_uninstall(uninstall_args) == 0
        # The CLAUDE.md file remains (stripped of the nudge), but the skill is removed.
        assert not skill_md.exists()

    def test_install_strict_exits_3_on_noop(self, tmp_path):
        from advisor.__main__ import cmd_install

        parser = build_parser()
        claude_md = tmp_path / "CLAUDE.md"
        skill_md = tmp_path / "skills" / "advisor" / "SKILL.md"
        # First install: changed.
        args_first = parser.parse_args(
            [
                "install",
                "--path",
                str(claude_md),
                "--skill-path",
                str(skill_md),
                "--quiet",
            ]
        )
        cmd_install(args_first)
        # Second install --strict: nothing changed, should exit 3.
        args_second = parser.parse_args(
            [
                "install",
                "--path",
                str(claude_md),
                "--skill-path",
                str(skill_md),
                "--quiet",
                "--strict",
            ]
        )
        assert cmd_install(args_second) == 3


class TestFormatStatusOptOut:
    """Status banner warns visibly when ADVISOR_NO_NUDGE is set."""

    def test_opt_out_shows_warning_line(self, tmp_path):
        from advisor.__main__ import _format_status
        from advisor.install import ComponentStatus, Status

        s = Status(
            nudge=ComponentStatus(
                name="nudge",
                path=tmp_path / "CLAUDE.md",
                present=True,
                current=True,
            ),
            skill=ComponentStatus(
                name="skill",
                path=tmp_path / "SKILL.md",
                present=True,
                current=True,
            ),
            opt_out=True,
        )
        out = _format_status(s, "9.9.9")
        assert "auto-install disabled" in out


class TestPrintCompletion:
    """--print-completion emits a completion script when shtab is installed,
    otherwise fails loudly with an install hint."""

    def test_missing_shtab_returns_1_and_prints_hint(self, monkeypatch, capsys):
        from advisor import __main__ as cli

        # Force shtab import to fail:
        monkeypatch.setitem(sys.modules, "shtab", None)
        rc = cli.main(["--print-completion", "bash"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "shtab" in err

    def test_with_shtab_emits_script(self, capsys):
        pytest.importorskip("shtab")
        from advisor import __main__ as cli

        # shtab installs its own argparse Action that prints the completion
        # script and calls sys.exit(0). Accept either a 0 return or a
        # SystemExit(0) — both mean "shtab handled it successfully".
        try:
            rc = cli.main(["--print-completion", "bash"])
        except SystemExit as exc:
            assert exc.code in (0, None), f"shtab action exited with {exc.code}"
        else:
            assert rc == 0
        out = capsys.readouterr().out
        assert out  # completion script body
        # Bash completion uses `complete -F ...`:
        assert "complete" in out.lower()

    def test_no_subcommand_errors_helpfully(self, capsys):
        from advisor import __main__ as cli

        with pytest.raises(SystemExit):
            cli.main([])
        err = capsys.readouterr().err
        assert "subcommand" in err.lower()


class TestCmdProtocol:
    """`advisor protocol` prints the strict TeamCreate/TeamDelete sequence."""

    def test_prints_lifecycle_steps(self, capsys):
        from advisor import __main__ as cli

        rc = cli.main(["protocol"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "TeamCreate" in out
        assert "TeamDelete" in out
        assert "shutdown_request" in out
        # Must warn against the broadcast pitfall documented in the audit:
        assert 'broadcast "*"' in out or 'broadcast `"*"`' in out

    def test_triggers_first_run_setup(self, tmp_path, monkeypatch):
        """`protocol` — like every non-install command — triggers the
        documented first-run auto-install of the nudge + skill so the
        README promise holds. ``ensure_nudge`` is idempotent, so subsequent
        invocations see an already-installed nudge and no-op.
        """
        from advisor import __main__ as cli

        from .conftest import isolate_home

        isolate_home(monkeypatch, tmp_path)
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        assert not claude_md.exists()
        rc = cli.main(["protocol"])
        assert rc == 0
        # First-run installs the nudge as a side effect:
        assert claude_md.exists()


class TestMainEntrypoint:
    """End-to-end coverage for the ``main()`` wrapper itself."""

    def test_version_flag_prints_and_exits(self, capsys):
        """``advisor --version`` matches the installed distribution version."""
        from advisor import __main__ as cli
        from advisor import __version__ as pkg_version

        with pytest.raises(SystemExit) as exc_info:
            cli.main(["--version"])
        assert exc_info.value.code == 0
        out = capsys.readouterr().out
        assert pkg_version in out

    def test_missing_subcommand_errors(self, capsys):
        """Running ``advisor`` with no subcommand prints an argparse error."""
        from advisor import __main__ as cli

        with pytest.raises(SystemExit) as exc_info:
            cli.main([])
        # argparse.error() exits with code 2
        assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "subcommand" in err.lower()

    def test_broken_pipe_exits_cleanly(self, monkeypatch):
        """A downstream pipe closing mid-output must not raise.

        Regression guard: ``advisor plan . | head -n 1`` used to bubble a
        ``BrokenPipeError`` traceback up from the subprocess when head exited
        before advisor finished writing.
        """
        import io

        from advisor import __main__ as cli

        def _raise_pipe(_args):
            raise BrokenPipeError

        # Swap the real stdout for a throwaway StringIO so ``main()``'s
        # ``sys.stdout.close()`` in the BrokenPipeError branch only closes
        # the throwaway — pytest's captured stdout stays intact.
        fake_stdout = io.StringIO()
        monkeypatch.setattr(sys, "stdout", fake_stdout)

        parser = cli.build_parser()
        for action in parser._subparsers._group_actions:  # type: ignore[attr-defined]
            for name, sp in getattr(action, "choices", {}).items():
                if name == "status":
                    sp.set_defaults(func=_raise_pipe)
        monkeypatch.setattr(cli, "build_parser", lambda: parser)

        rc = cli.main(["status"])
        # BrokenPipeError path returns 0 — the downstream reader left first;
        # that is not an error condition for us.
        assert rc == 0

    def test_print_completion_without_shtab_errors(self, monkeypatch, capsys):
        """Without the shtab extra, ``--print-completion bash`` must exit 1
        with a useful install hint (not a cryptic ModuleNotFoundError)."""
        import builtins

        from advisor import __main__ as cli

        real_import = builtins.__import__

        def _no_shtab(name, *args, **kwargs):
            if name == "shtab":
                raise ImportError("shtab not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_shtab)
        rc = cli.main(["--print-completion", "bash"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "shtab" in err.lower()
        assert "completion" in err.lower()

    def test_subcommand_returning_none_is_normalized_to_zero(self, monkeypatch):
        """Some future subcommand returning ``None`` must land on exit 0.

        The ``rc = args.func(args); return int(rc) if rc is not None else 0``
        guard was added specifically because mypy strict had flagged
        ``args.func`` as returning ``Any``. This locks in the behavior.
        """
        from advisor import __main__ as cli

        def _returns_none(_args):
            return None

        parser = cli.build_parser()
        for action in parser._subparsers._group_actions:  # type: ignore[attr-defined]
            for name, sp in getattr(action, "choices", {}).items():
                if name == "protocol":
                    sp.set_defaults(func=_returns_none)

        monkeypatch.setattr(cli, "build_parser", lambda: parser)
        rc = cli.main(["protocol"])
        assert rc == 0


class TestCmdPlanErrorPaths:
    """Coverage for `cmd_plan` JSON output and empty-result paths."""

    def test_json_output_with_batches(self, tmp_path, capsys):
        """`advisor plan --json --batch-size N` emits batch structure."""
        import json

        from advisor import __main__ as cli

        (tmp_path / "auth.py").write_text("def login(): pass\n")
        (tmp_path / "helper.py").write_text("x = 1\n")
        rc = cli.main(
            [
                "plan",
                str(tmp_path),
                "--json",
                "--batch-size",
                "2",
                "--min-priority",
                "1",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["target"] == str(tmp_path)
        assert "tasks" in data
        assert "batches" in data
        assert isinstance(data["batches"], list)

    def test_empty_target_dir_prints_warning(self, tmp_path, capsys):
        """Empty dir renders a helpful tip rather than a blank plan."""
        from advisor import __main__ as cli

        rc = cli.main(["plan", str(tmp_path), "--min-priority", "5"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no files" in out or "try --min-priority" in out

    def test_plan_rejects_file_target_not_directory(self, tmp_path, capsys):
        """Passing a file path to `advisor plan` must fail loudly with a
        directory-required error rather than silently producing an empty
        plan via ``_safe_rglob``.
        """
        from advisor import __main__ as cli

        f = tmp_path / "single.py"
        f.write_text("x = 1\n", encoding="utf-8")
        rc = cli.main(["plan", str(f)])
        assert rc == 2
        err = capsys.readouterr().err
        assert "must be a directory" in err

    def test_max_runners_above_ceiling_warns_and_clamps(self, tmp_path, capsys):
        """Passing ``--max-runners`` above the ceiling now emits a visible
        warning instead of silently clamping. The ceiling itself is the
        same ``_MAX_RUNNERS_CEILING`` value enforced inside the config.
        """
        from advisor import __main__ as cli

        (tmp_path / "auth.py").write_text("x = 1\n", encoding="utf-8")
        rc = cli.main(
            [
                "plan",
                str(tmp_path),
                "--max-runners",
                "100",
                "--min-priority",
                "1",
            ]
        )
        assert rc == 0
        err = capsys.readouterr().err
        assert "100" in err
        assert "20" in err

    def test_bad_file_types_glob_errors_cleanly(self, tmp_path, capsys):
        """A malformed glob exits non-zero with a visible error, not a trace."""
        from advisor import __main__ as cli

        # `[` with no closing `]` is a malformed fnmatch range.
        rc = cli.main(["plan", str(tmp_path), "--file-types", "[unclosed"])
        # cmd_plan prints the _safe_rglob error and returns 1. Accept either
        # 0 (if the glob is tolerated) or 1 (if the platform rejects it).
        err = capsys.readouterr().err
        assert rc in (0, 1)
        if rc == 1:
            assert "pattern" in err.lower() or "filesystem" in err.lower()

    def test_absolute_file_types_pattern_errors_cleanly(self, tmp_path, capsys):
        from advisor import __main__ as cli

        rc = cli.main(["plan", str(tmp_path), "--file-types", "C:\\*.py"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "pattern" in err.lower()


class TestStatusJsonFlag:
    """`advisor status --json` is the scripting-friendly variant."""

    def test_status_json_has_version_and_components(self, tmp_path, monkeypatch, capsys):
        import json

        from advisor import __main__ as cli
        from advisor import __version__ as pkg_version

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("ADVISOR_NO_NUDGE", "1")
        rc = cli.main(["status", "--json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["version"] == pkg_version
        assert "nudge" in data
        assert "skill" in data
        assert isinstance(data["nudge"]["present"], bool)

    def test_status_strict_when_unhealthy_exits_3(self, tmp_path, monkeypatch, capsys):
        """`--strict` exits 3 when the install is incomplete."""
        from advisor import __main__ as cli

        from .conftest import isolate_home

        isolate_home(monkeypatch, tmp_path)
        monkeypatch.setenv("ADVISOR_NO_NUDGE", "1")
        rc = cli.main(["status", "--strict"])
        # Install is missing → non-zero
        assert rc != 0


class TestCmdInstallErrorPaths:
    """`cmd_install` / `cmd_uninstall` OSError handling via `_run_install_op`."""

    def test_nudge_oserror_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        """An OSError from the nudge installer is reported and exits 1."""
        from advisor import __main__ as cli

        def _broken(path=None, body=None):
            raise OSError("permission denied")

        monkeypatch.setattr(cli, "install_nudge", _broken)
        parser = cli.build_parser()
        args = parser.parse_args(["install", "--path", str(tmp_path / "CLAUDE.md")])
        rc = cli.cmd_install(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "nudge" in err.lower()

    def test_skill_oserror_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        """An OSError from the skill installer is reported and exits 1.

        The nudge must have succeeded first, so the error line specifically
        cites `skill:` — not `nudge:`."""
        from advisor import __main__ as cli

        def _broken_skill(path=None, body=None):
            raise OSError("readonly filesystem")

        monkeypatch.setattr(cli, "install_skill", _broken_skill)
        parser = cli.build_parser()
        args = parser.parse_args(
            [
                "install",
                "--path",
                str(tmp_path / "CLAUDE.md"),
                "--skill-path",
                str(tmp_path / "skill" / "advisor" / "SKILL.md"),
            ]
        )
        rc = cli.cmd_install(args)
        assert rc == 1
        err = capsys.readouterr().err
        assert "skill" in err.lower()

    def test_install_quiet_suppresses_component_lines(self, tmp_path, capsys):
        """`--quiet` suppresses per-component lines but still sets exit code."""
        from advisor import __main__ as cli

        claude_md = tmp_path / "CLAUDE.md"
        skill_md = tmp_path / "skill" / "advisor" / "SKILL.md"
        rc = cli.main(
            [
                "install",
                "--path",
                str(claude_md),
                "--skill-path",
                str(skill_md),
                "--quiet",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        # Quiet → no nudge/skill action lines; install still worked.
        assert "installed" not in out.lower()
        assert claude_md.exists()

    def test_install_skip_skill_only_installs_nudge(self, tmp_path):
        """`--skip-skill` installs ONLY the nudge."""
        from advisor import __main__ as cli

        claude_md = tmp_path / "CLAUDE.md"
        skill_md = tmp_path / "skill" / "advisor" / "SKILL.md"
        rc = cli.main(
            [
                "install",
                "--path",
                str(claude_md),
                "--skill-path",
                str(skill_md),
                "--skip-skill",
            ]
        )
        assert rc == 0
        assert claude_md.exists()
        assert not skill_md.exists()


class TestCmdPromptHappyPaths:
    """Smoke-test the `advisor prompt {advisor,runner}` happy paths."""

    def test_advisor_prompt_prints_body(self, capsys):
        from advisor import __main__ as cli

        rc = cli.main(["prompt", "advisor", "."])
        assert rc == 0
        out = capsys.readouterr().out
        # Opus prompt body lives in the resource file — the rendered prompt
        # references Python files and the target:
        assert "Target" in out or "target" in out

    def test_runner_prompt_prints_body(self, capsys):
        from advisor import __main__ as cli

        rc = cli.main(["prompt", "runner", "."])
        assert rc == 0
        out = capsys.readouterr().out
        assert "runner-1" in out

    def test_pipeline_happy_path(self, capsys):
        from advisor import __main__ as cli

        rc = cli.main(["pipeline", "."])
        assert rc == 0
        out = capsys.readouterr().out
        assert "/advisor" in out


class TestCompletionHintPackageName:
    """`--print-completion` error must reference the real PyPI distribution."""

    def test_install_hint_uses_advisor_agent(self, monkeypatch, capsys):
        import builtins

        from advisor import __main__ as cli

        real_import = builtins.__import__

        def _no_shtab(name, *args, **kwargs):
            if name == "shtab":
                raise ImportError("shtab not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_shtab)
        rc = cli.main(["--print-completion", "bash"])
        assert rc == 1
        err = capsys.readouterr().err
        # The distribution on PyPI is `advisor-agent`, not `advisor`.
        # The old hint (``'advisor[completion]'``) would fail with
        # ``No matching distribution found`` when a user copy-pasted it.
        assert "advisor-agent[completion]" in err
        assert "'advisor[completion]'" not in err


class TestCmdProtocolDefaults:
    """The `advisor protocol` text must match current defaults."""

    def test_protocol_uses_current_defaults(self, capsys):
        from advisor import __main__ as cli

        rc = cli.main(["protocol"])
        assert rc == 0
        out = capsys.readouterr().out
        # Default team name is ``review``; stale ``advisor-review`` would
        # mislead anyone copy-pasting the protocol into a session.
        assert 'TeamCreate(name="review")' in out
        assert "advisor-review" not in out
        # Default models are the version-pinned shortcuts ``opus-4-7``
        # and ``sonnet-4-6``. The old text hardcoded ``opus``/``sonnet``,
        # which silently drift every time Claude Code retargets the
        # bare aliases.
        assert 'model="opus-4-7"' in out
        assert 'model="sonnet-4-6"' in out
        # P1-1: the printed protocol must reference the live subagent
        # type ``advisor-executor`` — the old text said ``deep-reasoning``
        # which contradicted ``build_advisor_agent``.
        assert 'subagent_type="advisor-executor"' in out
        assert "deep-reasoning" not in out


class TestCmdPlanResumeConfig:
    """`--resume` must use the checkpointed run config for cost estimates."""

    def test_resume_uses_checkpointed_models_for_estimate(self, tmp_path, capsys):
        import json as _json

        from advisor import __main__ as cli
        from advisor.checkpoint import save_checkpoint
        from advisor.focus import FocusTask

        (tmp_path / "auth.py").write_text("def login(password): ...\n", encoding="utf-8")
        run_id = "resume-test"
        save_checkpoint(
            tmp_path,
            run_id=run_id,
            tasks=[FocusTask(file_path=str(tmp_path / "auth.py"), priority=5, prompt="p")],
            batches=None,
            team_name="review",
            file_types="*.py",
            min_priority=3,
            max_runners=5,
            advisor_model="haiku",  # non-default; current CLI args would say ``opus``
            runner_model="haiku",
            max_fixes_per_runner=5,
            test_command="",
            context="",
        )

        rc = cli.main(
            [
                "plan",
                str(tmp_path),
                "--resume",
                run_id,
                "--estimate",
                "--json",
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        payload = _json.loads(out)
        est = payload["estimate"]
        # Before the fix, cost estimation grabbed models from the current
        # CLI args (``opus``/``sonnet``) rather than from the checkpoint.
        assert est["advisor_model"] == "haiku"
        assert est["runner_model"] == "haiku"


class TestHistoryLimitValidation:
    """`--limit` must reject values that would break the slice semantics."""

    def test_negative_limit_rejected(self, capsys):
        from advisor import __main__ as cli

        with pytest.raises(SystemExit) as exc:
            cli.main(["history", ".", "--limit", "-5"])
        assert exc.value.code == 2  # argparse error
        err = capsys.readouterr().err
        assert "--limit" in err

    def test_zero_limit_rejected(self, capsys):
        from advisor import __main__ as cli

        with pytest.raises(SystemExit) as exc:
            cli.main(["history", ".", "--limit", "0"])
        assert exc.value.code == 2


class TestCmdVersion:
    """``advisor version`` reports environment details with optional JSON."""

    def test_plain_version_prints_details(self, capsys):
        from advisor import __main__ as cli

        rc = cli.main(["version"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "advisor" in out
        assert "python" in out
        assert "install" in out

    def test_json_version_has_stable_keys(self, capsys):
        import json as _json

        from advisor import __main__ as cli

        rc = cli.main(["version", "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        payload = _json.loads(out)
        for key in (
            "schema_version",
            "advisor_version",
            "python_version",
            "python_implementation",
            "install_path",
            "platform",
        ):
            assert key in payload


class TestCmdCheckpoints:
    """``advisor checkpoints`` lists + deletes saved plans."""

    def _write_stub_checkpoint(self, tmp_path, run_id):
        from advisor.checkpoint import checkpoint_path

        p = checkpoint_path(tmp_path, run_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{}", encoding="utf-8")
        return p

    def test_list_empty_prints_placeholder(self, tmp_path, capsys):
        from advisor import __main__ as cli

        rc = cli.main(["checkpoints", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no checkpoints" in out

    def test_list_json_emits_ids(self, tmp_path, capsys):
        import json as _json

        from advisor import __main__ as cli

        self._write_stub_checkpoint(tmp_path, "20260101T000000Z-abc123")
        self._write_stub_checkpoint(tmp_path, "20260101T000001Z-def456")
        rc = cli.main(["checkpoints", str(tmp_path), "--json"])
        assert rc == 0
        out = capsys.readouterr().out
        payload = _json.loads(out)
        assert payload["count"] == 2
        assert set(payload["run_ids"]) == {
            "20260101T000000Z-abc123",
            "20260101T000001Z-def456",
        }

    def test_rm_removes_specific_checkpoint(self, tmp_path):
        from advisor import __main__ as cli
        from advisor.checkpoint import checkpoint_path

        keep = self._write_stub_checkpoint(tmp_path, "20260101T000000Z-keepme")
        gone = self._write_stub_checkpoint(tmp_path, "20260101T000001Z-nukeme")
        rc = cli.main(["checkpoints", str(tmp_path), "--rm", "20260101T000001Z-nukeme", "--quiet"])
        assert rc == 0
        assert keep.exists()
        assert not gone.exists()
        # idempotent: removing again returns 0
        assert (
            cli.main(["checkpoints", str(tmp_path), "--rm", "20260101T000001Z-nukeme", "--quiet"])
            == 0
        )
        # sanity: path helper agrees with our stub layout
        assert checkpoint_path(tmp_path, "20260101T000000Z-keepme") == keep

    def test_clear_removes_all(self, tmp_path):
        from advisor import __main__ as cli
        from advisor.checkpoint import list_checkpoints

        self._write_stub_checkpoint(tmp_path, "20260101T000000Z-a")
        self._write_stub_checkpoint(tmp_path, "20260101T000001Z-b")
        rc = cli.main(["checkpoints", str(tmp_path), "--clear", "--quiet"])
        assert rc == 0
        assert list_checkpoints(tmp_path) == []

    def test_rm_and_clear_are_mutually_exclusive(self, tmp_path, capsys):
        from advisor import __main__ as cli

        rc = cli.main(["checkpoints", str(tmp_path), "--rm", "anything", "--clear"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "mutually exclusive" in err

    def test_rm_rejects_path_traversal_run_id(self, tmp_path, capsys):
        from advisor import __main__ as cli

        rc = cli.main(["checkpoints", str(tmp_path), "--rm", "..\\..\\victim"])
        assert rc == 2
        err = capsys.readouterr().err
        assert "invalid run_id" in err

    def test_list_shows_relative_age(self, tmp_path, capsys, monkeypatch):
        """The list gains an ``Xm ago`` column sourced from mtime.

        Gives Claude Code users a cheap staleness cue so they can pick the
        newest checkpoint without parsing the 20-char timestamp embedded
        in the run_id itself.
        """
        import os

        from advisor import __main__ as cli

        # Write two checkpoints and backdate one by ~10 minutes so we
        # exercise the "Xm ago" branch deterministically.
        now_p = self._write_stub_checkpoint(tmp_path, "20260421T000000Z-new")
        old_p = self._write_stub_checkpoint(tmp_path, "20260420T000000Z-old")
        now = 1_700_000_000.0
        os.utime(now_p, (now, now))
        os.utime(old_p, (now - 600, now - 600))  # 10 min earlier
        monkeypatch.setattr(cli.time, "time", lambda: now)

        rc = cli.main(["checkpoints", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "just now" in out
        assert "10m ago" in out

    def test_list_single_checkpoint_suppresses_resume_tip(self, tmp_path, capsys):
        """With only one row the ``resume with…`` tip is redundant — the
        run_id is already on the line above — so the CLI skips it.
        """
        from advisor import __main__ as cli

        self._write_stub_checkpoint(tmp_path, "20260421T000000Z-only")
        rc = cli.main(["checkpoints", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "resume with" not in out

    def test_list_multi_checkpoint_emits_resume_tip(self, tmp_path, capsys):
        from advisor import __main__ as cli

        self._write_stub_checkpoint(tmp_path, "20260421T000000Z-a")
        self._write_stub_checkpoint(tmp_path, "20260421T000001Z-b")
        rc = cli.main(["checkpoints", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "resume" in out and "advisor plan --resume <RUN_ID>" in out

    def test_empty_list_suggests_checkpoint_flag(self, tmp_path, capsys):
        """Empty-state message nudges towards ``--checkpoint``."""
        from advisor import __main__ as cli

        rc = cli.main(["checkpoints", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no checkpoints yet" in out
        assert "advisor plan --checkpoint" in out


class TestRelativeAge:
    """Boundary coverage for the ``_relative_age`` formatter."""

    def test_sub_second_is_just_now(self):
        from advisor.__main__ import _relative_age

        assert _relative_age(100.0, now_epoch=100.5) == "just now"

    def test_seconds_and_minutes_and_hours_and_days(self):
        from advisor.__main__ import _relative_age

        base = 10_000.0
        assert _relative_age(base, now_epoch=base + 5) == "5s ago"
        assert _relative_age(base, now_epoch=base + 180) == "3m ago"
        assert _relative_age(base, now_epoch=base + 7200) == "2h ago"
        assert _relative_age(base, now_epoch=base + 3 * 86400) == "3d ago"

    def test_future_mtime_clamped_to_just_now(self):
        """Clock skew (mtime > now) should never render as a negative age."""
        from advisor.__main__ import _relative_age

        assert _relative_age(100.0, now_epoch=50.0) == "just now"

    def test_extreme_age_caps_at_99_days(self):
        from advisor.__main__ import _relative_age

        assert _relative_age(0.0, now_epoch=1000 * 86400) == "99d ago"


class TestCmdPlanExclude:
    """``--exclude`` filters paths before ranking."""

    def test_exclude_drops_matching_files(self, tmp_path, capsys):
        import json as _json

        from advisor import __main__ as cli

        (tmp_path / "app.py").write_text("def f():\n    pass\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_app.py").write_text("def test_f():\n    pass\n")
        rc = cli.main(
            [
                "plan",
                str(tmp_path),
                "--min-priority",
                "1",
                "--exclude",
                "tests/**",
                "--json",
            ]
        )
        assert rc == 0
        payload = _json.loads(capsys.readouterr().out)
        names = {Path(t["file_path"]).name for t in payload["tasks"]}
        assert "app.py" in names
        assert "test_app.py" not in names


class TestCmdPlanPricing:
    """``--pricing FILE`` overrides the default per-family pricing."""

    def test_estimate_honors_max_runners(self, tmp_path, capsys):
        import json as _json

        from advisor import __main__ as cli

        for i in range(4):
            (tmp_path / f"auth{i}.py").write_text("password = 'x'\n")

        rc = cli.main(
            [
                "plan",
                str(tmp_path),
                "--min-priority",
                "1",
                "--max-runners",
                "2",
                "--estimate",
                "--json",
            ]
        )

        assert rc == 0
        payload = _json.loads(capsys.readouterr().out)
        assert payload["estimate"]["runner_count"] == 2

    def test_pricing_file_overrides_estimate(self, tmp_path, capsys):
        import json as _json

        from advisor import __main__ as cli

        (tmp_path / "a.py").write_text("x = 1\n" * 200)
        pricing = tmp_path / "pricing.json"
        pricing.write_text(
            _json.dumps(
                {
                    "opus": {"input": 1, "output": 1},
                    "sonnet": {"input": 1, "output": 1},
                    "haiku": {"input": 1, "output": 1},
                }
            )
        )
        rc = cli.main(
            [
                "plan",
                str(tmp_path),
                "--min-priority",
                "1",
                "--estimate",
                "--pricing",
                str(pricing),
                "--json",
            ]
        )
        assert rc == 0
        payload = _json.loads(capsys.readouterr().out)
        est = payload["estimate"]
        # With 1c/1c/Mtok across the board, cost is orders of magnitude
        # below the 300/1500c sonnet default — anything > $0.01 means
        # the override didn't take effect.
        assert est["cost_usd_max"] < 0.01

    def test_invalid_pricing_file_errors_cleanly(self, tmp_path, capsys):
        from advisor import __main__ as cli

        (tmp_path / "a.py").write_text("x = 1\n")
        bad = tmp_path / "pricing.json"
        bad.write_text("{not json")
        rc = cli.main(
            [
                "plan",
                str(tmp_path),
                "--min-priority",
                "1",
                "--estimate",
                "--pricing",
                str(bad),
                "--json",
            ]
        )
        assert rc == 2
        err = capsys.readouterr().err
        assert "pricing file" in err


class TestCmdPlanGitignoreTip:
    """Checkpointing a plan nudges the user when ``.advisor/`` isn't ignored."""

    def test_tip_printed_when_gitignore_lacks_advisor(self, tmp_path, capsys):
        from advisor import __main__ as cli

        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / ".gitignore").write_text("__pycache__/\n*.pyc\n")
        rc = cli.main(["plan", str(tmp_path), "--min-priority", "1", "--checkpoint"])
        assert rc == 0
        err = capsys.readouterr().err
        assert ".advisor/" in err

    def test_tip_suppressed_when_gitignore_has_advisor(self, tmp_path, capsys):
        from advisor import __main__ as cli

        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / ".gitignore").write_text(".advisor/\n")
        rc = cli.main(["plan", str(tmp_path), "--min-priority", "1", "--checkpoint"])
        assert rc == 0
        err = capsys.readouterr().err
        assert ".advisor/" not in err

    def test_tip_suppressed_without_gitignore(self, tmp_path, capsys):
        from advisor import __main__ as cli

        (tmp_path / "a.py").write_text("x = 1\n")
        rc = cli.main(["plan", str(tmp_path), "--min-priority", "1", "--checkpoint"])
        assert rc == 0
        err = capsys.readouterr().err
        assert ".advisor/" not in err


class TestCmdPlanCheckpointSaveCosmetics:
    """The checkpoint-save success message is friendlier and actionable."""

    def test_save_shows_success_and_resume_hint(self, tmp_path, capsys):
        """Output includes the success glyph, the run_id, and the exact
        ``advisor plan --resume <id>`` command so the next step is a copy.
        """
        from advisor import __main__ as cli

        (tmp_path / "a.py").write_text("x = 1\n")
        rc = cli.main(["plan", str(tmp_path), "--min-priority", "1", "--checkpoint"])
        assert rc == 0
        out = capsys.readouterr().out
        # Success glyph (or ASCII fallback) precedes the status line.
        assert "checkpoint saved:" in out
        # Resume hint must echo the actual run_id so copy/paste works.
        assert "advisor plan --resume 2026" in out
        # Legacy verbose key is gone in favour of the cleaner format.
        assert "run_id=" not in out


class TestCmdHistoryEmptyState:
    """``advisor history`` on a fresh tree guides the reader."""

    def test_empty_output_includes_tip(self, tmp_path, capsys):
        from advisor import __main__ as cli

        rc = cli.main(["history", str(tmp_path)])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no history yet" in out
        # Tip explains *when* entries appear so the command isn't a dead-end.
        assert "when you confirm them" in out


class TestNoColorFlag:
    """``--no-color`` flag disables ANSI output and is honored before dispatch."""

    def test_no_color_flag_disables_ansi(self, tmp_path, capsys, monkeypatch):
        from advisor import __main__ as cli
        from advisor import _style

        monkeypatch.delenv("NO_COLOR", raising=False)
        _style.reset_color_cache()
        rc = cli.main(["--no-color", "status"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "\033[" not in out

    def test_no_color_flag_sets_no_color_env(self, monkeypatch):
        import os

        from advisor import __main__ as cli
        from advisor import _style

        monkeypatch.delenv("NO_COLOR", raising=False)
        _style.reset_color_cache()
        cli.main(["--no-color", "status"])
        assert os.environ.get("NO_COLOR") == "1"
        assert _style.supports_color() is False
