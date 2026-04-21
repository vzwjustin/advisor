"""Token and cost estimates for a planned advisor run.

Rough first-order model. Real cost depends on how much dialogue the advisor
has with runners, so we surface a *range* anchored to minimum (explore pass
only) and maximum (explore + up to ``max_fixes_per_runner`` fix waves per
runner) scenarios.

Token math (per million tokens) uses published public pricing as of 2025-04.
Users can override via ``cost_cents_per_mtok`` if pricing changes. Prices are
advisory — treat the output as "order of magnitude", not invoice-accurate.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from .focus import FocusBatch, FocusTask

# Default published pricing (USD per million tokens) as of 2025 April.
# These are intentionally encoded as cents to avoid float drift during
# aggregation. Override via ``price_override=`` if Anthropic changes list price.
DEFAULT_PRICING_CENTS_PER_MTOK: dict[str, tuple[int, int]] = {
    # family → (input_cents_per_mtok, output_cents_per_mtok)
    "opus": (1500, 7500),
    "sonnet": (300, 1500),
    "haiku": (100, 500),
}

# Fixed prompt overhead per dispatch (advisor prompt + runner prompt +
# message framing). Empirically ~3-6k tokens each; use a conservative middle.
ADVISOR_SYSTEM_TOKENS = 4_500
RUNNER_SYSTEM_TOKENS = 2_000
PER_MESSAGE_OVERHEAD_TOKENS = 300

# Very rough character → token ratio for the content being read.
# English prose: ~4 chars/token. Source code: ~3.5 chars/token (more symbols).
CHARS_PER_TOKEN = 3.5


def _family_of(model: str) -> str:
    """Return the canonical family (``opus``, ``sonnet``, ``haiku``) for a model name.

    Unknown names fall back to ``sonnet`` pricing as a reasonable
    middle-of-the-road default, but emit a one-shot :class:`UserWarning`
    so the caller knows their estimate is a guess. Warning is
    de-duplicated per unique model string so a plan with many runners
    doesn't spam stderr.
    """
    m = model.lower()
    if "opus" in m:
        return "opus"
    if "sonnet" in m:
        return "sonnet"
    if "haiku" in m:
        return "haiku"
    _warn_unknown_family(model)
    return "sonnet"


@cache
def _warn_unknown_family(model: str) -> None:
    """Issue a once-per-model warning for unclassifiable model names.

    ``cache`` dedupes so repeated calls for the same model during a
    single ``estimate_cost`` invocation (advisor + N runners of the same
    family) emit exactly one line.
    """
    warnings.warn(
        f"cost: unknown model family for {model!r}; pricing as 'sonnet' — "
        f"pass `pricing=` to override",
        UserWarning,
        stacklevel=3,
    )


def _tokens_for_file(path: str) -> int:
    """Best-effort token estimate for a file's content (single Read call)."""
    try:
        size = Path(path).stat().st_size
    except OSError:
        return 0
    return int(size / CHARS_PER_TOKEN)


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Token + USD range for a planned advisor run."""

    input_tokens_min: int
    input_tokens_max: int
    output_tokens_min: int
    output_tokens_max: int
    cost_usd_min: float
    cost_usd_max: float
    runner_count: int
    file_count: int
    advisor_model: str
    runner_model: str

    def to_dict(self) -> dict[str, object]:
        """Round-trippable dict for JSON output."""
        return {
            "runner_count": self.runner_count,
            "file_count": self.file_count,
            "advisor_model": self.advisor_model,
            "runner_model": self.runner_model,
            "input_tokens_min": self.input_tokens_min,
            "input_tokens_max": self.input_tokens_max,
            "output_tokens_min": self.output_tokens_min,
            "output_tokens_max": self.output_tokens_max,
            "cost_usd_min": round(self.cost_usd_min, 4),
            "cost_usd_max": round(self.cost_usd_max, 4),
        }


def estimate_cost(
    tasks: list[FocusTask],
    batches: list[FocusBatch] | None,
    *,
    advisor_model: str,
    runner_model: str,
    max_fixes_per_runner: int,
    pricing: dict[str, tuple[int, int]] | None = None,
) -> CostEstimate:
    """Estimate token usage + USD cost for a planned run.

    * **min** scenario — advisor does discovery + one explore pass; runners
      read each file once; no fix waves.
    * **max** scenario — every runner consumes its full ``max_fixes_per_runner``
      budget; each fix is a full read + write round trip.
    """
    pricing = pricing or DEFAULT_PRICING_CENTS_PER_MTOK
    runner_count = len(batches) if batches else min(5, len(tasks)) or 1
    file_count = len(tasks)

    # Sum of per-file read tokens (runners read every file once during explore).
    content_tokens = sum(_tokens_for_file(t.file_path) for t in tasks)

    # ---- MIN: one explore pass, no fixes ----
    advisor_in_min = ADVISOR_SYSTEM_TOKENS + (PER_MESSAGE_OVERHEAD_TOKENS * runner_count * 2)
    runner_in_min = (
        runner_count * RUNNER_SYSTEM_TOKENS
        + content_tokens
        + runner_count * PER_MESSAGE_OVERHEAD_TOKENS
    )
    advisor_out_min = runner_count * 400  # short dispatches + final report
    runner_out_min = runner_count * 800  # findings block per runner

    # ---- MAX: max_fixes_per_runner full edit rounds per runner ----
    fix_rounds = max(0, max_fixes_per_runner) * runner_count
    advisor_in_max = advisor_in_min + fix_rounds * PER_MESSAGE_OVERHEAD_TOKENS * 2
    # Each fix round: runner re-reads file (~2k avg) + writes back a patch
    avg_file_tokens = content_tokens // max(1, file_count)
    runner_in_max = runner_in_min + fix_rounds * (avg_file_tokens + PER_MESSAGE_OVERHEAD_TOKENS)
    advisor_out_max = advisor_out_min + fix_rounds * 200
    runner_out_max = runner_out_min + fix_rounds * 600

    # ---- Pricing ----
    adv_fam = _family_of(advisor_model)
    run_fam = _family_of(runner_model)
    adv_in_c, adv_out_c = pricing.get(adv_fam, pricing["sonnet"])
    run_in_c, run_out_c = pricing.get(run_fam, pricing["sonnet"])

    def _dollars(input_tokens: int, output_tokens: int, in_c: int, out_c: int) -> float:
        return (input_tokens * in_c + output_tokens * out_c) / 1_000_000 / 100

    cost_min = _dollars(advisor_in_min, advisor_out_min, adv_in_c, adv_out_c) + _dollars(
        runner_in_min, runner_out_min, run_in_c, run_out_c
    )
    cost_max = _dollars(advisor_in_max, advisor_out_max, adv_in_c, adv_out_c) + _dollars(
        runner_in_max, runner_out_max, run_in_c, run_out_c
    )

    return CostEstimate(
        input_tokens_min=advisor_in_min + runner_in_min,
        input_tokens_max=advisor_in_max + runner_in_max,
        output_tokens_min=advisor_out_min + runner_out_min,
        output_tokens_max=advisor_out_max + runner_out_max,
        cost_usd_min=cost_min,
        cost_usd_max=cost_max,
        runner_count=runner_count,
        file_count=file_count,
        advisor_model=advisor_model,
        runner_model=runner_model,
    )


def load_pricing(path: str | Path) -> dict[str, tuple[int, int]]:
    """Load a pricing override table from a JSON file.

    Accepts two equivalent shapes so hand-authored files stay readable:

    * ``{"opus": {"input": 1500, "output": 7500}, ...}`` (preferred)
    * ``{"opus": [1500, 7500], ...}`` (matches the in-memory tuple layout)

    Values are in cents per million tokens (same unit as
    :data:`DEFAULT_PRICING_CENTS_PER_MTOK`). All three canonical families
    (``opus``, ``sonnet``, ``haiku``) must be present so unknown-family
    fallbacks have something to land on. Raises :class:`ValueError` with
    an actionable message for any parse or shape error.
    """
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"could not read pricing file {p}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"pricing file {p} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"pricing file {p} must be a JSON object at the top level")
    out: dict[str, tuple[int, int]] = {}
    for family in ("opus", "sonnet", "haiku"):
        if family not in raw:
            raise ValueError(
                f"pricing file {p} is missing family {family!r}; "
                f"all three of opus/sonnet/haiku must be present"
            )
        entry = raw[family]
        if isinstance(entry, dict):
            try:
                in_c = int(entry["input"])
                out_c = int(entry["output"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(
                    f"pricing file {p}: family {family!r} object must have "
                    f"integer 'input' and 'output' keys"
                ) from exc
        elif isinstance(entry, list) and len(entry) == 2:
            try:
                in_c = int(entry[0])
                out_c = int(entry[1])
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"pricing file {p}: family {family!r} array must contain two integers"
                ) from exc
        else:
            raise ValueError(
                f"pricing file {p}: family {family!r} must be "
                f"an object {{input, output}} or a [input, output] array"
            )
        if in_c < 0 or out_c < 0:
            raise ValueError(
                f"pricing file {p}: family {family!r} cents values must be non-negative"
            )
        out[family] = (in_c, out_c)
    return out


def format_estimate(est: CostEstimate) -> str:
    """Human-readable summary of a :class:`CostEstimate`."""
    return (
        f"## Cost estimate\n\n"
        f"- Files: {est.file_count}\n"
        f"- Runners: {est.runner_count}\n"
        f"- Models: advisor={est.advisor_model}, runners={est.runner_model}\n"
        f"- Input tokens: {est.input_tokens_min:,} – {est.input_tokens_max:,}\n"
        f"- Output tokens: {est.output_tokens_min:,} – {est.output_tokens_max:,}\n"
        f"- **Est. cost: ${est.cost_usd_min:.2f} – ${est.cost_usd_max:.2f}**\n"
        f"\n_Range covers review-only (min) to full fix waves (max). "
        f"Actual cost depends on dialogue depth and fix count._"
    )
