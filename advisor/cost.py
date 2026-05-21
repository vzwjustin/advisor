"""Token and cost estimates for a planned advisor run.

Rough first-order model. Real cost depends on how much dialogue the advisor
has with runners, so we surface a *range* anchored to minimum (explore pass
only) and maximum (explore + up to ``max_fixes_per_runner`` fix waves per
runner) scenarios.

Token math (per million tokens) uses published public pricing as of 2025-04.
Users can override via ``cost_cents_per_mtok`` (or ship a JSON pricing file —
see :func:`load_pricing` and ``advisor plan --pricing FILE``) if pricing
changes; verify current rates at https://www.anthropic.com/pricing before
relying on the estimate. Prices are advisory — treat the output as "order
of magnitude", not invoice-accurate.
"""

from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass
from datetime import date
from functools import cache, lru_cache
from pathlib import Path

from ._fs import read_text_capped
from .focus import FocusBatch, FocusTask

#: Hard byte cap for ``--pricing FILE`` input. A pricing override is a small
#: three-family JSON object — under 1 KiB in practice. Capping the read at
#: 1 MiB closes the window where an accidentally-enormous (or hostile) file
#: buffers fully into memory before :func:`json.loads` ever sees it.
_PRICING_MAX_BYTES = 1_048_576

#: Snapshot date of :data:`DEFAULT_PRICING_CENTS_PER_MTOK`. Surfaced in
#: :func:`format_estimate` output and used to gate the stale-pricing
#: advisory in :func:`estimate_cost` (fires once per process when the
#: default table is more than 180 days old).
PRICING_AS_OF = date(2025, 4, 1)

# Default published pricing (USD per million tokens) snapshotted at
# :data:`PRICING_AS_OF`. Verify at https://www.anthropic.com/pricing before
# relying on the estimate. Intentionally encoded as cents to avoid float
# drift during aggregation. Override via ``pricing=`` if Anthropic changes
# list price.
DEFAULT_PRICING_CENTS_PER_MTOK: dict[str, tuple[int, int]] = {
    # family → (input_cents_per_mtok, output_cents_per_mtok)
    "opus": (1500, 7500),
    "sonnet": (300, 1500),
    "haiku": (100, 500),
}

#: Stale threshold (days) — defaults older than this trigger a one-shot
#: :class:`UserWarning` from :func:`estimate_cost` so users know to
#: refresh via ``advisor plan --pricing FILE`` or update the package.
_PRICING_STALE_DAYS = 180

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
        # Frames: warn → _warn_unknown_family → _family_of → estimate_cost
        # → user code. stacklevel=4 blames the user's estimate_cost(...) call.
        stacklevel=4,
    )


_stale_pricing_warned = False


def _maybe_warn_stale_default_pricing() -> None:
    """Emit a one-shot :class:`UserWarning` if the default pricing snapshot
    is older than :data:`_PRICING_STALE_DAYS`.

    Only called when ``pricing`` was left at its default in
    :func:`estimate_cost`. Mirrors the once-per-process suppression
    pattern of :func:`_warn_unknown_family` so a plan with many runners
    doesn't spam stderr. ``warnings.warn`` is used rather than direct
    stderr so callers can filter via ``-W`` or ``warnings.simplefilter``.
    """
    global _stale_pricing_warned
    if _stale_pricing_warned:
        return
    age_days = (date.today() - PRICING_AS_OF).days
    if age_days <= _PRICING_STALE_DAYS:
        return
    _stale_pricing_warned = True
    warnings.warn(
        f"cost: default pricing table is stale "
        f"(snapshot {PRICING_AS_OF.isoformat()}, {age_days} days old); "
        f"verify rates at https://www.anthropic.com/pricing and override "
        f"with `advisor plan --pricing FILE`",
        UserWarning,
        # Frames: warn → _maybe_warn_stale_default_pricing → estimate_cost
        # → user code. stacklevel=3 blames the user's estimate_cost(...) call.
        stacklevel=3,
    )


@lru_cache(maxsize=4096)
def _tokens_for_file_stat(path: str, size: int, mtime_ns: int) -> int:
    """Return cached token estimate for a specific file identity.

    ``mtime_ns`` participates in the key so changed files naturally invalidate
    stale estimates while repeated dashboard polls reuse the cheap arithmetic
    result for unchanged files.
    """
    del path, mtime_ns
    return int(size / CHARS_PER_TOKEN)


def _tokens_for_file(path: str) -> int:
    """Best-effort token estimate for a file's content (single stat call)."""
    try:
        stat = os.stat(path)
    except OSError:
        return 0
    return _tokens_for_file_stat(path, stat.st_size, stat.st_mtime_ns)


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
    max_runners: int | None = None,
    pricing: dict[str, tuple[int, int]] | None = None,
) -> CostEstimate:
    """Estimate token usage + USD cost for a planned run.

    * **min** scenario — advisor does discovery + one explore pass; runners
      read each file once; no fix waves.
    * **max** scenario — every runner consumes its full ``max_fixes_per_runner``
      budget; each fix is a full read + write round trip.
    """
    if pricing is None:
        # Default table in use — surface staleness if the snapshot has aged
        # past the threshold. Skipped entirely when the caller supplied a
        # ``pricing=`` override (they're already opted out of the default).
        _maybe_warn_stale_default_pricing()
        pricing = DEFAULT_PRICING_CENTS_PER_MTOK
    # Validate required keys before any KeyError-prone lookups below.
    # Custom ``pricing=`` dicts must cover the families we fall back to —
    # without this, a partial dict (e.g. {"opus": (...)} with no "sonnet")
    # raises KeyError mid-calculation rather than the clearer ValueError
    # the caller is expected to handle.
    missing = [k for k in ("sonnet", "opus", "haiku") if k not in pricing]
    if missing:
        raise ValueError(
            f"pricing= is missing required keys {missing!r}; "
            "supply entries for sonnet/opus/haiku or omit pricing= to use defaults"
        )
    # Validate value shape symmetrically with ``load_pricing``. Without
    # this, a malformed entry (e.g. ``{"opus": (1500,)}`` or
    # ``{"opus": "1500/7500"}``) would raise a cryptic ``ValueError: not
    # enough values to unpack`` deep inside the pricing lookup below
    # instead of a clear message at the input boundary.
    for fam, value in pricing.items():
        if not isinstance(value, tuple) or len(value) != 2:
            raise ValueError(
                f"pricing= entry for {fam!r} must be a 2-tuple "
                f"(input_cents_per_mtok, output_cents_per_mtok); got {value!r}"
            )
        in_c, out_c = value
        for label, cents in (("input", in_c), ("output", out_c)):
            if isinstance(cents, bool) or not isinstance(cents, int):
                raise ValueError(
                    f"pricing= entry for {fam!r} {label} cents must be an integer (got {cents!r})"
                )
            if cents < 0:
                raise ValueError(
                    f"pricing= entry for {fam!r} {label} cents must be non-negative (got {cents})"
                )
    # Reject negative caps explicitly. The CLI argparse and
    # ``default_team_config`` both floor at >=1 before reaching here, so
    # this guard only catches direct-API misuse — but a negative cap
    # would otherwise silently clamp via ``max(0, ...)`` at the
    # fix-rounds line, making the MAX scenario identical to MIN with
    # no indication of the misconfiguration.
    if max_fixes_per_runner < 0:
        raise ValueError(
            f"max_fixes_per_runner must be >= 0 (got {max_fixes_per_runner}); "
            "0 disables fix waves, negative is not meaningful"
        )
    # max_runners=0 is a real input (Opus-direct mode skips the pool) — let
    # it through so the estimate doesn't silently inflate to 1 runner. The
    # downstream MIN/MAX math is all multiplicative on runner_count, so a
    # zero count produces a plan that reflects an Opus-only pipeline.
    runner_limit = 5 if max_runners is None else max(0, max_runners)
    if batches:
        runner_count = len(batches)
    elif runner_limit == 0 or not tasks:
        runner_count = 0
    else:
        runner_count = min(runner_limit, len(tasks))
    file_count = len(tasks)

    # Sum of per-file read tokens (runners read every file once during explore).
    # De-dupe within an estimate so duplicated focus tasks don't repeat stats.
    token_cache: dict[str, int] = {}
    content_tokens = 0
    for task in tasks:
        if task.file_path not in token_cache:
            token_cache[task.file_path] = _tokens_for_file(task.file_path)
        content_tokens += token_cache[task.file_path]

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
        # Use ``read_text_capped``'s default ``utf-8-sig`` encoding so a BOM
        # prepended by Windows editors (Notepad, older VS Code variants) is
        # silently stripped instead of surfacing as a misleading "not valid
        # JSON" error — the file IS valid JSON, just BOM-prefixed, and the
        # helper was designed with that default specifically to absorb this.
        raw = json.loads(read_text_capped(p, _PRICING_MAX_BYTES))
    except ValueError as exc:
        # ``read_text_capped`` raises ValueError on oversize; ``json.loads``
        # raises ``JSONDecodeError`` which is a ValueError subclass. Both land
        # here. Disambiguate via the message so the user sees the right hint —
        # an oversize-file pointer is more actionable than a JSON parse error
        # mid-binary-blob.
        if isinstance(exc, json.JSONDecodeError):
            raise ValueError(f"pricing file {p} is not valid JSON: {exc}") from exc
        raise ValueError(
            f"pricing file {p} exceeds {_PRICING_MAX_BYTES}-byte cap; "
            f"shape it like `advisor plan --dump-pricing-template`"
        ) from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"could not read pricing file {p}: {exc}") from exc
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
                raw_in = entry["input"]
                raw_out = entry["output"]
            except KeyError as exc:
                raise ValueError(
                    f"pricing file {p}: family {family!r} object must have "
                    f"integer 'input' and 'output' keys"
                ) from exc
            # Reject non-integers explicitly. ``int(15.9)`` would silently
            # truncate to 15 — wrong at a config boundary where the
            # docstring promises integer cents-per-Mtok. ``isinstance(True,
            # int)`` is True in Python, so carve out ``bool`` first.
            for label, value in (("input", raw_in), ("output", raw_out)):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(
                        f"pricing file {p}: family {family!r} {label!r} "
                        f"must be an integer (got {value!r})"
                    )
            in_c = raw_in
            out_c = raw_out
        elif isinstance(entry, list) and len(entry) == 2:
            raw_in, raw_out = entry[0], entry[1]
            for label, value in (("input", raw_in), ("output", raw_out)):
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(
                        f"pricing file {p}: family {family!r} array {label!r} "
                        f"must be an integer (got {value!r})"
                    )
            in_c = raw_in
            out_c = raw_out
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
        f"Actual cost depends on dialogue depth and fix count. "
        f"Pricing as of {PRICING_AS_OF.isoformat()} — "
        f"verify at https://www.anthropic.com/pricing._"
    )
