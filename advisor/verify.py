"""Verification pass — filters agent findings for real, significant issues.

A final agent reviews all findings from focused agents,
confirms whether each is real and interesting, and filters out noise. This
prevents false positives from wasting human attention.

Relationship with `orchestrate.build_verify_message`:
    This module (``build_verify_prompt`` + ``Finding`` + ``parse_findings_from_text``)
    is the **structured-findings path**: callers collect ``Finding`` objects,
    format them into a fenced block, and ask an agent to CONFIRM/REJECT each.
    ``orchestrate.build_verify_message`` is a **different, complementary**
    helper — a short SendMessage used to resume the live advisor mid-pipeline
    with an already-assembled findings string. Both are public API and
    intentionally separate: one is for offline/batch verification, the other
    for the in-team advisor loop. Don't wire them together blindly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Finding:
    """A single finding from a focused agent."""
    file_path: str
    severity: str
    description: str
    evidence: str
    fix: str


_REQUIRED_FIELDS = ("file_path", "severity", "description", "evidence", "fix")

_KEY_PREFIXES: dict[str, tuple[str, ...]] = {
    "file_path": ("- **File**:", "**File**:"),
    "severity": ("- **Severity**:", "**Severity**:"),
    "description": ("- **Description**:", "**Description**:"),
    "evidence": ("- **Evidence**:", "**Evidence**:"),
    "fix": ("- **Fix**:", "**Fix**:"),
}


VERIFY_PROMPT_TEMPLATE = (
    "You are the verification agent. Your job is to review findings from "
    "other agents and determine which are real, significant issues versus "
    "false positives or low-value noise.\n\n"
    "## Findings to Verify\n\n"
    "```\n"
    "{findings_block}\n"
    "```\n\n"
    "## Instructions\n\n"
    "For each finding:\n"
    "1. Read the cited file and line to confirm the issue exists.\n"
    "2. Check whether the issue is exploitable or impactful in practice.\n"
    "3. Reject findings that are:\n"
    "   - False positives (the code is actually safe)\n"
    "   - Theoretical only (requires unrealistic conditions)\n"
    "   - Duplicates of another finding\n"
    "   - Too minor to act on (style nits, unlikely edge cases)\n\n"
    "4. For each finding, output:\n"
    "   - **CONFIRMED** or **REJECTED**\n"
    "   - **Reason**: why you confirmed or rejected it\n\n"
    "5. End with a summary: how many confirmed, how many rejected, "
    "and the top 3 most critical issues to fix first.\n\n"
    "Be strict. Only confirm issues that are real and worth fixing."
)


def format_findings_block(findings: list[Finding]) -> str:
    """Format findings into a markdown block for the verification prompt."""
    if not findings:
        return "_No findings to verify._"

    lines: list[str] = []
    for i, f in enumerate(findings, 1):
        lines.append(f"### Finding {i}")
        lines.append(f"- **File**: `{f.file_path}`")
        lines.append(f"- **Severity**: {f.severity}")
        lines.append(f"- **Description**: {f.description}")
        lines.append(f"- **Evidence**: {f.evidence}")
        lines.append(f"- **Fix**: {f.fix}")
        lines.append("")

    return "\n".join(lines)


def build_verify_prompt(findings: list[Finding]) -> str:
    """Build the complete verification agent prompt."""
    block = format_findings_block(findings)
    return VERIFY_PROMPT_TEMPLATE.format(findings_block=block)


def parse_findings_from_text(text: str) -> list[Finding]:
    """Best-effort parse of agent output into Finding objects.

    Blocks missing any of the five required fields are dropped rather than
    silently promoted with default values. This prevents the verification
    pipeline from acting on fabricated data when agent output is malformed.

    Block boundaries are detected in two ways (primary first):
    1. ``### Finding`` header lines — the primary delimiter, matches the
       format emitted by ``format_findings_block``.
    2. A second ``file_path`` key — safety-net for output that omits headers.

    Within a block, a key match is only treated as a new field if the key
    has not already been set in the current block. Otherwise the line is
    treated as continuation text for the active field. This prevents field
    labels that appear inside description/evidence/fix bodies (e.g. a
    description containing "Fix: use parameterized queries") from corrupting
    the block.
    """
    findings: list[Finding] = []
    current: dict[str, str] = {}
    active_key: str | None = None

    def _flush() -> None:
        finding = _dict_to_finding(current)
        if finding is not None:
            findings.append(finding)

    def _match_key(stripped: str) -> tuple[str, str] | None:
        for key, prefixes in _KEY_PREFIXES.items():
            for prefix in prefixes:
                if stripped.startswith(prefix):
                    return key, _extract_value(stripped, prefix)
        return None

    for line in text.split("\n"):
        stripped = line.strip()

        # Primary block delimiter: ### Finding headers from format_findings_block.
        if stripped.startswith("### Finding"):
            if current:
                _flush()
                current = {}
            active_key = None
            continue

        matched = _match_key(stripped)

        if matched is not None:
            key, value = matched
            if key not in current:
                # New field for this block — record it.
                current[key] = value
                active_key = key
            elif key == "file_path":
                # Safety-net: second file_path signals a new block in
                # header-less output. Flush and start fresh.
                _flush()
                current = {key: value}
                active_key = key
            else:
                # Key already set — this label text appeared inside a body
                # value. Treat as continuation of the active field.
                if active_key:
                    current[active_key] = current[active_key] + " " + stripped
        elif active_key and stripped and not stripped.startswith("#"):
            current[active_key] = current.get(active_key, "") + " " + stripped

    if current:
        _flush()

    return findings


def _extract_value(line: str, prefix: str = "") -> str:
    """Extract the value after the known prefix in a '**Key**: value' line.

    Uses the matched prefix length to avoid splitting on colons inside the
    value (e.g. Windows paths like ``C:\\Users\\...``).
    """
    if prefix:
        after = line[len(prefix):]
        return after.strip().strip("`")
    parts = line.split(":", 1)
    return parts[1].strip().strip("`") if len(parts) > 1 else ""


def _dict_to_finding(d: dict[str, str]) -> Finding | None:
    """Convert a parsed block to a Finding, or None if any required field is missing."""
    missing = [k for k in _REQUIRED_FIELDS if not d.get(k)]
    if missing:
        return None
    return Finding(
        file_path=d["file_path"],
        severity=d["severity"],
        description=d["description"],
        evidence=d["evidence"],
        fix=d["fix"],
    )
