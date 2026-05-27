"""Tests for ``scripts/sarif_gate.py`` — the workflow ``--fail-on`` gate.

The script lives outside the ``advisor`` package (and is shipped via the
repo's GitHub Actions workflow, not via the wheel), so it's imported
here via :mod:`importlib.util` from its absolute path. Tests exercise
the pure :func:`evaluate` function with synthetic SARIF docs and the
:func:`main` CLI shell for the error-exit paths.

Cross-checked against ``advisor/__main__.py:_FAIL_ON_RANK`` so the
gate and ``advisor audit --fail-on LEVEL`` stay in lockstep.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "sarif_gate.py"


def _load_gate() -> Any:
    """Import ``sarif_gate`` from its absolute path (it's not a package)."""
    spec = importlib.util.spec_from_file_location("sarif_gate", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate() -> Any:
    return _load_gate()


def _sarif(*results: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal SARIF 2.1.0 wrapper around ``results``."""
    return {
        "version": "2.1.0",
        "runs": [{"tool": {"driver": {"name": "advisor"}}, "results": list(results)}],
    }


class TestRankConsistency:
    """The gate's ``RANK`` dict must agree with ``advisor.__main__._FAIL_ON_RANK``
    so ``advisor audit --fail-on LEVEL`` and the workflow gate trip at
    identical severities."""

    def test_rank_matches_audit_subcommand(self, gate: Any) -> None:
        from advisor.__main__ import _FAIL_ON_RANK

        assert gate.RANK == _FAIL_ON_RANK


class TestEvaluateSeverityFromProperties:
    """When ``properties.severity`` is present (advisor's own SARIF),
    the gate uses it directly so CRITICAL and HIGH are correctly
    distinguished — the lossy SARIF level mapping (both → ``error``)
    is bypassed."""

    @pytest.mark.parametrize(
        ("severity", "threshold", "expected"),
        [
            ("CRITICAL", "critical", True),
            ("HIGH", "critical", False),
            ("HIGH", "high", True),
            ("MEDIUM", "high", False),
            ("MEDIUM", "medium", True),
            ("LOW", "medium", False),
            ("LOW", "low", True),
            ("CRITICAL", "low", True),
            ("CRITICAL", "never", False),
        ],
    )
    def test_severity_matrix(
        self, gate: Any, severity: str, threshold: str, expected: bool
    ) -> None:
        doc = _sarif({"level": "error", "properties": {"severity": severity}})
        tripped, _ = gate.evaluate(doc, threshold)
        assert tripped is expected

    def test_lowercase_severity_accepted(self, gate: Any) -> None:
        """The lookup lowercases before comparing, so a SARIF that
        emits a lowercase severity (uncommon but legal) still gates."""
        doc = _sarif({"level": "error", "properties": {"severity": "critical"}})
        assert gate.evaluate(doc, "critical")[0] is True


class TestEvaluateSarifLevelFallback:
    """When ``properties.severity`` is absent (third-party SARIF), the
    gate falls back to the SARIF level mapping. Lossy — CRITICAL and
    HIGH both emit ``error``, so neither can trip ``fail-on=critical``
    via this path."""

    @pytest.mark.parametrize(
        ("level", "threshold", "expected"),
        [
            ("error", "high", True),
            ("error", "critical", False),
            ("warning", "medium", True),
            ("warning", "high", False),
            ("note", "low", True),
            ("note", "medium", False),
        ],
    )
    def test_level_matrix(self, gate: Any, level: str, threshold: str, expected: bool) -> None:
        doc = _sarif({"level": level})
        tripped, _ = gate.evaluate(doc, threshold)
        assert tripped is expected

    def test_no_level_defaults_to_warning(self, gate: Any) -> None:
        """A result missing both ``properties.severity`` AND ``level``
        falls back to warning (rank 2), so ``fail-on=medium`` trips."""
        doc = _sarif({"description": "missing level field"})
        assert gate.evaluate(doc, "medium")[0] is True
        assert gate.evaluate(doc, "high")[0] is False


class TestEvaluateHostileInput:
    """The gate must not crash on malformed or hostile SARIF — every
    dict / list / string narrow uses ``isinstance`` so a malformed doc
    skips the bad result instead of raising AttributeError."""

    def test_non_dict_doc(self, gate: Any) -> None:
        assert gate.evaluate([], "critical") == (False, 0)
        assert gate.evaluate("not a dict", "critical") == (False, 0)
        assert gate.evaluate(None, "critical") == (False, 0)

    def test_non_list_runs(self, gate: Any) -> None:
        """``runs`` as a dict used to iterate as string keys and crash
        ``.get()``; the type-guard now exits cleanly."""
        assert gate.evaluate({"runs": {"k": "v"}}, "critical") == (False, 0)
        assert gate.evaluate({"runs": 42}, "critical") == (False, 0)

    def test_non_dict_run(self, gate: Any) -> None:
        assert gate.evaluate({"runs": [None, "string", 42]}, "critical") == (False, 0)

    def test_non_list_results(self, gate: Any) -> None:
        assert gate.evaluate({"runs": [{"results": 7}]}, "critical") == (False, 0)

    def test_non_dict_result(self, gate: Any) -> None:
        assert gate.evaluate({"runs": [{"results": ["string", 42, None]}]}, "critical") == (
            False,
            0,
        )

    def test_non_dict_properties_no_crash(self, gate: Any) -> None:
        """``properties: 42`` used to crash on ``properties.get(...)``;
        the type-guard now falls through to the SARIF level fallback."""
        doc = _sarif({"level": "error", "properties": 42})
        tripped, _ = gate.evaluate(doc, "high")
        assert tripped is True  # falls back to level=error → rank 3

    def test_non_string_severity_no_crash(self, gate: Any) -> None:
        """``severity: 42`` used to crash on ``int.strip()``; the
        type-guard now falls through to the SARIF level fallback."""
        doc = _sarif({"level": "error", "properties": {"severity": 42}})
        tripped, _ = gate.evaluate(doc, "high")
        assert tripped is True

    def test_non_string_level_no_crash(self, gate: Any) -> None:
        doc = _sarif({"level": 42})
        tripped, _ = gate.evaluate(doc, "medium")
        # Non-string level → falls back to "warning" default → rank 2.
        assert tripped is True

    def test_empty_runs(self, gate: Any) -> None:
        assert gate.evaluate({"runs": []}, "critical") == (False, 0)

    def test_empty_results(self, gate: Any) -> None:
        assert gate.evaluate({"runs": [{"results": []}]}, "critical") == (False, 0)


class TestEvaluateGateExitsOnFirstHit:
    """Once a result trips, evaluation short-circuits — the second run
    is never inspected. Confirms the documented exit-on-first behavior."""

    def test_short_circuits_on_first_trip(self, gate: Any) -> None:
        # First run trips; second has lower severity that wouldn't gate.
        doc = {
            "runs": [
                {"results": [{"level": "error", "properties": {"severity": "CRITICAL"}}]},
                {"results": [{"level": "note"}]},
            ]
        }
        tripped, highest = gate.evaluate(doc, "high")
        assert tripped is True
        assert highest == 4  # caught the CRITICAL, didn't reach the note


class TestMainCLI:
    """End-to-end through ``main()`` — covers the I/O paths the pure
    ``evaluate`` cannot reach (missing file, malformed JSON, env-var
    handoff)."""

    def test_threshold_never_short_circuits_without_reading_file(
        self, gate: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``ADVISOR_FAIL_ON=never`` must exit 0 BEFORE opening the
        SARIF — so the gate is a true no-op even if the file is broken."""
        monkeypatch.setenv("ADVISOR_FAIL_ON", "never")
        broken = tmp_path / "broken.sarif"
        broken.write_text("not json {{{", encoding="utf-8")
        assert gate.main([str(broken)]) == 0

    def test_missing_file_exits_2(
        self,
        gate: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("ADVISOR_FAIL_ON", "high")
        missing = tmp_path / "nope.sarif"
        rc = gate.main([str(missing)])
        assert rc == gate.READ_ERROR_EXIT_CODE
        err = capsys.readouterr().out
        assert "cannot read" in err

    def test_malformed_json_exits_2(
        self,
        gate: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("ADVISOR_FAIL_ON", "high")
        broken = tmp_path / "broken.sarif"
        broken.write_text("definitely not json", encoding="utf-8")
        rc = gate.main([str(broken)])
        assert rc == gate.READ_ERROR_EXIT_CODE
        out = capsys.readouterr().out
        assert "cannot read" in out

    def test_tripped_exits_4_with_annotation(
        self,
        gate: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("ADVISOR_FAIL_ON", "critical")
        sarif = tmp_path / "x.sarif"
        sarif.write_text(
            json.dumps(_sarif({"level": "error", "properties": {"severity": "CRITICAL"}})),
            encoding="utf-8",
        )
        rc = gate.main([str(sarif)])
        assert rc == gate.GATE_TRIPPED_EXIT_CODE
        out = capsys.readouterr().out
        assert "::error::" in out
        assert "fail-on=critical" in out

    def test_clean_sarif_exits_0(
        self, gate: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("ADVISOR_FAIL_ON", "high")
        sarif = tmp_path / "clean.sarif"
        sarif.write_text(
            json.dumps(_sarif({"level": "note", "properties": {"severity": "LOW"}})),
            encoding="utf-8",
        )
        assert gate.main([str(sarif)]) == 0

    def test_default_sarif_path(
        self,
        gate: Any,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """No positional arg → defaults to ``advisor.sarif`` in CWD,
        matching the workflow's ``python scripts/sarif_gate.py`` call."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ADVISOR_FAIL_ON", "high")
        (tmp_path / "advisor.sarif").write_text(
            json.dumps(_sarif({"level": "note"})), encoding="utf-8"
        )
        assert gate.main([]) == 0


class TestSarifWriterIntegration:
    """Cross-check: the gate must correctly read SARIF produced by
    ``advisor/sarif.py``. Builds a real SARIF doc via the writer and
    feeds it through the gate to confirm the field shape agrees."""

    def test_advisor_sarif_critical_finding_trips_critical_gate(
        self, gate: Any, tmp_path: Path
    ) -> None:
        from advisor.sarif import findings_to_sarif
        from advisor.verify import Finding

        finding = Finding(
            file_path=str(tmp_path / "auth.py:42"),
            severity="CRITICAL",
            description="hardcoded API key",
            evidence="line 42: TOKEN = 'abc'",
            fix="read from env",
        )
        # Ensure target dir contains the finding's file so the SARIF
        # writer's containment check passes (it does NOT require the
        # file to exist on disk for relative paths).
        (tmp_path / "auth.py").write_text("# noop", encoding="utf-8")
        doc = findings_to_sarif([finding], tool_version="test", target_dir=tmp_path)
        tripped, _ = gate.evaluate(doc, "critical")
        assert tripped is True

    def test_advisor_sarif_high_finding_does_not_trip_critical_gate(
        self, gate: Any, tmp_path: Path
    ) -> None:
        """The whole point of preferring ``properties.severity``: a HIGH
        result must NOT trip ``fail-on=critical`` even though both
        emit SARIF ``error``."""
        from advisor.sarif import findings_to_sarif
        from advisor.verify import Finding

        finding = Finding(
            file_path=str(tmp_path / "session.py:10"),
            severity="HIGH",
            description="weak session id entropy",
            evidence="line 10",
            fix="use secrets.token_urlsafe",
        )
        (tmp_path / "session.py").write_text("# noop", encoding="utf-8")
        doc = findings_to_sarif([finding], tool_version="test", target_dir=tmp_path)
        tripped, _ = gate.evaluate(doc, "critical")
        assert tripped is False
        # But fail-on=high SHOULD trip.
        assert gate.evaluate(doc, "high")[0] is True


# Sanity: the script must be importable as a top-level module too —
# ``python scripts/sarif_gate.py`` and ``import sarif_gate`` (when the
# scripts dir is on sys.path) should both work. The CI workflow uses
# the first form; tooling that bundles the script might use the second.
def test_script_is_self_contained(gate: Any) -> None:
    """The gate has no imports from the ``advisor`` package — it lives
    in scripts/ specifically so the workflow doesn't drag in the whole
    package import graph for a 60-line SARIF lint."""
    # The fixture loads the gate; if any of its imports needed advisor
    # the load would have failed. Confirm by reading the source.
    text = _SCRIPT_PATH.read_text(encoding="utf-8")
    assert "import advisor" not in text
    assert "from advisor" not in text
