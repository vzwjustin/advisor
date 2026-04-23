"""Targeted false-positive suppressions.

Baselines suppress everything-at-a-point-in-time; suppressions are
targeted per-rule, per-file, with optional expiry dates. They live in
``.advisor/suppressions.jsonl`` (JSON Lines) so the core package stays
zero-dep — no YAML runtime requirement. Each line is a JSON object with
the same fields a YAML entry would have.

## Schema (one JSON object per line, plus a top-line header)

    {"__advisor_suppressions__": true, "schema_version": "1.0"}
    {"rule_id": "advisor/high/abc1234567", "file": "src/legacy/parser.py",
     "reason": "rewrite scheduled for Q3", "until": "2026-09-01"}
    {"rule_id": "advisor/medium/def8901234", "file_glob": "tests/**/*.py",
     "reason": "test fixtures intentionally permissive"}

## Enforcement

- ``until`` is **required** for any suppression that applies to a
  finding above MEDIUM severity — expiry prevents "suppress and
  forget". Loading a file with a non-expiring > MEDIUM entry raises
  ``ValueError``.
- ``reason`` is required whenever ``until`` is required. Non-empty.
- An expired ``until`` logs a warning at load time and the entry is
  *not* silently re-activated — it must be renewed explicitly.
- ``file`` (exact path) and ``file_glob`` (fnmatch-style) are mutually
  exclusive. ``file_glob`` supports ``**``.

The loader is pure. Applying suppressions to a list of findings happens
via :func:`apply_suppressions`.
"""

from __future__ import annotations

import fnmatch
import json
import re
import warnings
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePath

from .verify import Finding

SCHEMA_VERSION = "1.0"
_HEADER_KEY = "__advisor_suppressions__"

_SEVERITY_RANK: dict[str, int] = {
    "LOW": 1,
    "MEDIUM": 2,
    "HIGH": 3,
    "CRITICAL": 4,
}
_REQUIRES_EXPIRY_ABOVE = 2  # > MEDIUM requires an ``until`` date


@dataclass(frozen=True, slots=True)
class Suppression:
    """A single suppression entry."""

    rule_id: str
    reason: str
    file: str | None = None
    file_glob: str | None = None
    until: str | None = None  # ISO date, YYYY-MM-DD
    expired: bool = False  # set by the loader when ``until`` is in the past

    def matches(self, finding_path: str, finding_rule_id: str) -> bool:
        if finding_rule_id != self.rule_id:
            return False
        if self.file is not None and finding_path != self.file:
            return False
        if self.file_glob is not None and not _matches_glob(finding_path, self.file_glob):
            return False
        return True


def _matches_glob(file_path: str, pattern: str) -> bool:
    """Match ``file_path`` against ``pattern``, supporting ``**`` recursion."""
    p = PurePath(file_path).as_posix()
    if "**" in pattern:
        # Simple translation: ** → .*, * → [^/]*, ? → [^/]
        parts: list[str] = []
        i = 0
        while i < len(pattern):
            c = pattern[i]
            if c == "*":
                if i + 1 < len(pattern) and pattern[i + 1] == "*":
                    if i + 2 < len(pattern) and pattern[i + 2] == "/":
                        parts.append("(?:.*/)?")
                        i += 3
                    else:
                        parts.append(".*")
                        i += 2
                else:
                    parts.append("[^/]*")
                    i += 1
            elif c == "?":
                parts.append("[^/]")
                i += 1
            else:
                parts.append(re.escape(c))
                i += 1
        try:
            return bool(re.match("^" + "".join(parts) + "$", p))
        except re.error:
            # Malformed regex translation — fall through to plain fnmatch
            # so a suppressions.jsonl with an edge-case glob doesn't crash
            # the whole load pass.
            pass
    return fnmatch.fnmatch(p, pattern)


_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_until(raw: str | None, *, context: str) -> tuple[str | None, bool]:
    """Return (normalized_until, expired) or raise ValueError on bad shape."""
    if raw is None or raw == "":
        return None, False
    # Reject datetime-shaped strings up front. Python 3.11+ ``date.fromisoformat``
    # accepts full ISO datetimes (e.g. ``2026-09-01T00:00:00+05:30``) and
    # silently drops the time and timezone, which would store an
    # off-by-one expiry under any non-UTC offset.
    if not _DATE_ONLY_RE.match(raw):
        raise ValueError(
            f"{context}: invalid until={raw!r}: expected YYYY-MM-DD (date only, no time/tz)"
        )
    try:
        d = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{context}: invalid until={raw!r}: {exc}") from exc
    today = datetime.now(timezone.utc).date()
    expired = d < today
    return d.isoformat(), expired


def load_suppressions(path: Path) -> tuple[Suppression, ...]:
    """Load suppressions from a JSONL file.

    Missing file → empty tuple. Malformed lines raise :class:`ValueError`
    with a pointer to the offending line; entries that fail the
    "> MEDIUM requires ``until`` + ``reason``" rule raise the same. Use
    a try/except at the call site if you want graceful fallback.

    Expired ``until`` dates are not a hard error — they emit a warning
    and the matching entry keeps ``expired=True`` so callers can choose
    to respect-but-report rather than silently re-activate the finding.
    """
    if not path.exists():
        return ()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc

    entries: list[Suppression] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
        if obj.get(_HEADER_KEY):
            version = str(obj.get("schema_version", ""))
            if version and version != SCHEMA_VERSION:
                warnings.warn(
                    f"{path}: schema_version {version!r} does not match "
                    f"expected {SCHEMA_VERSION!r}; parsing anyway",
                    UserWarning,
                    stacklevel=2,
                )
            continue
        ctx = f"{path}:{line_no}"
        rule_id = obj.get("rule_id")
        reason = obj.get("reason", "")
        file_ = obj.get("file")
        file_glob = obj.get("file_glob")
        until_raw = obj.get("until")

        if not rule_id or not isinstance(rule_id, str):
            raise ValueError(f"{ctx}: missing required 'rule_id'")
        if file_ and file_glob:
            raise ValueError(f"{ctx}: 'file' and 'file_glob' are mutually exclusive")
        if not (file_ or file_glob):
            raise ValueError(f"{ctx}: one of 'file' or 'file_glob' is required")

        until_norm, expired = _parse_until(until_raw, context=ctx)

        # Above-MEDIUM requires both until and reason.
        if _severity_from_rule_id(rule_id) > _REQUIRES_EXPIRY_ABOVE:
            if not until_norm:
                raise ValueError(
                    f"{ctx}: rule {rule_id!r} is above MEDIUM — 'until' date is required"
                )
            if not reason or not str(reason).strip():
                raise ValueError(
                    f"{ctx}: rule {rule_id!r} is above MEDIUM — non-empty 'reason' is required"
                )

        if expired:
            warnings.warn(
                f"{ctx}: suppression for {rule_id!r} expired on {until_norm}; renew or remove",
                UserWarning,
                stacklevel=2,
            )

        entries.append(
            Suppression(
                rule_id=str(rule_id),
                reason=str(reason or ""),
                file=str(file_) if file_ else None,
                file_glob=str(file_glob) if file_glob else None,
                until=until_norm,
                expired=expired,
            )
        )
    return tuple(entries)


def _severity_from_rule_id(rule_id: str) -> int:
    """Extract severity rank from a ``advisor/<sev>/<hash>``-shaped id.

    Foreign rule ids default to MEDIUM (rank 2) so expiry is not forced
    on callers that pipe findings from other tools.

    Forms recognized as authoritative-severity (subject to the
    ``until``/``reason`` gate above MEDIUM):

    * ``advisor/<sev>/<hash>`` — first segment is the namespace marker
    * ``<SEV>/<anything>`` — short form, e.g. ``HIGH/abc``; first
      segment IS the severity. Without this branch, ``HIGH/abc`` would
      decay to MEDIUM and bypass the expiry requirement.
    * Bare ``<SEV>`` — single segment treated as severity directly.
    """
    parts = rule_id.split("/")
    if parts and parts[0].upper() == "ADVISOR" and len(parts) >= 2:
        return _SEVERITY_RANK.get(parts[1].upper(), _REQUIRES_EXPIRY_ABOVE)
    if parts and parts[0].upper() in _SEVERITY_RANK:
        return _SEVERITY_RANK[parts[0].upper()]
    return _REQUIRES_EXPIRY_ABOVE


def apply_suppressions(
    findings: list[Finding],
    suppressions: tuple[Suppression, ...],
) -> tuple[list[Finding], list[tuple[Finding, Suppression]]]:
    """Drop findings that match an active (non-expired) suppression.

    Returns ``(kept, dropped_pairs)``. Expired suppressions are ignored
    for matching — their findings fall through to ``kept``.
    """
    from .sarif import synthesize_rule_id

    active = [s for s in suppressions if not s.expired]
    kept: list[Finding] = []
    dropped: list[tuple[Finding, Suppression]] = []
    for f in findings:
        rid = f.rule_id or synthesize_rule_id(f.severity, f.description)
        matched: Suppression | None = None
        for s in active:
            if s.matches(f.file_path, rid):
                matched = s
                break
        if matched is not None:
            dropped.append((f, matched))
        else:
            kept.append(f)
    return kept, dropped
