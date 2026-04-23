"""Tests for advisor.install — nudge append/remove helpers."""

import io
import os
import sys
from pathlib import Path

import pytest

from advisor.install import (
    END_MARKER,
    OPT_OUT_ENV,
    START_MARKER,
    _atomic_write_text,
    apply_nudge,
    ensure_nudge,
    install,
    install_skill,
    remove_nudge,
    render_block,
    should_auto_nudge,
    status,
    uninstall,
    uninstall_skill,
)
from advisor.skill_asset import SKILL_MD


class TestApplyNudge:
    def test_installs_into_empty_file(self):
        new, action = apply_nudge("")
        assert action == "installed"
        assert START_MARKER in new
        assert END_MARKER in new

    def test_appends_to_existing_content(self):
        existing = "# My CLAUDE.md\n\nHello.\n"
        new, action = apply_nudge(existing)
        assert action == "installed"
        assert new.startswith(existing.rstrip() + "\n") or existing.rstrip() in new
        assert START_MARKER in new
        assert new.endswith("\n")

    def test_is_idempotent_when_body_unchanged(self):
        once, _ = apply_nudge("# CLAUDE.md\n")
        twice, action = apply_nudge(once)
        assert action == "unchanged"
        assert twice == once

    def test_updates_when_body_changes(self):
        once, _ = apply_nudge("# CLAUDE.md\n", body="old body")
        twice, action = apply_nudge(once, body="new body")
        assert action == "updated"
        assert "new body" in twice
        assert "old body" not in twice

    def test_unchanged_when_only_trailing_whitespace_differs(self):
        """Whitespace-only differences must not trigger a spurious 'updated'."""
        canonical, _ = apply_nudge("# CLAUDE.md\n")
        # Add extra trailing newlines — same nudge body, different whitespace.
        with_extra_whitespace = canonical + "\n\n\n"
        result, action = apply_nudge(with_extra_whitespace)
        assert action == "unchanged"
        # Original bytes preserved — no normalization written back.
        assert result == with_extra_whitespace

    def test_no_mutation_of_input(self):
        existing = "# CLAUDE.md\n"
        original = str(existing)
        apply_nudge(existing)
        assert existing == original

    def test_single_marker_pair_after_multiple_installs(self):
        result = ""
        for _ in range(5):
            result, _ = apply_nudge(result)
        assert result.count(START_MARKER) == 1
        assert result.count(END_MARKER) == 1


class TestRemoveNudge:
    def test_removes_installed_block(self):
        installed, _ = apply_nudge("# CLAUDE.md\n\nHello.\n")
        removed, action = remove_nudge(installed)
        assert action == "removed"
        assert START_MARKER not in removed
        assert END_MARKER not in removed
        assert "Hello." in removed

    def test_absent_when_no_block(self):
        new, action = remove_nudge("# just a file\n")
        assert action == "absent"
        assert new == "# just a file\n"

    def test_removes_orphan_start_marker(self):
        existing = "# CLAUDE.md\n\n" + START_MARKER + "\nleftover\n"
        removed, action = remove_nudge(existing)
        assert action == "removed"
        assert START_MARKER not in removed
        assert "leftover" in removed

    def test_removes_orphan_end_marker(self):
        existing = "# CLAUDE.md\n\nleftover\n" + END_MARKER + "\n"
        removed, action = remove_nudge(existing)
        assert action == "removed"
        assert END_MARKER not in removed
        assert "leftover" in removed

    def test_remove_then_install_is_stable(self):
        installed, _ = apply_nudge("# CLAUDE.md\n")
        removed, _ = remove_nudge(installed)
        re_installed, _ = apply_nudge(removed)
        assert re_installed.count(START_MARKER) == 1


class TestRenderBlock:
    def test_wraps_body_in_markers(self):
        block = render_block("hello")
        assert block.startswith(START_MARKER)
        assert block.rstrip().endswith(END_MARKER)
        assert "hello" in block


class TestInstallIO:
    def test_install_creates_file(self, tmp_path: Path):
        target = tmp_path / "nested" / "CLAUDE.md"
        result = install(path=target)
        assert result.action == "installed"
        assert target.exists()
        assert START_MARKER in target.read_text()

    def test_install_is_idempotent(self, tmp_path: Path):
        target = tmp_path / "CLAUDE.md"
        install(path=target)
        result = install(path=target)
        assert result.action == "unchanged"

    def test_uninstall_removes_block(self, tmp_path: Path):
        target = tmp_path / "CLAUDE.md"
        target.write_text("# Existing\n\nContent.\n")
        install(path=target)
        result = uninstall(path=target)
        assert result.action == "removed"
        text = target.read_text()
        assert START_MARKER not in text
        assert "Existing" in text
        assert "Content." in text

    def test_uninstall_absent_when_file_missing(self, tmp_path: Path):
        target = tmp_path / "missing.md"
        result = uninstall(path=target)
        assert result.action == "absent"
        assert not target.exists()


class TestShouldAutoNudge:
    def test_default_is_true(self):
        assert should_auto_nudge({}) is True

    def test_opt_out_truthy_values(self):
        for val in ("1", "true", "yes", "on", "TRUE", "Yes"):
            assert should_auto_nudge({OPT_OUT_ENV: val}) is False, val

    def test_opt_out_falsy_values_still_nudge(self):
        for val in ("", "0", "false", "no"):
            assert should_auto_nudge({OPT_OUT_ENV: val}) is True, val


class TestEnsureNudge:
    def test_installs_on_fresh_file(self, tmp_path: Path):
        target = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        stream = io.StringIO()
        result = ensure_nudge(path=target, env={}, stream=stream, skill_path=skill)
        assert result.action == "installed"
        assert START_MARKER in target.read_text()
        assert skill.exists()
        assert skill.read_text(encoding="utf-8") == SKILL_MD
        notice = stream.getvalue()
        assert "advisor first-run setup" in notice
        assert "nudge" in notice
        assert "skill" in notice
        assert "Setup complete!" in notice

    def test_unchanged_when_already_installed(self, tmp_path: Path):
        target = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        install(path=target)
        install_skill(path=skill)
        stream = io.StringIO()
        result = ensure_nudge(path=target, env={}, stream=stream, skill_path=skill)
        assert result.action == "unchanged"
        assert stream.getvalue() == ""

    def test_opt_out_skips(self, tmp_path: Path):
        target = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        stream = io.StringIO()
        result = ensure_nudge(
            path=target,
            env={OPT_OUT_ENV: "1"},
            stream=stream,
            skill_path=skill,
        )
        assert result.action == "unchanged"
        assert not target.exists()
        assert not skill.exists()
        assert stream.getvalue() == ""

    def test_survives_unwritable_parent(self, tmp_path: Path):
        # Make a file where a directory would need to be, so mkdir raises.
        (tmp_path / "blocker").write_text("not a dir")
        target = tmp_path / "blocker" / "nested" / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        result = ensure_nudge(path=target, env={}, stream=io.StringIO(), skill_path=skill)
        assert result.action == "unchanged"
        assert not target.exists()
        # Skill install should still succeed even though nudge failed.
        assert skill.exists()

    def test_surface_error_via_result(self, tmp_path: Path):
        """Non-fatal write failures must be surfaced on the returned result
        so programmatic consumers can detect partial installs (audit 7.1)."""
        (tmp_path / "blocker").write_text("not a dir")
        target = tmp_path / "blocker" / "nested" / "CLAUDE.md"
        # Also break the skill target so both halves fail:
        skill = tmp_path / "blocker" / "skills" / "advisor" / "SKILL.md"
        stream = io.StringIO()
        result = ensure_nudge(path=target, env={}, stream=stream, skill_path=skill)
        # Warning is visible (not dim), not silently swallowed:
        out = stream.getvalue()
        assert "nudge write failed" in out or "skill write failed" in out
        # And surfaced on the result itself:
        assert result.error is not None
        assert "nudge write failed" in result.error or "skill write failed" in result.error

    def test_no_error_on_success(self, tmp_path: Path):
        target = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        result = ensure_nudge(path=target, env={}, stream=io.StringIO(), skill_path=skill)
        assert result.error is None


class TestInstallSkill:
    def test_fresh_install_writes_skill_file(self, tmp_path: Path):
        target = tmp_path / "skills" / "advisor" / "SKILL.md"
        result = install_skill(path=target)
        assert result.action == "installed"
        assert target.read_text(encoding="utf-8") == SKILL_MD

    def test_second_install_is_unchanged(self, tmp_path: Path):
        target = tmp_path / "skills" / "advisor" / "SKILL.md"
        install_skill(path=target)
        result = install_skill(path=target)
        assert result.action == "unchanged"

    def test_install_with_different_body_updates(self, tmp_path: Path):
        target = tmp_path / "skills" / "advisor" / "SKILL.md"
        install_skill(path=target, body="old body")
        result = install_skill(path=target)  # bundled SKILL_MD
        assert result.action == "updated"
        assert target.read_text(encoding="utf-8") == SKILL_MD

    def test_install_creates_parent_dirs(self, tmp_path: Path):
        target = tmp_path / "deep" / "nested" / "skills" / "advisor" / "SKILL.md"
        result = install_skill(path=target)
        assert result.action == "installed"
        assert target.exists()

    def test_uninstall_removes_file_and_empty_dir(self, tmp_path: Path):
        target = tmp_path / "skills" / "advisor" / "SKILL.md"
        install_skill(path=target)
        result = uninstall_skill(path=target)
        assert result.action == "removed"
        assert not target.exists()
        assert not target.parent.exists()  # parent advisor/ dir was empty, got cleaned
        assert target.parent.parent.exists()  # ~/.claude/skills/ itself is left alone

    def test_uninstall_absent_when_nothing_installed(self, tmp_path: Path):
        target = tmp_path / "skills" / "advisor" / "SKILL.md"
        result = uninstall_skill(path=target)
        assert result.action == "absent"

    def test_uninstall_preserves_non_empty_skill_dir(self, tmp_path: Path):
        skill_dir = tmp_path / "skills" / "advisor"
        target = skill_dir / "SKILL.md"
        install_skill(path=target)
        (skill_dir / "user_added.md").write_text("something the user added")
        result = uninstall_skill(path=target)
        assert result.action == "removed"
        assert not target.exists()
        assert skill_dir.exists()  # kept because user_added.md is still there

    @pytest.mark.skipif(sys.platform == "win32", reason="chmod not meaningful on Windows")
    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses POSIX permission bits so the unlink won't fail",
    )
    def test_uninstall_raises_oserror_on_permission_failure(self, tmp_path: Path):
        """OSError propagates when the file exists but cannot be deleted."""
        skill_dir = tmp_path / "skills" / "advisor"
        target = skill_dir / "SKILL.md"
        install_skill(path=target)
        # Remove write permission from parent dir so unlink() fails.
        skill_dir.chmod(0o500)
        try:
            with pytest.raises(OSError):
                uninstall_skill(path=target)
        finally:
            skill_dir.chmod(0o700)  # restore so tmp_path cleanup works


class TestStatus:
    def test_missing_when_nothing_installed(self, tmp_path: Path):
        nudge = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        s = status(nudge_path=nudge, skill_path=skill, env={})
        assert s.nudge.present is False
        assert s.nudge.current is False
        assert s.skill.present is False
        assert s.opt_out is False

    def test_present_and_current_after_install(self, tmp_path: Path):
        nudge = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        install(path=nudge)
        install_skill(path=skill)
        s = status(nudge_path=nudge, skill_path=skill, env={})
        assert s.nudge.present and s.nudge.current
        assert s.skill.present and s.skill.current

    def test_outdated_skill_detected(self, tmp_path: Path):
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        install_skill(path=skill, body="old body, not the bundled SKILL.md")
        s = status(nudge_path=tmp_path / "CLAUDE.md", skill_path=skill, env={})
        assert s.skill.present is True
        assert s.skill.current is False

    def test_outdated_nudge_detected(self, tmp_path: Path):
        nudge = tmp_path / "CLAUDE.md"
        install(path=nudge, body="legacy nudge body that no longer matches")
        s = status(nudge_path=nudge, skill_path=tmp_path / "missing", env={})
        assert s.nudge.present is True
        assert s.nudge.current is False

    def test_opt_out_reflected(self, tmp_path: Path):
        s = status(
            nudge_path=tmp_path / "CLAUDE.md",
            skill_path=tmp_path / "skill",
            env={OPT_OUT_ENV: "1"},
        )
        assert s.opt_out is True

    def test_writes_nothing(self, tmp_path: Path):
        nudge = tmp_path / "CLAUDE.md"
        skill = tmp_path / "skills" / "advisor" / "SKILL.md"
        status(nudge_path=nudge, skill_path=skill, env={})
        assert not nudge.exists()
        assert not skill.exists()


class TestAtomicWriteSymlinkHardening:
    """`_atomic_write_text` refuses to clobber a symlink target.

    Defense against a shared-host attack where a writable ``~/.claude``
    contains a symlink a malicious user points at, e.g., ``~/.bashrc``.
    We must not follow that symlink through ``os.replace``.
    """

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="creating symlinks requires elevated privilege or Developer Mode on Windows",
    )
    def test_refuses_symlink_target(self, tmp_path: Path):
        real = tmp_path / "real.txt"
        real.write_text("original\n")
        link = tmp_path / "link.txt"
        link.symlink_to(real)

        with pytest.raises(OSError, match="refusing to write through symlink"):
            _atomic_write_text(link, "payload\n")

        # Real file untouched — no follow-through happened.
        assert real.read_text() == "original\n"

    def test_writes_normally_when_target_is_not_a_symlink(self, tmp_path: Path):
        target = tmp_path / "plain.txt"
        _atomic_write_text(target, "hello\n")
        assert target.read_text() == "hello\n"
        assert not target.is_symlink()

    def test_chmod_failure_is_non_fatal(self, tmp_path: Path, monkeypatch):
        """On restricted filesystems ``os.chmod`` may refuse (Windows, FAT,
        certain container mounts). The write must still succeed — mode
        simply stays at the tempfile default (0o600).
        """
        import os as _os

        target = tmp_path / "out.txt"
        real_chmod = _os.chmod

        def picky(path, mode, *a, **kw):
            if str(path).endswith(".tmp"):
                raise OSError("read-only fs")
            return real_chmod(path, mode, *a, **kw)

        monkeypatch.setattr(_os, "chmod", picky)
        _atomic_write_text(target, "payload\n")
        assert target.read_text() == "payload\n"


class TestInstallSkillUpdateAndUninstall:
    """Cover the update / unchanged / uninstall-empty-parent paths of
    ``install_skill`` and ``uninstall_skill``.
    """

    def test_install_skill_is_unchanged_when_content_identical(self, tmp_path: Path):
        from advisor.install import SKILL_MD, install_skill

        target = tmp_path / "SKILL.md"
        # Windows default codepage is cp1252, which cannot encode the
        # arrow characters used in SKILL.md — force UTF-8 so this test
        # runs identically on every platform.
        target.write_text(SKILL_MD, encoding="utf-8")
        result = install_skill(path=target)
        assert result.action == "unchanged"

    def test_install_skill_updates_when_content_differs(self, tmp_path: Path):
        from advisor.install import install_skill

        target = tmp_path / "SKILL.md"
        target.write_text("stale\n")
        result = install_skill(path=target)
        assert result.action == "updated"

    def test_install_skill_handles_unreadable_existing_file(self, tmp_path: Path, monkeypatch):
        from advisor.install import install_skill

        target = tmp_path / "SKILL.md"
        target.write_text("whatever")
        real_read = Path.read_text

        def boom(self, *a, **kw):
            if self == target:
                raise OSError("denied")
            return real_read(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", boom)
        # Should not raise — treats as empty and overwrites
        result = install_skill(path=target)
        assert result.action == "updated"

    def test_uninstall_skill_removes_empty_parent_advisor_dir(self, tmp_path: Path):
        from advisor.install import SKILL_DIR_NAME, uninstall_skill

        parent = tmp_path / SKILL_DIR_NAME
        parent.mkdir()
        target = parent / "SKILL.md"
        target.write_text("x")
        result = uninstall_skill(path=target)
        assert result.action == "removed"
        assert not parent.exists(), "empty advisor/ parent should be cleaned up"

    def test_uninstall_skill_absent_returns_absent(self, tmp_path: Path):
        from advisor.install import uninstall_skill

        result = uninstall_skill(path=tmp_path / "does-not-exist.md")
        assert result.action == "absent"


class TestVersionBadge:
    """Covers the E12 version-badge mechanism (SKILL.md self-identification)."""

    def test_bundled_skill_contains_badge(self):
        from advisor.install import parse_badge
        from advisor.skill_asset import SKILL_MD, VERSION_BADGE

        assert VERSION_BADGE in SKILL_MD
        assert parse_badge(SKILL_MD) is not None

    def test_parse_badge_returns_none_for_unbadged_text(self):
        from advisor.install import parse_badge

        assert parse_badge("# just a markdown file") is None
        assert parse_badge("") is None

    def test_parse_badge_extracts_version(self):
        from advisor.install import parse_badge

        assert parse_badge("prefix\n<!-- advisor:1.2.3 -->\nsuffix") == "1.2.3"

    def test_get_installed_skill_version_reads_badge(self, tmp_path: Path):
        from advisor.install import get_installed_skill_version

        skill = tmp_path / "SKILL.md"
        skill.write_text("<!-- advisor:9.9.9 -->\nbody\n", encoding="utf-8")
        assert get_installed_skill_version(path=skill) == "9.9.9"

    def test_get_installed_skill_version_missing_file(self, tmp_path: Path):
        from advisor.install import get_installed_skill_version

        assert get_installed_skill_version(path=tmp_path / "nope.md") is None
