"""Tests for ``advisor.doctor`` — diagnostic command."""

from __future__ import annotations

import os
from pathlib import Path

from advisor.doctor import DoctorReport, format_report, run_doctor


class TestRunDoctor:
    def test_returns_report(self, tmp_path: Path) -> None:
        nudge = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        r = run_doctor(nudge_path=nudge, skill_path=skill, version="0.4.0")
        assert isinstance(r, DoctorReport)
        assert r.advisor_version == "0.4.0"
        assert any(c.name == "python" for c in r.checks)
        assert any(c.name == "claude-home" for c in r.checks)

    def test_env_overrides_detected(self, tmp_path: Path) -> None:
        nudge = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        os.environ["ADVISOR_MODEL"] = "sonnet"
        try:
            r = run_doctor(nudge_path=nudge, skill_path=skill)
            assert r.env_overrides.get("ADVISOR_MODEL") == "sonnet"
        finally:
            os.environ.pop("ADVISOR_MODEL", None)

    def test_to_dict_shape(self, tmp_path: Path) -> None:
        nudge = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        r = run_doctor(nudge_path=nudge, skill_path=skill)
        d = r.to_dict()
        assert "healthy" in d
        assert "checks" in d
        assert isinstance(d["checks"], list)

    def test_format_report_produces_string(self, tmp_path: Path) -> None:
        nudge = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        r = run_doctor(nudge_path=nudge, skill_path=skill, version="0.4.0")
        s = format_report(r)
        assert "advisor doctor" in s
        assert "python" in s

    def test_healthy_flag_tracks_fails(self, tmp_path: Path) -> None:
        nudge = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        r = run_doctor(nudge_path=nudge, skill_path=skill)
        fails = [c for c in r.checks if c.level == "fail"]
        assert r.healthy == (len(fails) == 0)

    def test_opt_out_env_var_is_tracked(self, tmp_path: Path) -> None:
        """``ADVISOR_NO_NUDGE`` is the real opt-out env var (see
        ``install.OPT_OUT_ENV``). Doctor must surface it in
        ``env_overrides`` — the previous list tracked the phantom
        ``ADVISOR_NO_AUTO_INSTALL`` instead.
        """
        from advisor.install import OPT_OUT_ENV

        nudge = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        os.environ[OPT_OUT_ENV] = "1"
        try:
            r = run_doctor(nudge_path=nudge, skill_path=skill)
            assert r.env_overrides.get(OPT_OUT_ENV) == "1"
        finally:
            os.environ.pop(OPT_OUT_ENV, None)
