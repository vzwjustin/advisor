#!/usr/bin/env python3
"""SARIF ``fail-on`` gate used by the bundled GitHub Actions workflow.

Reads a SARIF 2.1.0 document and exits non-zero (status 4 — matches
``advisor audit --fail-on``'s ``_FAIL_ON_EXIT_CODE``) when any result
meets or exceeds the configured severity threshold. Lives in
``scripts/`` rather than the ``advisor`` package so the workflow gate
doesn't drag the whole package's import graph in for what is a 60-line
SARIF lint.

CLI is intentionally minimal — the workflow's ``run:`` step calls
``python scripts/sarif_gate.py advisor.sarif`` with the threshold in
the ``ADVISOR_FAIL_ON`` env var. A local user can run it the same way
to lint a SARIF artifact captured from CI.

Threshold semantics mirror ``advisor/__main__.py:_FAIL_ON_RANK`` so the
gate behaves identically to ``advisor audit --fail-on LEVEL``:

* ``never``    — never trips (default).
* ``low``      — trips on LOW / MEDIUM / HIGH / CRITICAL.
* ``medium``   — trips on MEDIUM / HIGH / CRITICAL.
* ``high``     — trips on HIGH / CRITICAL.
* ``critical`` — trips on CRITICAL only.

Severity source: prefers ``result.properties.severity`` (the original
advisor severity string, emitted by ``advisor/sarif.py``). Falls back
to the SARIF ``level`` field for third-party SARIF without that
property — that fallback is lossy because CRITICAL and HIGH both emit
SARIF ``error``, so ``--fail-on=critical`` cannot trip on third-party
results.

Defensive against hostile / malformed SARIF: every dict / list / string
narrow uses ``isinstance`` so a doc with ``runs`` as a dict, ``results``
as an int, ``properties`` as a scalar, or ``severity`` as ``42`` skips
that result instead of crashing the workflow with an AttributeError.
A SARIF that can't be JSON-parsed exits 2 with a clear ``::error::``
annotation.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Match advisor/__main__.py:_FAIL_ON_RANK exactly so this gate's
# threshold semantics agree with the audit subcommand.
RANK: dict[str, int] = {
    "never": 99,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

# Fallback for third-party SARIF that lacks ``properties.severity``:
# map the SARIF level back to an advisor rank. Lossy — CRITICAL and
# HIGH both emit SARIF ``error`` — so this branch cannot distinguish
# critical from high.
SARIF_LEVEL_TO_RANK: dict[str, int] = {
    "error": 3,
    "warning": 2,
    "note": 1,
    "none": 0,
}

GATE_TRIPPED_EXIT_CODE = 4
READ_ERROR_EXIT_CODE = 2


def _rank_for_result(result: dict[str, object]) -> int:
    """Compute the advisor-rank for a single SARIF result.

    Returns 0 when the result has no usable severity signal (worst-case
    "low impact" — a fail-on=low gate would still trip on this).
    """
    props = result.get("properties")
    sev_raw = props.get("severity") if isinstance(props, dict) else None
    sev = sev_raw.strip().lower() if isinstance(sev_raw, str) else ""
    if sev in RANK:
        return RANK[sev]
    level = result.get("level")
    return SARIF_LEVEL_TO_RANK.get(level if isinstance(level, str) else "warning", 0)


def evaluate(doc: object, threshold: str) -> tuple[bool, int]:
    """Walk a parsed SARIF doc and return ``(tripped, highest_rank_seen)``.

    Pure function — no I/O, no sys.exit. Lets the CLI shell wrap it
    and tests call it directly with synthetic docs.
    """
    gate = RANK.get(threshold.lower(), 99)
    if gate == 99:
        return False, 0
    highest = 0
    runs = doc.get("runs") if isinstance(doc, dict) else None
    if not isinstance(runs, list):
        return False, 0
    for run in runs:
        if not isinstance(run, dict):
            continue
        results = run.get("results")
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, dict):
                continue
            rank = _rank_for_result(result)
            if rank > highest:
                highest = rank
            if rank >= gate:
                return True, highest
    return False, highest


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    sarif_path = Path(args[0]) if args else Path("advisor.sarif")
    threshold = os.environ.get("ADVISOR_FAIL_ON", "never").lower()
    # Short-circuit before any I/O — a 'never' gate never opens the file.
    if RANK.get(threshold, 99) == 99:
        return 0
    try:
        with sarif_path.open(encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::error::advisor fail-on: cannot read {sarif_path}: {exc}", flush=True)
        return READ_ERROR_EXIT_CODE
    tripped, _highest = evaluate(doc, threshold)
    if tripped:
        print(
            f"::error::advisor fail-on={threshold} tripped: at least one "
            "finding at/above the threshold (see Code Scanning)",
            flush=True,
        )
        return GATE_TRIPPED_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
