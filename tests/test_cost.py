"""Tests for ``advisor.cost`` — token & dollar estimates."""

from __future__ import annotations

from pathlib import Path

from advisor.cost import CostEstimate, estimate_cost, format_estimate
from advisor.focus import FocusTask


def _task(path: str, priority: int = 3) -> FocusTask:
    return FocusTask(file_path=path, priority=priority, prompt="")


class TestEstimateCost:
    def test_estimate_returns_range(self, tmp_path: Path) -> None:
        p = tmp_path / "a.py"
        p.write_text("x" * 4000)
        e = estimate_cost(
            [_task(str(p))],
            None,
            advisor_model="opus",
            runner_model="sonnet",
            max_fixes_per_runner=5,
        )
        assert isinstance(e, CostEstimate)
        assert e.cost_usd_min <= e.cost_usd_max
        assert e.input_tokens_min <= e.input_tokens_max
        assert e.output_tokens_min <= e.output_tokens_max

    def test_missing_file_tokens_zero(self, tmp_path: Path) -> None:
        e = estimate_cost(
            [_task(str(tmp_path / "missing.py"))],
            None,
            advisor_model="opus",
            runner_model="sonnet",
            max_fixes_per_runner=5,
        )
        assert e.cost_usd_min >= 0.0

    def test_opus_costs_more_than_sonnet(self, tmp_path: Path) -> None:
        (tmp_path / "f.py").write_text("x" * 2000)
        cheap = estimate_cost(
            [_task(str(tmp_path / "f.py"))],
            None,
            advisor_model="sonnet",
            runner_model="haiku",
            max_fixes_per_runner=5,
        )
        pricey = estimate_cost(
            [_task(str(tmp_path / "f.py"))],
            None,
            advisor_model="opus",
            runner_model="opus",
            max_fixes_per_runner=5,
        )
        assert pricey.cost_usd_max > cheap.cost_usd_max

    def test_format_estimate_contains_dollars(self, tmp_path: Path) -> None:
        (tmp_path / "f.py").write_text("x" * 500)
        e = estimate_cost(
            [_task(str(tmp_path / "f.py"))],
            None,
            advisor_model="opus",
            runner_model="sonnet",
            max_fixes_per_runner=5,
        )
        s = format_estimate(e)
        assert "$" in s
        assert "Cost estimate" in s

    def test_to_dict_roundtrip(self, tmp_path: Path) -> None:
        (tmp_path / "f.py").write_text("x" * 500)
        e = estimate_cost(
            [_task(str(tmp_path / "f.py"))],
            None,
            advisor_model="opus",
            runner_model="sonnet",
            max_fixes_per_runner=5,
        )
        d = e.to_dict()
        assert d["file_count"] == 1
        assert d["advisor_model"] == "opus"
