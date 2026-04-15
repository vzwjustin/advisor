"""Tests for advisor.install — nudge append/remove helpers."""

import io
from pathlib import Path

from advisor.install import (
    END_MARKER,
    OPT_OUT_ENV,
    START_MARKER,
    apply_nudge,
    ensure_nudge,
    install,
    remove_nudge,
    render_block,
    should_auto_nudge,
    uninstall,
)


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
        stream = io.StringIO()
        result = ensure_nudge(path=target, env={}, stream=stream)
        assert result.action == "installed"
        assert START_MARKER in target.read_text()
        assert "advisor: added nudge" in stream.getvalue()

    def test_unchanged_when_already_installed(self, tmp_path: Path):
        target = tmp_path / "CLAUDE.md"
        install(path=target)
        stream = io.StringIO()
        result = ensure_nudge(path=target, env={}, stream=stream)
        assert result.action == "unchanged"
        assert stream.getvalue() == ""

    def test_opt_out_skips(self, tmp_path: Path):
        target = tmp_path / "CLAUDE.md"
        stream = io.StringIO()
        result = ensure_nudge(
            path=target, env={OPT_OUT_ENV: "1"}, stream=stream
        )
        assert result.action == "unchanged"
        assert not target.exists()
        assert stream.getvalue() == ""

    def test_survives_unwritable_parent(self, tmp_path: Path):
        # Make a file where a directory would need to be, so mkdir raises.
        (tmp_path / "blocker").write_text("not a dir")
        target = tmp_path / "blocker" / "nested" / "CLAUDE.md"
        result = ensure_nudge(path=target, env={}, stream=io.StringIO())
        assert result.action == "unchanged"
        assert not target.exists()
