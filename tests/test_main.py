"""Tests for advisor.__main__ CLI."""

import io
import sys

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

    @pytest.mark.parametrize("cmd", ["plan", "pipeline", "prompt", "install", "uninstall", "status", "doctor"])
    def test_read_only_commands_skip_nudge(self, cmd):
        assert cmd in _NUDGE_SKIP_COMMANDS
