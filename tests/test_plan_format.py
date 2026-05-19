"""Tests for ``plan --format`` and the shared ``_resolve_json_output`` helper."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from advisor.__main__ import _resolve_json_output, build_parser


class TestResolveJsonOutput:
    def _ns(self, **kw: object) -> argparse.Namespace:
        return argparse.Namespace(**kw)

    def test_format_json_wins(self) -> None:
        assert _resolve_json_output(self._ns(format="json", json=False)) is True

    def test_format_pretty_overrides_legacy_json(self) -> None:
        assert _resolve_json_output(self._ns(format="pretty", json=True)) is False

    def test_legacy_json_honored_when_format_unset(self) -> None:
        assert _resolve_json_output(self._ns(format=None, json=True)) is True

    def test_default_is_pretty(self) -> None:
        assert _resolve_json_output(self._ns(format=None, json=False)) is False

    def test_missing_attrs_default_false(self) -> None:
        assert _resolve_json_output(argparse.Namespace()) is False


class TestPlanFormatCLI:
    def _run(self, capsys, argv: list[str]) -> str:
        args = build_parser().parse_args(argv)
        rc = args.func(args)
        assert rc == 0
        return capsys.readouterr().out

    def test_format_json_emits_valid_json(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "m.py").write_text("import os\nos.system('x')\n", encoding="utf-8")
        out = self._run(capsys, ["plan", str(tmp_path), "--format", "json"])
        payload = json.loads(out)
        assert payload["schema_version"]
        assert "tasks" in payload

    def test_format_json_equals_legacy_json(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "m.py").write_text("import os\nos.system('x')\n", encoding="utf-8")
        a = self._run(capsys, ["plan", str(tmp_path), "--format", "json"])
        b = self._run(capsys, ["plan", str(tmp_path), "--json"])
        assert a == b

    def test_format_pretty_overrides_json(self, tmp_path: Path, capsys) -> None:
        (tmp_path / "m.py").write_text("import os\nos.system('x')\n", encoding="utf-8")
        out = self._run(
            capsys,
            ["plan", str(tmp_path), "--min-priority", "1", "--format", "pretty", "--json"],
        )
        # Pretty wins → human dispatch plan, not a JSON document.
        assert not out.lstrip().startswith("{")
        assert "Dispatch Plan" in out
