"""Baseline snapshots — accept-current, flag-new findings lifecycle.

Adopting advisor on an existing codebase produces a long list of
pre-existing findings that the team has no bandwidth to fix right now.
Rather than drown future reviews in that backlog, the baseline feature
captures the current set of findings as *accepted* — subsequent runs
suppress anything matching the baseline and surface only net-new issues.

The baseline is a JSONL file (one finding-key per line) with a top-line
``schema_version`` record so future advisor releases can evolve the
format.

Identity key: ``(file_path, rule_id_or_synthesized, description_hash)``.
The description hash uses the first 120 chars so a trivial rewording of
the same underlying finding still matches. More aggressive matching
(e.g. line-number tolerance) is deliberately out of scope — baselines
should be tight enough that a genuine new finding surfaces.
"""

from __future__ import annotations

import hashlib
import json
import warnings
from dataclasses import dataclass
from pathlib import Path

from ._fs import atomic_write_text as _shared_atomic_write
from .sarif import synthesize_rule_id
from .verify import Finding

SCHEMA_VERSION = "1.0"

# Sentinel top-level record written on the first line so parsers can
# verify shape without decoding the full payload.
_HEADER_KEY = "__advisor_baseline__"


def _atomic_write_text(target: Path, text: str) -> None:
    """Atomic write helper — thin alias over :func:`advisor._fs.atomic_write_text`.

    Baseline files live under the user's own target directory and don't
    need the symlink-rejection or 0o644 chmod that the shared-host
    install path applies.
    """
    _shared_atomic_write(target, text)


@dataclass(frozen=True, slots=True)
class BaselineEntry:
    """A single captured finding identity."""

    file_path: str
    rule_id: str
    description_hash: str
    severity: str = ""
    description: str = ""

    def key(self) -> tuple[str, str, str]:
        return (self.file_path, self.rule_id, self.description_hash)


def _rule_id_for(f: Finding) -> str:
    if f.rule_id:
        return f.rule_id
    return synthesize_rule_id(f.severity, f.description)


def _finding_key(f: Finding) -> tuple[str, str, str]:
    """Identity key matching :meth:`BaselineEntry.key` for a live ``Finding``.

    Centralizes the (file_path, rule_id, description_hash) construction so
    the baseline write and diff paths cannot drift on how a finding is
    identified.
    """
    return (f.file_path, _rule_id_for(f), _description_hash(f.description))


def _description_hash(description: str) -> str:
    """Stable short hash of the first 120 chars of ``description``.

    Used to identity-match findings across runs. First 120 chars so a
    trivially reworded description still collides — stricter would
    defeat the baseline's "accept known noise" purpose.

    The hash is the first 16 hex chars of SHA-1 (64 bits). Birthday-bound
    collision probability stays under 1 in 2^32 even for tens of thousands
    of distinct (file, rule_id) keys, well above any realistic baseline
    size. SHA-1 is used for stability, not security.
    """
    return hashlib.sha1(description[:120].encode("utf-8")).hexdigest()[:16]


def findings_to_entries(findings: list[Finding]) -> list[BaselineEntry]:
    """Convert confirmed findings into baseline identity entries."""
    return [
        BaselineEntry(
            file_path=f.file_path,
            rule_id=_rule_id_for(f),
            description_hash=_description_hash(f.description),
            severity=f.severity,
            description=f.description[:120],
        )
        for f in findings
    ]


def write_baseline(path: Path, entries: list[BaselineEntry]) -> None:
    """Write entries to a baseline JSONL file, overwriting any existing file."""
    lines: list[str] = [
        json.dumps({_HEADER_KEY: True, "schema_version": SCHEMA_VERSION, "count": len(entries)})
    ]
    for e in entries:
        lines.append(
            json.dumps(
                {
                    "file_path": e.file_path,
                    "rule_id": e.rule_id,
                    "description_hash": e.description_hash,
                    "severity": e.severity,
                    "description": e.description,
                }
            )
        )
    _atomic_write_text(path, "\n".join(lines) + "\n")


def read_baseline(path: Path) -> list[BaselineEntry]:
    """Read entries from a baseline JSONL file. Missing file → empty list.

    Malformed lines are skipped with a warning; the schema-version record
    must decode cleanly or a warning is emitted.
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        warnings.warn(f"could not read baseline {path}: {exc}", UserWarning, stacklevel=2)
        return []
    entries: list[BaselineEntry] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            warnings.warn(
                f"skipping malformed baseline entry at {path}:{line_no}: {exc}",
                UserWarning,
                stacklevel=2,
            )
            continue
        if obj.get(_HEADER_KEY):
            # Header — verify schema_version but do not emit.
            version = str(obj.get("schema_version", ""))
            if version and version != SCHEMA_VERSION:
                warnings.warn(
                    f"{path}: schema_version {version!r} does not match "
                    f"expected {SCHEMA_VERSION!r}; parsing anyway",
                    UserWarning,
                    stacklevel=2,
                )
            continue
        try:
            entries.append(
                BaselineEntry(
                    file_path=str(obj["file_path"]),
                    rule_id=str(obj["rule_id"]),
                    description_hash=str(obj["description_hash"]),
                    severity=str(obj.get("severity", "")),
                    description=str(obj.get("description", "")),
                )
            )
        except (KeyError, TypeError) as exc:
            warnings.warn(
                f"skipping invalid baseline entry at {path}:{line_no}: {exc}",
                UserWarning,
                stacklevel=2,
            )
    return entries


def filter_against_baseline(
    findings: list[Finding],
    baseline: list[BaselineEntry],
) -> tuple[list[Finding], list[Finding]]:
    """Partition ``findings`` into (new, suppressed-by-baseline).

    Matching is by the same identity key written out by
    :func:`findings_to_entries`. The function is pure — no I/O, no logging.
    """
    baseline_keys = {e.key() for e in baseline}
    new_findings: list[Finding] = []
    suppressed: list[Finding] = []
    for f in findings:
        if _finding_key(f) in baseline_keys:
            suppressed.append(f)
        else:
            new_findings.append(f)
    return new_findings, suppressed


@dataclass(frozen=True, slots=True)
class BaselineDiff:
    """Partitioning of current findings against a baseline.

    The three lists are guaranteed disjoint and, together with
    ``baseline_only``, cover the full union of old and new identity keys.
    """

    new: list[Finding]
    persisting: list[Finding]
    fixed: list[BaselineEntry]  # in baseline but not in current
    baseline_only: list[BaselineEntry]  # alias kept for backward-compat


def diff_against_baseline(
    findings: list[Finding],
    baseline: list[BaselineEntry],
) -> BaselineDiff:
    """Compare current ``findings`` to a saved ``baseline``.

    Returns a :class:`BaselineDiff` split into:
        - new: in ``findings`` but not in ``baseline``
        - persisting: in both
        - fixed: in ``baseline`` but not in ``findings`` (presumably fixed)

    The three finding-lists are disjoint; ``baseline_only`` is an alias
    of ``fixed`` preserved so older callers that only care about
    "what disappeared" can read that name.
    """
    baseline_by_key = {e.key(): e for e in baseline}
    current_keys: set[tuple[str, str, str]] = set()
    new_findings: list[Finding] = []
    persisting: list[Finding] = []
    for f in findings:
        key = _finding_key(f)
        current_keys.add(key)
        if key in baseline_by_key:
            persisting.append(f)
        else:
            new_findings.append(f)
    fixed = [baseline_by_key[k] for k in baseline_by_key.keys() - current_keys]
    return BaselineDiff(
        new=new_findings,
        persisting=persisting,
        fixed=fixed,
        baseline_only=fixed,
    )
