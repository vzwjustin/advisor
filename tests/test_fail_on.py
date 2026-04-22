"""Tests for --fail-on threshold (Phase 4a)."""

from __future__ import annotations

import pytest

from advisor.__main__ import _FAIL_ON_EXIT_CODE, _fail_on_findings
from advisor.verify import Finding


def _f(severity: str) -> Finding:
    return Finding(file_path="a.py", severity=severity, description="d", evidence="", fix="")


class TestFailOn:
    @pytest.mark.parametrize(
        ("threshold", "severity", "expected"),
        [
            ("never", "CRITICAL", None),
            (None, "CRITICAL", None),
            ("low", "LOW", _FAIL_ON_EXIT_CODE),
            ("low", "MEDIUM", _FAIL_ON_EXIT_CODE),
            ("low", "HIGH", _FAIL_ON_EXIT_CODE),
            ("low", "CRITICAL", _FAIL_ON_EXIT_CODE),
            ("medium", "LOW", None),
            ("medium", "MEDIUM", _FAIL_ON_EXIT_CODE),
            ("medium", "HIGH", _FAIL_ON_EXIT_CODE),
            ("high", "LOW", None),
            ("high", "MEDIUM", None),
            ("high", "HIGH", _FAIL_ON_EXIT_CODE),
            ("high", "CRITICAL", _FAIL_ON_EXIT_CODE),
            ("critical", "HIGH", None),
            ("critical", "CRITICAL", _FAIL_ON_EXIT_CODE),
        ],
    )
    def test_threshold_matrix(
        self, threshold: str | None, severity: str, expected: int | None
    ) -> None:
        rc = _fail_on_findings(threshold, [_f(severity)])
        assert rc == expected

    def test_no_findings_never_trips(self) -> None:
        assert _fail_on_findings("critical", []) is None

    def test_unknown_severity_ignored(self) -> None:
        assert _fail_on_findings("high", [_f("UNKNOWN")]) is None
