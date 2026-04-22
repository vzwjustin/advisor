"""SARIF 2.1.0 emitter for advisor findings.

Pure conversion module. No I/O. No network. The caller decides where the
JSON goes. Output is a dict that a caller can serialize with
:func:`json.dumps` and that validates against the SARIF 2.1.0 schema at
https://json.schemastore.org/sarif-2.1.0.json.

## Rule-id policy

A :class:`~advisor.verify.Finding` carries an optional ``rule_id``. When it
is ``None`` (the common case — runners emit prose, not rule names), the
emitter synthesizes a stable id via :func:`synthesize_rule_id` using
severity + a short hash of the description. Same description + severity
→ same rule id across runs, so repeated findings group under one rule
entry on GitHub Code Scanning.

## Path handling

SARIF represents file locations as URIs relative to a source-root
designator (``%SRCROOT%``). :func:`findings_to_sarif` enforces that
every ``Finding.file_path`` resolves inside ``target_dir``; absolute
paths outside that tree raise :class:`ValueError` rather than leaking
the attacker's path into a CI artifact.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from advisor.verify import Finding

SARIF_SCHEMA_URI = "https://json.schemastore.org/sarif-2.1.0.json"
SARIF_VERSION = "2.1.0"
# advisor's own JSON-output schema version (E6). Bump when the shape
# emitted by ``findings_to_sarif`` changes in a breaking way for
# downstream consumers (the SARIF schema itself is pinned above).
SCHEMA_VERSION = "1.0"

# CRITICAL / HIGH → error, MEDIUM → warning, LOW → note. "note" is the
# SARIF 2.1.0 term for informational; there is no dedicated "low"
# severity. Unknown/empty severity falls back to warning so the record
# still surfaces rather than being silently dropped.
_LEVEL_MAP: dict[str, str] = {
    "CRITICAL": "error",
    "HIGH": "error",
    "MEDIUM": "warning",
    "LOW": "note",
}

# Mirror of _LEVEL_MAP used when a finding carries an unrecognized
# severity string — we want to emit *something* so the finding surfaces.
_DEFAULT_LEVEL = "warning"


def synthesize_rule_id(severity: str, description: str, *, prefix: str = "advisor") -> str:
    """Stable rule-id for a finding that lacks one.

    Uses severity + a hash of the first 80 chars of the description so
    repeated findings (same description text, same severity) group under
    the same rule on GitHub Code Scanning. The severity is lowercased so
    ``CRITICAL``/``critical`` collapse to one id.
    """
    slug = hashlib.sha1(description[:80].encode("utf-8")).hexdigest()[:10]
    return f"{prefix}/{severity.lower()}/{slug}"


def _rule_id_for(finding: Finding, *, prefix: str) -> str:
    """Return the finding's rule_id, synthesizing one when absent."""
    if finding.rule_id:
        return finding.rule_id
    return synthesize_rule_id(finding.severity, finding.description, prefix=prefix)


def _level_for(severity: str) -> str:
    """Map an advisor severity string to a SARIF level."""
    return _LEVEL_MAP.get(severity.upper(), _DEFAULT_LEVEL)


def _parse_file_path(raw: str) -> tuple[str, int | None]:
    """Split ``path:line`` / ``path:line:col`` into (path, line_number_or_None).

    Findings emitted by runners conventionally append a line number as
    ``src/auth.py:42``. SARIF wants the two fields separate.
    """
    stripped = raw.strip().strip("`").rstrip()
    parts = stripped.rsplit(":", 2)
    # Scan from the right: accept ``path:line`` and ``path:line:col``.
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return parts[0], int(parts[1])
    if len(parts) >= 2 and parts[-1].isdigit():
        return ":".join(parts[:-1]), int(parts[-1])
    return stripped, None


def _resolve_relative(path: str, target_dir: Path) -> str:
    """Return ``path`` as a POSIX path relative to ``target_dir``.

    Relative paths are treated as already rooted at ``target_dir``.
    Absolute paths must resolve to a location inside ``target_dir`` or
    :class:`ValueError` is raised — SARIF's ``%SRCROOT%`` semantics
    require every URI to be below the source-root.
    """
    target_resolved = target_dir.resolve()
    p = Path(path)
    if p.is_absolute():
        try:
            rel = p.resolve().relative_to(target_resolved)
        except ValueError as exc:
            raise ValueError(
                f"file_path {path!r} is outside target_dir {target_dir!s}; "
                f"SARIF requires paths to resolve under %SRCROOT%"
            ) from exc
        return rel.as_posix()
    # Already relative — normalize to POSIX separators for cross-platform
    # determinism but do NOT resolve (a non-existent file shouldn't fail).
    return p.as_posix().lstrip("./")


def findings_to_sarif(
    findings: list[Finding],
    *,
    tool_version: str,
    target_dir: Path,
    rule_id_fallback_prefix: str = "advisor",
) -> dict[str, Any]:
    """Convert verified findings into a SARIF 2.1.0 run object.

    Args:
        findings: Confirmed findings from the verify pass.
        tool_version: Advisor's version string (e.g. ``"0.5.0"``) — shown
            under ``driver.version`` in the SARIF output.
        target_dir: Filesystem root for this run. All ``Finding.file_path``
            values must resolve inside this tree; absolute paths that
            escape it raise :class:`ValueError`.
        rule_id_fallback_prefix: First segment of synthesized rule ids.
            Override when emitting under a different tool name.

    Returns:
        A plain dict ready for :func:`json.dumps`. Validates against
        ``https://json.schemastore.org/sarif-2.1.0.json``.
    """
    # Unique rules, ordered by first appearance — GitHub renders the rule
    # list in this order in the Code Scanning UI.
    rules_seen: dict[str, dict[str, Any]] = {}
    rule_index_by_id: dict[str, int] = {}
    results: list[dict[str, Any]] = []

    for f in findings:
        rule_id = _rule_id_for(f, prefix=rule_id_fallback_prefix)
        if rule_id not in rules_seen:
            rule_index_by_id[rule_id] = len(rules_seen)
            rules_seen[rule_id] = {
                "id": rule_id,
                "name": rule_id.replace("/", "_"),
                "shortDescription": {"text": _short_text(f.description)},
                "fullDescription": {"text": f.description or rule_id},
                "defaultConfiguration": {"level": _level_for(f.severity)},
                "help": {"text": f.fix or "See advisor output for remediation guidance."},
            }
        file_path, line = _parse_file_path(f.file_path)
        rel = _resolve_relative(file_path, target_dir)

        region: dict[str, Any] = {}
        if line is not None:
            region["startLine"] = line

        physical_location: dict[str, Any] = {
            "artifactLocation": {
                "uri": rel,
                "uriBaseId": "%SRCROOT%",
            },
        }
        if region:
            physical_location["region"] = region

        results.append(
            {
                "ruleId": rule_id,
                "ruleIndex": rule_index_by_id[rule_id],
                "level": _level_for(f.severity),
                "message": {"text": f.description or rule_id},
                "locations": [{"physicalLocation": physical_location}],
                "properties": {
                    "severity": f.severity,
                    "evidence": f.evidence,
                    "fix": f.fix,
                },
            }
        )

    run: dict[str, Any] = {
        "tool": {
            "driver": {
                "name": "advisor",
                "version": tool_version,
                "informationUri": "https://github.com/vzwjustin/advisor",
                "rules": list(rules_seen.values()),
            },
        },
        "originalUriBaseIds": {
            "%SRCROOT%": {"uri": target_dir.resolve().as_uri() + "/"},
        },
        "results": results,
    }

    return {
        "$schema": SARIF_SCHEMA_URI,
        "version": SARIF_VERSION,
        "runs": [run],
    }


def _short_text(text: str, *, limit: int = 120) -> str:
    """Clip ``text`` to ``limit`` chars, for the SARIF shortDescription field."""
    stripped = text.strip()
    if len(stripped) <= limit:
        return stripped or "advisor finding"
    return stripped[: limit - 1].rstrip() + "…"
