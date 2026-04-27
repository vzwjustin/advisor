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
from urllib.parse import quote as _url_quote

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

    The slug is the first 16 hex chars of SHA-1 (64 bits) — at that width
    the birthday-bound collision probability stays under 1 in 2^32 even
    for ~65k distinct rule keys per run, which exceeds any realistic
    finding count by orders of magnitude. SHA-1 is used for stability,
    not security; the input is severity-bucketed description prefix.
    """
    slug = hashlib.sha1(description[:80].encode("utf-8")).hexdigest()[:16]
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
    # Detect a Windows drive-letter prefix (``C:`` / ``c:``) and peel it
    # off before splitting so paths like ``C:\src\auth.py:42`` aren't
    # decomposed into ``["C", "\\src\\auth.py", "42"]``. Re-apply the
    # prefix to the path component before returning.
    # Strip any embedded whitespace and NUL bytes anywhere in the path.
    # Filenames don't contain newlines / tabs / CR / NUL — if any of
    # those slipped in (e.g. a runner emitted ``"src/foo.py\n:42"`` from
    # a malformed template) they would otherwise survive into the SARIF
    # ``artifactLocation.uri`` and break path-equality matching for
    # GitHub Code Scanning. ``\x00`` is included because some SARIF
    # consumers treat the URI as a C string and truncate at the first
    # NUL — silent path corruption.
    stripped = "".join(c for c in stripped if c not in "\x00\n\r\t")
    drive_prefix = ""
    body = stripped
    if len(stripped) >= 2 and stripped[1] == ":" and stripped[0].isalpha():
        drive_prefix = stripped[:2]
        body = stripped[2:]
    parts = body.rsplit(":", 2)
    # Scan from the right: accept ``path:line`` and ``path:line:col``.
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return drive_prefix + parts[0], int(parts[1])
    if len(parts) >= 2 and parts[-1].isdigit():
        return drive_prefix + ":".join(parts[:-1]), int(parts[-1])
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
    posix = p.as_posix()
    # Guard against relative paths that escape the source root via ``..``.
    # lstrip("./") was wrong here — it strips the character *set* {'.', '/'},
    # corrupting ``../foo`` → ``foo`` and ``.hidden`` → ``hidden``. We keep
    # the path as-is but reject any segment that climbs above %SRCROOT%.
    if any(part == ".." for part in p.parts):
        raise ValueError(
            f"file_path {path!r} escapes target_dir {target_dir!s} via '..'; "
            f"SARIF requires paths to resolve under %SRCROOT%"
        )
    return posix


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
        # Skip findings with no file_path — an empty path would produce a
        # SARIF result pointing at the source root, which is misleading.
        # _dict_to_finding already drops these upstream; this guard covers
        # directly-constructed Finding objects.
        if not f.file_path or not f.file_path.strip():
            continue
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
            # SARIF 2.1.0 requires startLine >= 1. Runners occasionally
            # emit ``path:0`` for file-level findings — clamp rather than
            # let a downstream validator reject the whole run.
            region["startLine"] = max(1, line)

        # SARIF's ``artifactLocation.uri`` is a uri-reference per RFC 3986
        # (per the schema's ``"format": "uri-reference"`` constraint).
        # Path components must be percent-encoded — spaces, ``#``, ``?``,
        # ``&``, and other reserved chars otherwise change the URI's
        # meaning to consumers like GitHub Code Scanning. Preserve ``/``
        # so the relative path structure stays intact.
        physical_location: dict[str, Any] = {
            "artifactLocation": {
                "uri": _url_quote(rel, safe="/"),
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
    """Clip ``text`` to ``limit`` chars, for the SARIF shortDescription field.

    Collapses any embedded newlines / CR / tabs to single spaces. SARIF
    consumers (notably GitHub Code Scanning) display ``shortDescription``
    on a single line; an embedded newline survives into the rule list
    and breaks rendering. Strip-and-clip happens AFTER the collapse so
    a clip that lands on a former newline doesn't leave a trailing space.
    """
    # Collapse all whitespace runs (incl. \n, \r, \t) to a single space
    # in one pass so the truncation math operates on display-width.
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed or "advisor finding"
    return collapsed[: limit - 1].rstrip() + "…"
