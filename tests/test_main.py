"""Tests for advisor.__main__ CLI."""

import io
import sys

import pytest

from advisor.__main__ import build_parser


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
