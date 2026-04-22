"""Tests for rule-pack presets (Phase 3)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from advisor.presets import PRESETS, RulePack, get_preset, list_presets
from advisor.rank import rank_files


class TestPresetCatalog:
    def test_six_presets_registered(self) -> None:
        expected = {
            "python-web",
            "python-cli",
            "node-api",
            "typescript-react",
            "go-service",
            "rust-crate",
        }
        assert set(PRESETS) == expected

    @pytest.mark.parametrize("name", list(PRESETS))
    def test_preset_shape(self, name: str) -> None:
        p = PRESETS[name]
        assert isinstance(p, RulePack)
        assert p.name == name
        assert p.description
        assert p.file_types
        assert 1 <= p.min_priority <= 5
        assert all(isinstance(k, int) and 1 <= k <= 5 for k in p.extra_keywords_by_tier)
        assert all(
            isinstance(v, tuple) and all(isinstance(s, str) for s in v)
            for v in p.extra_keywords_by_tier.values()
        )

    def test_get_preset_unknown_raises_with_list(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            get_preset("nope")
        msg = str(exc_info.value)
        # Error should list available presets so users don't have to guess.
        for name in PRESETS:
            assert name in msg

    def test_list_presets_sorted(self) -> None:
        names = [p.name for p in list_presets()]
        assert names == sorted(names)


class TestPresetKeywordOverlay:
    def test_python_web_boosts_views(self, tmp_path: Path) -> None:
        """A Flask-ish views.py should outrank utils.py under python-web."""
        (tmp_path / "views.py").write_text(
            "@app.route('/login')\n"
            "def login():\n"
            "    if not session.get('csrf'):\n"
            "        return jwt.decode(request.form['t'])\n"
        )
        (tmp_path / "utils.py").write_text("def add(a, b):\n    return a + b\n")
        files = [str(tmp_path / "views.py"), str(tmp_path / "utils.py")]
        from advisor.presets import get_preset

        pack = get_preset("python-web")
        ranked = rank_files(
            files,
            read_fn=lambda p: Path(p).read_text(),
            extra_keywords=dict(pack.extra_keywords_by_tier),
        )
        # views.py should have higher priority than utils.py.
        by_path = {r.path: r for r in ranked}
        assert (
            by_path[str(tmp_path / "views.py")].priority
            > by_path[str(tmp_path / "utils.py")].priority
        )


class TestPresetsSubcommand:
    def test_cli_pretty(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "advisor", "presets"],
            capture_output=True,
            text=True,
            env={**os.environ, "ADVISOR_NO_NUDGE": "1", "NO_COLOR": "1"},
        )
        assert result.returncode == 0
        for name in PRESETS:
            assert name in result.stdout

    def test_cli_json(self, tmp_path: Path) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "advisor", "presets", "--json"],
            capture_output=True,
            text=True,
            env={**os.environ, "ADVISOR_NO_NUDGE": "1", "NO_COLOR": "1"},
        )
        assert result.returncode == 0
        doc = json.loads(result.stdout)
        assert doc["schema_version"] == "1.0"
        assert doc["count"] == len(PRESETS)
        names = {p["name"] for p in doc["presets"]}
        assert names == set(PRESETS)


class TestTeamConfigPresetMerge:
    def test_preset_fills_file_types_when_default(self) -> None:
        from advisor.orchestrate import default_team_config

        cfg = default_team_config(
            target_dir=".",
            preset="node-api",
            warn_unknown_model=False,
        )
        assert cfg.file_types == "*.js,*.ts"
        assert cfg.preset == "node-api"

    def test_explicit_overrides_preset(self) -> None:
        from advisor.orchestrate import default_team_config

        cfg = default_team_config(
            target_dir=".",
            preset="node-api",
            file_types="*.mjs",
            warn_unknown_model=False,
        )
        assert cfg.file_types == "*.mjs"

    def test_unknown_preset_raises(self) -> None:
        from advisor.orchestrate import default_team_config

        with pytest.raises(ValueError):
            default_team_config(target_dir=".", preset="absent", warn_unknown_model=False)
