"""Tests for targeted suppressions (Phase 4c)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest

from advisor.suppressions import (
    Suppression,
    apply_suppressions,
    load_suppressions,
)
from advisor.verify import Finding


def _f(
    path: str = "src/legacy/parser.py",
    severity: str = "HIGH",
    description: str = "x",
    rule_id: str | None = None,
) -> Finding:
    return Finding(
        file_path=path,
        severity=severity,
        description=description,
        evidence="",
        fix="",
        rule_id=rule_id,
    )


def _write_jsonl(path: Path, lines: list[dict[str, object]]) -> None:
    import json

    text = "\n".join(json.dumps(x) for x in lines) + "\n"
    path.write_text(text, encoding="utf-8")


class TestLoader:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_suppressions(tmp_path / "absent.jsonl") == ()

    def test_malformed_json_warns_and_skips(self, tmp_path: Path) -> None:
        # A malformed line must not abort the whole load — that would
        # silently disable the entire suppression file and resurface all
        # suppressed findings. Matches baseline.read_baseline semantics.
        p = tmp_path / "bad.jsonl"
        p.write_text(
            '{not valid json\n{"rule_id": "r1", "file": "x.py"}\n',
            encoding="utf-8",
        )
        with pytest.warns(UserWarning, match="skipping malformed suppression entry"):
            result = load_suppressions(p)
        assert len(result) == 1
        assert result[0].rule_id == "r1"

    def test_non_utf8_file_raises_value_error(self, tmp_path: Path) -> None:
        p = tmp_path / "bad-encoding.jsonl"
        p.write_bytes(b"\x80\x81\x82")
        with pytest.raises(ValueError, match="could not read"):
            load_suppressions(p)

    def test_above_medium_requires_until(self, tmp_path: Path) -> None:
        p = tmp_path / "s.jsonl"
        _write_jsonl(
            p,
            [
                {"__advisor_suppressions__": True, "schema_version": "1.0"},
                {
                    "rule_id": "advisor/high/abc",
                    "file": "src/a.py",
                    "reason": "reason",
                    # missing until
                },
            ],
        )
        with pytest.raises(ValueError, match="above MEDIUM"):
            load_suppressions(p)

    def test_above_medium_requires_reason(self, tmp_path: Path) -> None:
        p = tmp_path / "s.jsonl"
        future = (date.today() + timedelta(days=30)).isoformat()
        _write_jsonl(
            p,
            [
                {
                    "rule_id": "advisor/critical/abc",
                    "file": "src/a.py",
                    "until": future,
                    "reason": "",
                },
            ],
        )
        with pytest.raises(ValueError, match="reason"):
            load_suppressions(p)

    def test_expired_emits_warning(self, tmp_path: Path) -> None:
        p = tmp_path / "s.jsonl"
        past = (date.today() - timedelta(days=30)).isoformat()
        _write_jsonl(
            p,
            [
                {
                    "rule_id": "advisor/high/abc",
                    "file": "x.py",
                    "until": past,
                    "reason": "r",
                },
            ],
        )
        with pytest.warns(UserWarning, match="expired"):
            entries = load_suppressions(p)
        assert entries[0].expired is True

    def test_file_and_file_glob_mutually_exclusive(self, tmp_path: Path) -> None:
        p = tmp_path / "s.jsonl"
        _write_jsonl(
            p,
            [
                {
                    "rule_id": "advisor/medium/abc",
                    "file": "a.py",
                    "file_glob": "tests/**/*.py",
                    "reason": "r",
                },
            ],
        )
        with pytest.raises(ValueError, match="mutually exclusive"):
            load_suppressions(p)

    def test_medium_not_required_to_expire(self, tmp_path: Path) -> None:
        p = tmp_path / "s.jsonl"
        _write_jsonl(
            p,
            [
                {
                    "rule_id": "advisor/medium/abc",
                    "file_glob": "tests/**/*.py",
                    "reason": "permissive",
                },
            ],
        )
        entries = load_suppressions(p)
        assert entries[0].rule_id == "advisor/medium/abc"
        assert entries[0].expired is False


class TestApply:
    def test_active_suppression_drops_match(self) -> None:
        supp = (
            Suppression(
                rule_id="custom/rule",
                reason="r",
                file="x.py",
                until="2030-01-01",
            ),
        )
        findings = [_f(path="x.py", rule_id="custom/rule"), _f(path="y.py")]
        kept, dropped = apply_suppressions(findings, supp)
        assert len(kept) == 1
        assert kept[0].file_path == "y.py"
        assert len(dropped) == 1

    def test_exact_file_match_normalizes_finding_paths(self) -> None:
        supp = (
            Suppression(
                rule_id="custom/rule",
                reason="r",
                file="src/app.py",
            ),
        )
        findings = [_f(path="./src\\app.py:42", rule_id="custom/rule")]
        kept, dropped = apply_suppressions(findings, supp)
        assert kept == []
        assert len(dropped) == 1

    def test_expired_suppression_does_not_drop(self) -> None:
        supp = (
            Suppression(
                rule_id="custom/rule",
                reason="r",
                file="x.py",
                until="2000-01-01",
                expired=True,
            ),
        )
        findings = [_f(path="x.py", rule_id="custom/rule")]
        kept, dropped = apply_suppressions(findings, supp)
        assert len(kept) == 1
        assert dropped == []

    def test_file_glob_match(self) -> None:
        supp = (
            Suppression(
                rule_id="custom/rule",
                reason="r",
                file_glob="tests/**/*.py",
            ),
        )
        findings = [_f(path="tests/unit/x.py", rule_id="custom/rule")]
        kept, dropped = apply_suppressions(findings, supp)
        assert kept == []
        assert len(dropped) == 1

    def test_file_glob_match_normalizes_finding_paths(self) -> None:
        supp = (
            Suppression(
                rule_id="custom/rule",
                reason="r",
                file_glob="tests/**/*.py",
            ),
        )
        findings = [_f(path="tests\\unit\\x.py:9", rule_id="custom/rule")]
        kept, dropped = apply_suppressions(findings, supp)
        assert kept == []
        assert len(dropped) == 1
