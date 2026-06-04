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

    def test_update_skill_is_checked(self, tmp_path: Path, monkeypatch) -> None:
        """Doctor must surface missing /advisor-update install state."""
        from advisor.install import install, install_skill

        from .conftest import isolate_home

        isolate_home(monkeypatch, tmp_path)
        nudge = tmp_path / ".claude" / "CLAUDE.md"
        skill = tmp_path / ".claude" / "skills" / "advisor" / "SKILL.md"
        install(path=nudge)
        install_skill(path=skill)

        r = run_doctor(nudge_path=nudge, skill_path=skill)
        update_checks = [c for c in r.checks if c.name == "install-update-skill"]
        assert len(update_checks) == 1
        assert update_checks[0].level == "warn"

    def test_format_report_redacts_unknown_env_var(self, tmp_path: Path, monkeypatch) -> None:
        """A-12: A new env var added to _KNOWN_ENV_VARS but not to
        _KNOWN_SAFE_ENV_VARS must be redacted in the doctor report."""
        import advisor.doctor as doctor_mod

        hypothetical = "ADVISOR_FUTURE_API_TOKEN"
        secret_value = "s3cr3t-token"

        # Temporarily extend _KNOWN_ENV_VARS to include the hypothetical var.
        extended = (*doctor_mod._KNOWN_ENV_VARS, hypothetical)
        monkeypatch.setattr(doctor_mod, "_KNOWN_ENV_VARS", extended)
        monkeypatch.setenv(hypothetical, secret_value)

        nudge = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        r = run_doctor(nudge_path=nudge, skill_path=skill)

        # The key must appear (presence preserved) but the value must be redacted.
        assert hypothetical in r.env_overrides
        assert r.env_overrides[hypothetical] != secret_value
        assert r.env_overrides[hypothetical] == doctor_mod._REDACTED_VALUE

        # The raw secret must not appear in the formatted report either.
        rendered = format_report(r)
        assert secret_value not in rendered

    def test_check_home_no_duplicate_is_symlink(self, tmp_path: Path) -> None:
        """A-13: _check_codex_home and _check_claude_home delegate to the
        shared _check_home_dir helper; both return correct results for a
        normal (non-symlink) directory."""
        from advisor.doctor import _check_home_dir

        existing_dir = tmp_path / "mydir"
        existing_dir.mkdir()

        result = _check_home_dir("test-home", existing_dir)
        assert result.level == "ok"
        assert "regular directory" in result.message

        missing = tmp_path / "missing"
        result_missing = _check_home_dir("test-home", missing)
        assert result_missing.level == "warn"
        assert "does not exist" in result_missing.message
