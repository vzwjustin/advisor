"""Tests for advisor.audit module."""

from __future__ import annotations

import json

from advisor import (
    audit_to_dict,
    audit_transcript,
    format_audit_report,
)
from advisor.checkpoint import Checkpoint, save_checkpoint
from advisor.focus import FocusBatch, FocusTask


def _mk_checkpoint(
    run_id: str = "2026-04-21T00-00-00Z",
    *,
    max_fixes_per_runner: int = 5,
    large_file_line_threshold: int = 800,
    large_file_max_fixes: int = 3,
    batch_files: list[list[str]] | None = None,
) -> Checkpoint:
    """Build a Checkpoint for direct consumption by audit_transcript.

    ``batch_files`` is a list of per-batch file lists; batch ids are
    assigned 1..N in order. When None, a single batch of two files is
    created.
    """
    if batch_files is None:
        batch_files = [["auth.py", "session.py"]]
    batches: list[dict[str, object]] = []
    for i, files in enumerate(batch_files, 1):
        batches.append(
            {
                "batch_id": i,
                "complexity": "medium",
                "top_priority": 3,
                "tasks": [{"file_path": fp, "priority": 3, "prompt": ""} for fp in files],
            }
        )
    all_tasks: list[dict[str, object]] = []
    for files in batch_files:
        all_tasks.extend({"file_path": fp, "priority": 3, "prompt": ""} for fp in files)

    return Checkpoint(
        run_id=run_id,
        created_at="2026-04-21T00:00:00+00:00",
        target=".",
        team_name="review",
        file_types="*.py",
        min_priority=3,
        max_runners=5,
        advisor_model="opus",
        runner_model="sonnet",
        max_fixes_per_runner=max_fixes_per_runner,
        large_file_line_threshold=large_file_line_threshold,
        large_file_max_fixes=large_file_max_fixes,
        test_command="",
        context="",
        tasks=all_tasks,
        batches=batches,
    )


class TestAuditTranscriptFixCounts:
    def test_counts_fix_assignments_per_runner(self):
        transcript = """
        SendMessage(to='runner-1', message='## Fix assignment (fix 1 of 5)')
        SendMessage(to='runner-1', message='## Fix assignment (fix 2 of 5)')
        SendMessage(to='runner-2', message='## Fix assignment (fix 1 of 5)')
        """
        report = audit_transcript(transcript, _mk_checkpoint())
        assert report.fix_counts == {"runner-1": 2, "runner-2": 1}
        assert report.fix_numbers == {"runner-1": [1, 2], "runner-2": [1]}

    def test_unattributed_fix_lands_under_question_runner(self):
        """A fix assignment without a nearby runner mention is still counted."""
        # No runner mention within the attribution window before the header.
        transcript = "garbage " * 200 + "\n## Fix assignment (fix 1 of 5)\n"
        report = audit_transcript(transcript, _mk_checkpoint())
        assert "runner-?" in report.fix_counts

    def test_last_fix_wrapper_parses(self):
        """The LAST FIX banner form must be recognized too."""
        transcript = (
            "SendMessage(to='runner-1', message='## Fix assignment (**LAST FIX** (5 of 5)).'))"
        )
        report = audit_transcript(transcript, _mk_checkpoint())
        assert report.fix_counts["runner-1"] == 1


class TestAuditTranscriptCapOverruns:
    def test_flags_overrun(self):
        # 6 fixes to runner-1, cap is 5 ⇒ overrun.
        transcript = "\n".join(
            f"SendMessage(to='runner-1', message='## Fix assignment (fix {i} of 5)')"
            for i in range(1, 7)
        )
        report = audit_transcript(transcript, _mk_checkpoint(max_fixes_per_runner=5))
        assert len(report.cap_overruns) == 1
        assert "runner-1" in report.cap_overruns[0]
        assert "6" in report.cap_overruns[0]

    def test_no_overrun_when_within_cap(self):
        transcript = (
            "SendMessage(to='runner-1', message='## Fix assignment (fix 1 of 5)')\n"
            "SendMessage(to='runner-1', message='## Fix assignment (fix 2 of 5)')\n"
        )
        report = audit_transcript(transcript, _mk_checkpoint(max_fixes_per_runner=5))
        assert report.cap_overruns == []


class TestAuditTranscriptContextPressure:
    def test_counts_and_attributes_pings(self):
        transcript = """
        SendMessage(to='advisor', message='runner-1 CONTEXT_PRESSURE — 4 fixes deep')
        SendMessage(to='advisor', message='runner-2 CONTEXT_PRESSURE — 4 fixes')
        SendMessage(to='advisor', message='runner-1 CONTEXT_PRESSURE again')
        """
        report = audit_transcript(transcript, _mk_checkpoint())
        # First-mention order preserved; duplicates collapsed for the runner list.
        assert report.context_pressure_runners == ["runner-1", "runner-2"]
        assert report.context_pressure_count == 3

    def test_no_pings_when_silent(self):
        report = audit_transcript("nothing interesting here", _mk_checkpoint())
        assert report.context_pressure_runners == []
        assert report.context_pressure_count == 0


class TestAuditTranscriptRotations:
    def test_counts_handoff_headers(self):
        transcript = """
        ## Handoff from runner-1
        ...brief...
        ## Handoff from runner-2
        ...brief...
        """
        report = audit_transcript(transcript, _mk_checkpoint())
        assert report.rotations == 2

    def test_zero_when_no_handoffs(self):
        report = audit_transcript("pure conversation no rotations", _mk_checkpoint())
        assert report.rotations == 0


class TestAuditTranscriptProtocolViolations:
    def test_captures_exact_violation_lines(self):
        transcript = (
            "reasoning...\n"
            "PROTOCOL_VIOLATION: runner-2 at cap=5, fix #6 queued for auth.py\n"
            "more reasoning...\n"
            "PROTOCOL_VIOLATION: about to dispatch out-of-batch fix\n"
        )
        report = audit_transcript(transcript, _mk_checkpoint())
        assert len(report.protocol_violations) == 2
        assert "runner-2 at cap=5" in report.protocol_violations[0]
        assert "out-of-batch" in report.protocol_violations[1]


class TestAuditTranscriptScopeDrift:
    def test_flags_out_of_batch_finding(self):
        transcript = """### Finding 1
- **File**: auth.py
- **Severity**: HIGH
- **Description**: hardcoded token
- **Evidence**: line 10
- **Fix**: read env var

### Finding 2
- **File**: crypto.py
- **Severity**: MEDIUM
- **Description**: weak hash
- **Evidence**: md5 usage
- **Fix**: use sha256
"""
        cp = _mk_checkpoint(batch_files=[["auth.py", "session.py"]])
        report = audit_transcript(transcript, cp)
        assert [f.file_path for f in report.findings_in_batch] == ["auth.py"]
        assert [f.file_path for f in report.findings_out_of_batch] == ["crypto.py"]
        assert report.batch_file_count == 2

    def test_no_batches_falls_back_to_tasks(self):
        """A checkpoint with no batches should still use its task list for scope."""
        cp = Checkpoint(
            run_id="r",
            created_at="t",
            target=".",
            team_name="review",
            file_types="*.py",
            min_priority=3,
            max_runners=5,
            advisor_model="opus",
            runner_model="sonnet",
            max_fixes_per_runner=5,
            large_file_line_threshold=800,
            large_file_max_fixes=3,
            test_command="",
            context="",
            tasks=[{"file_path": "only.py", "priority": 3, "prompt": ""}],
            batches=[],
        )
        transcript = """### Finding 1
- **File**: only.py
- **Severity**: HIGH
- **Description**: d
- **Evidence**: e
- **Fix**: f

### Finding 2
- **File**: other.py
- **Severity**: LOW
- **Description**: d2
- **Evidence**: e2
- **Fix**: f2
"""
        report = audit_transcript(transcript, cp)
        assert [f.file_path for f in report.findings_in_batch] == ["only.py"]
        assert [f.file_path for f in report.findings_out_of_batch] == ["other.py"]


class TestAuditToDict:
    def test_round_trips_through_json(self):
        """audit_to_dict output must be json.dumps-clean."""
        transcript = (
            "SendMessage(to='runner-1', message='## Fix assignment (fix 1 of 5)')\n"
            "PROTOCOL_VIOLATION: test case\n"
        )
        report = audit_transcript(transcript, _mk_checkpoint(run_id="abc"))
        payload = audit_to_dict(report)
        # round-trip through JSON to confirm no bespoke types leak in.
        rehydrated = json.loads(json.dumps(payload))
        assert rehydrated["run_id"] == "abc"
        assert rehydrated["caps"]["max_fixes_per_runner"] == 5
        assert rehydrated["fix_counts"]["runner-1"] == 1
        assert rehydrated["protocol_violations"][0].startswith("PROTOCOL_VIOLATION")

    def test_preserves_rule_id_in_finding_json(self):
        """audit --json is a handoff format for baseline/SARIF consumers."""
        report = audit_transcript(
            """### Finding 1
- **File**: auth.py
- **Severity**: HIGH
- **Description**: d
- **Evidence**: e
- **Fix**: f
- **Rule**: custom/rule
""",
            _mk_checkpoint(batch_files=[["auth.py"]]),
        )
        payload = audit_to_dict(report)
        assert payload["findings_in_batch"][0]["rule_id"] == "custom/rule"


class TestFormatAuditReport:
    def test_clean_run_renders_no_findings_sections_with_none(self):
        report = audit_transcript("", _mk_checkpoint())
        text = format_audit_report(report)
        assert "# Audit — run" in text
        assert "(none — every runner stayed within cap)" in text
        assert "(none — no runner self-reported saturation)" in text
        assert "(none)" in text  # PROTOCOL_VIOLATION section

    def test_drift_is_visible(self):
        transcript = """### Finding 1
- **File**: crypto.py
- **Severity**: HIGH
- **Description**: weak hash
- **Evidence**: md5
- **Fix**: sha256
"""
        cp = _mk_checkpoint(batch_files=[["auth.py"]])
        report = audit_transcript(transcript, cp)
        text = format_audit_report(report)
        assert "crypto.py" in text
        assert "scope drift" in text.lower()


class TestAuditCLI:
    """`advisor audit RUN_ID` smoke test — uses saved_checkpoint + stdin."""

    def test_cli_json_output_round_trip(self, tmp_path, capsys):
        from advisor.__main__ import main

        target = tmp_path
        # Save a real checkpoint via the same API production uses.
        save_checkpoint(
            target,
            run_id="test-run",
            tasks=[FocusTask(file_path="auth.py", priority=3, prompt="")],
            batches=[
                FocusBatch(
                    batch_id=1,
                    tasks=(FocusTask(file_path="auth.py", priority=3, prompt=""),),
                    complexity="medium",
                )
            ],
            team_name="review",
            file_types="*.py",
            min_priority=3,
            max_runners=5,
            advisor_model="opus",
            runner_model="sonnet",
        )

        transcript_file = tmp_path / "log.txt"
        transcript_file.write_text(
            "SendMessage(to='runner-1', message='## Fix assignment (fix 1 of 5)')\n"
            "PROTOCOL_VIOLATION: test\n",
            encoding="utf-8",
        )

        rc = main(
            [
                "audit",
                "test-run",
                str(target),
                "--transcript",
                str(transcript_file),
                "--json",
            ]
        )
        assert rc == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["run_id"] == "test-run"
        assert payload["fix_counts"]["runner-1"] == 1
        assert len(payload["protocol_violations"]) == 1

    def test_cli_missing_checkpoint_returns_2(self, tmp_path, capsys):
        from advisor.__main__ import main

        rc = main(
            [
                "audit",
                "missing-run",
                str(tmp_path),
                "--transcript",
                str(tmp_path / "nonexistent.txt"),
            ]
        )
        # Missing checkpoint errors before stdin is read.
        assert rc == 2
