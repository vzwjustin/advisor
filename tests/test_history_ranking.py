"""Tests for history-informed ranking (Phase 2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from advisor.history import (
    HistoryEntry,
    append_entries,
    entry_now,
    file_repeat_counts,
    file_repeat_scores,
    history_path,
    load_recent_findings,
    new_run_id,
)
from advisor.rank import rank_files

UTC = timezone.utc


def _entry(
    file_path: str,
    severity: str = "HIGH",
    age_days: float = 1.0,
    status: str = "CONFIRMED",
) -> HistoryEntry:
    ts = (datetime.now(UTC) - timedelta(days=age_days)).isoformat(timespec="seconds")
    return HistoryEntry(
        timestamp=ts,
        file_path=file_path,
        severity=severity,
        description="issue",
        status=status,
        run_id="test-run",
    )


class TestLoadRecentFindings:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_recent_findings(tmp_path / "absent.jsonl") == []

    def test_newest_first(self, tmp_path: Path) -> None:
        append_entries(tmp_path, [_entry("a.py", age_days=10)])
        append_entries(tmp_path, [_entry("b.py", age_days=1)])
        result = load_recent_findings(history_path(tmp_path), limit=10)
        # Newest (b.py, 1 day old) should come first.
        assert result[0].file_path == "b.py"
        assert result[1].file_path == "a.py"

    def test_limit_bounds_result(self, tmp_path: Path) -> None:
        entries = [_entry(f"f{i}.py", age_days=i) for i in range(10)]
        append_entries(tmp_path, entries)
        result = load_recent_findings(history_path(tmp_path), limit=3)
        assert len(result) == 3

    def test_zero_limit(self, tmp_path: Path) -> None:
        append_entries(tmp_path, [_entry("a.py")])
        assert load_recent_findings(history_path(tmp_path), limit=0) == []

    def test_tolerates_malformed_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "history.jsonl"
        p.write_text(
            '{"garbage": true}\n{\n{"timestamp": "x", "file_path": "a.py", "severity": "LOW", "description": "d", "status": "CONFIRMED", "run_id": "r"}\n'
        )
        with pytest.warns(UserWarning):
            result = load_recent_findings(p, limit=5)
        assert len(result) == 1
        assert result[0].file_path == "a.py"


class TestFileRepeatScores:
    def test_single_recent_critical_scores_high(self) -> None:
        scores = file_repeat_scores([_entry("hot.py", severity="CRITICAL", age_days=1)])
        assert scores["hot.py"] > 1.5

    def test_decay_over_time(self) -> None:
        scores = file_repeat_scores([_entry("old.py", severity="CRITICAL", age_days=200)])
        # 200 days with 30-day half-life = ~0.01 of original weight
        assert scores.get("old.py", 0.0) < 0.1

    def test_rejected_entries_ignored(self) -> None:
        scores = file_repeat_scores(
            [_entry("x.py", severity="CRITICAL", status="REJECTED", age_days=1)]
        )
        assert scores == {}

    def test_multiple_findings_accumulate(self) -> None:
        entries = [
            _entry("x.py", severity="HIGH", age_days=1),
            _entry("x.py", severity="HIGH", age_days=2),
            _entry("x.py", severity="MEDIUM", age_days=3),
        ]
        scores = file_repeat_scores(entries)
        single = file_repeat_scores([_entry("x.py", severity="HIGH", age_days=1)])
        assert scores["x.py"] > single["x.py"]

    def test_invalid_half_life_raises(self) -> None:
        with pytest.raises(ValueError):
            file_repeat_scores([_entry("x.py")], half_life_days=0)


class TestFileRepeatCounts:
    def test_counts_within_window(self) -> None:
        entries = [
            _entry("x.py", age_days=1),
            _entry("x.py", age_days=50),
            _entry("x.py", age_days=150),  # outside 90d window
        ]
        counts = file_repeat_counts(entries, window_days=90.0)
        assert counts["x.py"] == 2


class TestRankFilesHistoryBoost:
    def test_no_history_preserves_baseline(self, tmp_path: Path) -> None:
        f = tmp_path / "util.py"
        f.write_text("# a utility module\n")
        ranked_no = rank_files([str(f)], read_fn=lambda p: Path(p).read_text())
        ranked_with = rank_files(
            [str(f)],
            read_fn=lambda p: Path(p).read_text(),
            history_scores=None,
        )
        assert ranked_no[0].priority == ranked_with[0].priority
        assert ranked_no[0].reasons == ranked_with[0].reasons

    def test_high_history_score_bumps_one_tier(self, tmp_path: Path) -> None:
        f = tmp_path / "util.py"
        f.write_text("# nothing special\n")
        baseline = rank_files([str(f)], read_fn=lambda p: Path(p).read_text())
        boosted = rank_files(
            [str(f)],
            read_fn=lambda p: Path(p).read_text(),
            history_scores={str(f): 5.0},
        )
        assert boosted[0].priority == min(5, baseline[0].priority + 1)
        assert any("repeat offender" in r for r in boosted[0].reasons)

    def test_cap_enforced_at_plus_one(self, tmp_path: Path) -> None:
        # A P3 file should become P4 regardless of how extreme the score.
        f = tmp_path / "handler.py"
        f.write_text("def handler(request):\n    return request.form\n")
        baseline = rank_files([str(f)], read_fn=lambda p: Path(p).read_text())
        boosted = rank_files(
            [str(f)],
            read_fn=lambda p: Path(p).read_text(),
            history_scores={str(f): 9999.0},
        )
        # Priority should not leap more than one tier.
        assert boosted[0].priority - baseline[0].priority <= 1

    def test_low_score_does_not_boost(self, tmp_path: Path) -> None:
        f = tmp_path / "util.py"
        f.write_text("# a utility\n")
        baseline = rank_files([str(f)], read_fn=lambda p: Path(p).read_text())
        result = rank_files(
            [str(f)],
            read_fn=lambda p: Path(p).read_text(),
            history_scores={str(f): 0.5},  # below threshold
        )
        assert result[0].priority == baseline[0].priority
        assert result[0].reasons == baseline[0].reasons

    def test_old_finding_doesnt_boost(self, tmp_path: Path) -> None:
        # An aged-out CRITICAL shouldn't meet the boost threshold.
        entries = [_entry("x.py", severity="CRITICAL", age_days=200)]
        scores = file_repeat_scores(entries)
        f = tmp_path / "x.py"
        f.write_text("# stub\n")
        baseline = rank_files([str(f)], read_fn=lambda p: Path(p).read_text())
        result = rank_files(
            [str(f)],
            read_fn=lambda p: Path(p).read_text(),
            history_scores={"x.py": scores.get("x.py", 0.0)},
        )
        assert result[0].priority == baseline[0].priority

    def test_reasons_include_count_when_provided(self, tmp_path: Path) -> None:
        f = tmp_path / "util.py"
        f.write_text("# stub\n")
        boosted = rank_files(
            [str(f)],
            read_fn=lambda p: Path(p).read_text(),
            history_scores={str(f): 5.0},
            history_counts={str(f): 3},
            history_window_days=90,
        )
        combined = " ".join(boosted[0].reasons)
        assert "3 findings in last 90d" in combined

    def test_repo_relative_history_key_matches_absolute_scan_path(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        f = src / "util.py"
        f.write_text("# stub\n")
        boosted = rank_files(
            [str(f)],
            read_fn=lambda p: Path(p).read_text(),
            history_scores={"src/util.py": 5.0},
            history_counts={"src/util.py": 2},
        )

        assert boosted[0].priority == 2
        combined = " ".join(boosted[0].reasons)
        assert "repeat offender" in combined
        assert "2 findings in last 90d" in combined

    def test_basename_history_key_does_not_boost_absolute_siblings(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        tests = tmp_path / "tests"
        src.mkdir()
        tests.mkdir()
        first = src / "util.py"
        second = tests / "util.py"
        first.write_text("# stub\n")
        second.write_text("# stub\n")

        ranked = rank_files(
            [str(first), str(second)],
            read_fn=lambda p: Path(p).read_text(),
            history_scores={"util.py": 5.0},
        )

        assert all("repeat offender" not in r.reasons for r in ranked)


@settings(
    deadline=1500, max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    scores=st.dictionaries(
        keys=st.text(min_size=1, max_size=20).filter(lambda s: "/" not in s and "\\" not in s),
        values=st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        max_size=10,
    ),
)
def test_fuzz_random_history_never_raises(tmp_path: Path, scores: dict[str, float]) -> None:
    f = tmp_path / "a.py"
    f.write_text("# stub\n")
    # Must not raise for any history_scores shape.
    ranked = rank_files([str(f)], read_fn=lambda p: Path(p).read_text(), history_scores=scores)
    assert 1 <= ranked[0].priority <= 5


class TestCliNoHistory:
    def test_flag_disables_boost(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import os
        import subprocess
        import sys

        target = tmp_path / "proj"
        target.mkdir()
        (target / "util.py").write_text("# stub\n")
        # Write a history entry that would boost util.py.
        append_entries(
            target,
            [
                entry_now(
                    file_path="util.py",
                    severity="CRITICAL",
                    description="test",
                    status="CONFIRMED",
                    run_id=new_run_id(),
                ),
                entry_now(
                    file_path="util.py",
                    severity="CRITICAL",
                    description="test2",
                    status="CONFIRMED",
                    run_id=new_run_id(),
                ),
            ],
        )
        env = {**os.environ, "ADVISOR_NO_NUDGE": "1", "NO_COLOR": "1"}
        # With history — expect repeat offender token.
        result = subprocess.run(
            [sys.executable, "-m", "advisor", "plan", str(target), "--min-priority", "1", "--json"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        # With --no-history — expect no boost.
        result2 = subprocess.run(
            [
                sys.executable,
                "-m",
                "advisor",
                "plan",
                str(target),
                "--min-priority",
                "1",
                "--no-history",
                "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result2.returncode == 0
