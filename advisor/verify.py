"""Verification pass — filters agent findings for real, significant issues.

Glasswing technique #3: a final agent reviews all findings from focused agents,
confirms whether each is real and interesting, and filters out noise. This
prevents false positives from wasting human attention.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Finding:
    """A single finding from a focused agent."""
    file_path: str
    severity: str
    description: str
    evidence: str
    fix: str


@dataclass(frozen=True)
class VerifiedResult:
    """The output of the verification pass."""
    confirmed: tuple[Finding, ...]
    rejected: tuple[Finding, ...]
    summary: str


VERIFY_PROMPT_TEMPLATE = (
    "You are the verification agent. Your job is to review findings from "
    "other agents and determine which are real, significant issues versus "
    "false positives or low-value noise.\n\n"
    "## Findings to Verify\n\n"
    "{findings_block}\n\n"
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
    """Format findings into a markdown block for the verification prompt.

    Args:
        findings: List of Finding objects from focused agents.

    Returns:
        Markdown-formatted findings block.
    """
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
    """Build the complete verification agent prompt.

    Args:
        findings: All findings from focused agents.

    Returns:
        Ready-to-use prompt string for the verification agent.
    """
    block = format_findings_block(findings)
    return VERIFY_PROMPT_TEMPLATE.format(findings_block=block)


def parse_findings_from_text(text: str) -> list[Finding]:
    """Best-effort parse of agent output into Finding objects.

    Looks for markdown-structured findings with File, Severity, Description,
    Evidence, and Fix fields. Tolerates missing fields gracefully.

    Args:
        text: Raw text output from a focused agent.

    Returns:
        List of Finding objects extracted from the text.
    """
    findings: list[Finding] = []
    current: dict[str, str] = {}

    for line in text.split("\n"):
        stripped = line.strip()

        if stripped.startswith("- **File**:") or stripped.startswith("**File**:"):
            if current.get("file_path"):
                findings.append(_dict_to_finding(current))
                current = {}
            current["file_path"] = _extract_value(stripped)

        elif stripped.startswith("- **Severity**:") or stripped.startswith("**Severity**:"):
            current["severity"] = _extract_value(stripped)

        elif stripped.startswith("- **Description**:") or stripped.startswith("**Description**:"):
            current["description"] = _extract_value(stripped)

        elif stripped.startswith("- **Evidence**:") or stripped.startswith("**Evidence**:"):
            current["evidence"] = _extract_value(stripped)

        elif stripped.startswith("- **Fix**:") or stripped.startswith("**Fix**:"):
            current["fix"] = _extract_value(stripped)

    if current.get("file_path"):
        findings.append(_dict_to_finding(current))

    return findings


def _extract_value(line: str) -> str:
    """Extract the value after the colon in a '**Key**: value' line."""
    parts = line.split(":", 1)
    return parts[1].strip().strip("`") if len(parts) > 1 else ""


def _dict_to_finding(d: dict[str, str]) -> Finding:
    """Convert a partial dict to a Finding, filling missing fields."""
    return Finding(
        file_path=d.get("file_path", "unknown"),
        severity=d.get("severity", "MEDIUM"),
        description=d.get("description", ""),
        evidence=d.get("evidence", ""),
        fix=d.get("fix", ""),
    )
