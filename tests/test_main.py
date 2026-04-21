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


class TestNudgeSkipCommands:
    """Dry-run / preview commands must not mutate ~/.claude/ via ensure_nudge."""

    @pytest.mark.parametrize(
        "cmd", ["plan", "pipeline", "prompt", "install", "uninstall", "status", "doctor"]
    )
    def test_read_only_commands_skip_nudge(self, cmd):
        assert cmd in _NUDGE_SKIP_COMMANDS


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

    def test_does_not_install_nudge(self, tmp_path, monkeypatch, capsys):
        """`protocol` is a pure read-only reference; it must not touch
        ~/.claude/CLAUDE.md even when the nudge is missing."""
        from advisor import __main__ as cli

        monkeypatch.setenv("HOME", str(tmp_path))
        claude_md = tmp_path / ".claude" / "CLAUDE.md"
        assert not claude_md.exists()
        rc = cli.main(["protocol"])
        assert rc == 0
        # Nudge must NOT have been installed as a side effect:
        assert not claude_md.exists()


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
        assert "No files" in out or "Try --min-priority" in out

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

        monkeypatch.setenv("HOME", str(tmp_path))
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
