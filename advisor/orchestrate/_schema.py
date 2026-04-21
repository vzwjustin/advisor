"""Canonical finding report schema for runner output."""
from __future__ import annotations

FINDING_SCHEMA = """\
- **File**: path:line_number
- **Severity**: CRITICAL / HIGH / MEDIUM / LOW
- **Description**: what the issue is
- **Evidence**: the code path or proof
- **Fix**: suggested remediation"""
