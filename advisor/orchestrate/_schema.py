"""Canonical finding report schema for runner output."""

from __future__ import annotations

FINDING_SCHEMA = """\
- **File**: path:line_number
- **Severity**: CRITICAL / HIGH / MEDIUM / LOW
- **Description**: what the issue is
- **Evidence**: the code path or proof
- **Expected → Actual**: *(MEDIUM+ only)* what you expected before reading this file · what you actually found — the divergence is the finding
- **Fix**: suggested remediation"""
