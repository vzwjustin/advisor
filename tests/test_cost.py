"""Tests for ``advisor.cost`` — token & dollar estimates."""

from __future__ import annotations

from pathlib import Path

from advisor.cost import CostEstimate, estimate_cost, format_estimate, load_pricing
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


class TestLoadPricing:
    """``load_pricing`` parses both object and array shapes."""

    def test_object_shape(self, tmp_path: Path) -> None:
        import json

        p = tmp_path / "pricing.json"
        p.write_text(
            json.dumps(
                {
                    "opus": {"input": 2000, "output": 8000},
                    "sonnet": {"input": 400, "output": 2000},
                    "haiku": {"input": 120, "output": 600},
                }
            )
        )
        pricing = load_pricing(p)
        assert pricing == {
            "opus": (2000, 8000),
            "sonnet": (400, 2000),
            "haiku": (120, 600),
        }

    def test_array_shape(self, tmp_path: Path) -> None:
        import json

        p = tmp_path / "pricing.json"
        p.write_text(
            json.dumps(
                {
                    "opus": [1500, 7500],
                    "sonnet": [300, 1500],
                    "haiku": [100, 500],
                }
            )
        )
        pricing = load_pricing(p)
        assert pricing["opus"] == (1500, 7500)

    def test_missing_family_raises(self, tmp_path: Path) -> None:
        import json

        import pytest

        p = tmp_path / "pricing.json"
        p.write_text(json.dumps({"opus": [1, 1], "sonnet": [1, 1]}))
        with pytest.raises(ValueError, match="haiku"):
            load_pricing(p)

    def test_bad_json_raises(self, tmp_path: Path) -> None:
        import pytest

        p = tmp_path / "pricing.json"
        p.write_text("not json at all")
        with pytest.raises(ValueError, match="not valid JSON"):
            load_pricing(p)

    def test_negative_cents_raises(self, tmp_path: Path) -> None:
        import json

        import pytest

        p = tmp_path / "pricing.json"
        p.write_text(
            json.dumps(
                {
                    "opus": [-1, 1],
                    "sonnet": [1, 1],
                    "haiku": [1, 1],
                }
            )
        )
        with pytest.raises(ValueError, match="non-negative"):
            load_pricing(p)

    def test_pricing_threads_into_estimate(self, tmp_path: Path) -> None:
        import json

        (tmp_path / "f.py").write_text("x" * 2000)
        pricing = {
            "opus": (1, 1),
            "sonnet": (1, 1),
            "haiku": (1, 1),
        }
        cheap = estimate_cost(
            [_task(str(tmp_path / "f.py"))],
            None,
            advisor_model="opus",
            runner_model="opus",
            max_fixes_per_runner=5,
            pricing=pricing,
        )
        # Also check the file-loading round trip produces the same estimate.
        file_path = tmp_path / "pricing.json"
        file_path.write_text(
            json.dumps(
                {
                    "opus": list(pricing["opus"]),
                    "sonnet": list(pricing["sonnet"]),
                    "haiku": list(pricing["haiku"]),
                }
            )
        )
        loaded = load_pricing(file_path)
        same = estimate_cost(
            [_task(str(tmp_path / "f.py"))],
            None,
            advisor_model="opus",
            runner_model="opus",
            max_fixes_per_runner=5,
            pricing=loaded,
        )
        assert cheap.cost_usd_max == same.cost_usd_max
