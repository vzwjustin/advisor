"""Tests for advisor.sarif — SARIF 2.1.0 emitter."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings
from hypothesis import strategies as st

from advisor.sarif import (
    SARIF_SCHEMA_URI,
    SARIF_VERSION,
    findings_to_sarif,
    synthesize_rule_id,
)
from advisor.verify import Finding


def _make_finding(
    *,
    file_path: str = "src/auth.py:42",
    severity: str = "HIGH",
    description: str = "Hardcoded API key",
    evidence: str = "line 42",
    fix: str = "use env var",
    rule_id: str | None = None,
) -> Finding:
    return Finding(
        file_path=file_path,
        severity=severity,
        description=description,
        evidence=evidence,
        fix=fix,
        rule_id=rule_id,
    )


class TestRoundTrip:
    def test_three_findings_shape(self, tmp_path: Path) -> None:
        findings = [
            _make_finding(severity="CRITICAL", description="hardcoded secret"),
            _make_finding(severity="MEDIUM", description="unvalidated input"),
            _make_finding(severity="LOW", description="verbose logging"),
        ]
        doc = findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)

        assert doc["$schema"] == SARIF_SCHEMA_URI
        assert doc["version"] == SARIF_VERSION
        assert len(doc["runs"]) == 1
        run = doc["runs"][0]
        assert run["tool"]["driver"]["name"] == "advisor"
        assert run["tool"]["driver"]["version"] == "0.5.0"
        assert len(run["results"]) == 3
        assert len(run["tool"]["driver"]["rules"]) == 3


class TestSeverityMapping:
    @pytest.mark.parametrize(
        ("severity", "expected_level"),
        [
            ("CRITICAL", "error"),
            ("HIGH", "error"),
            ("MEDIUM", "warning"),
            ("LOW", "note"),
        ],
    )
    def test_maps_to_sarif_level(self, tmp_path: Path, severity: str, expected_level: str) -> None:
        findings = [_make_finding(severity=severity)]
        doc = findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)
        assert doc["runs"][0]["results"][0]["level"] == expected_level

    def test_unknown_severity_falls_back_to_warning(self, tmp_path: Path) -> None:
        findings = [_make_finding(severity="UNKNOWN_LEVEL")]
        doc = findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)
        assert doc["runs"][0]["results"][0]["level"] == "warning"


class TestRuleIdSynthesis:
    def test_missing_rule_id_gets_synthesized(self, tmp_path: Path) -> None:
        findings = [_make_finding(rule_id=None, severity="HIGH", description="XSS")]
        doc = findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)
        rule_id = doc["runs"][0]["results"][0]["ruleId"]
        assert rule_id.startswith("advisor/high/")

    def test_same_desc_severity_yields_same_rule_id(self) -> None:
        a = synthesize_rule_id("HIGH", "hardcoded secret in config")
        b = synthesize_rule_id("HIGH", "hardcoded secret in config")
        assert a == b

    def test_different_desc_yields_different_rule_id(self) -> None:
        a = synthesize_rule_id("HIGH", "hardcoded secret")
        b = synthesize_rule_id("HIGH", "sql injection")
        assert a != b

    def test_case_insensitive_severity(self) -> None:
        a = synthesize_rule_id("HIGH", "issue")
        b = synthesize_rule_id("high", "issue")
        assert a == b

    def test_custom_prefix(self) -> None:
        rid = synthesize_rule_id("HIGH", "issue", prefix="custom")
        assert rid.startswith("custom/high/")

    def test_explicit_rule_id_preserved(self, tmp_path: Path) -> None:
        findings = [_make_finding(rule_id="custom/rule/12345", severity="HIGH")]
        doc = findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)
        assert doc["runs"][0]["results"][0]["ruleId"] == "custom/rule/12345"


class TestPathHandling:
    def test_relative_path_preserved(self, tmp_path: Path) -> None:
        findings = [_make_finding(file_path="src/auth.py:42")]
        doc = findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)
        loc = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
        assert loc["artifactLocation"]["uri"] == "src/auth.py"
        assert loc["region"]["startLine"] == 42

    def test_absolute_inside_target_is_relativized(self, tmp_path: Path) -> None:
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        abs_file = src_dir / "auth.py"
        abs_file.write_text("# stub\n")
        findings = [_make_finding(file_path=f"{abs_file}:10")]
        doc = findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)
        loc = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
        assert loc["artifactLocation"]["uri"] == "src/auth.py"
        assert loc["region"]["startLine"] == 10

    def test_absolute_outside_target_raises(self, tmp_path: Path) -> None:
        other = tmp_path.parent / "outside_target.py"
        findings = [_make_finding(file_path=str(other))]
        with pytest.raises(ValueError, match="outside target_dir"):
            findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)

    def test_path_without_line_number(self, tmp_path: Path) -> None:
        findings = [_make_finding(file_path="src/auth.py")]
        doc = findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)
        loc = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
        assert loc["artifactLocation"]["uri"] == "src/auth.py"
        assert "region" not in loc


class TestSchemaShape:
    """Light structural validation without pulling in a jsonschema dep.

    Verifies the handful of fields GitHub Code Scanning requires.
    """

    def test_has_required_top_level_keys(self, tmp_path: Path) -> None:
        findings = [_make_finding()]
        doc = findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)
        assert "$schema" in doc
        assert doc["version"] == SARIF_VERSION
        assert isinstance(doc["runs"], list)

    def test_run_has_tool_driver_and_results(self, tmp_path: Path) -> None:
        findings = [_make_finding()]
        doc = findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)
        run = doc["runs"][0]
        assert "tool" in run
        assert "driver" in run["tool"]
        assert "results" in run
        assert run["originalUriBaseIds"]["%SRCROOT%"]["uri"].startswith("file://")

    def test_empty_findings_produces_valid_run(self, tmp_path: Path) -> None:
        doc = findings_to_sarif([], tool_version="0.5.0", target_dir=tmp_path)
        run = doc["runs"][0]
        assert run["results"] == []
        assert run["tool"]["driver"]["rules"] == []


class TestUriEncoding:
    """SARIF ``artifactLocation.uri`` is a uri-reference per RFC 3986;
    file paths with spaces or reserved chars must be percent-encoded
    before they land in the output, or downstream consumers (notably
    GitHub Code Scanning) misinterpret the path. ``/`` must be
    preserved so the relative-path structure stays intact.
    """

    @staticmethod
    def _result_uri(doc: dict[str, object]) -> str:
        runs = doc["runs"]
        assert isinstance(runs, list)
        results = runs[0]["results"]  # type: ignore[index]
        return results[0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]

    def test_space_is_percent_encoded(self, tmp_path: Path) -> None:

        f = _make_finding(file_path="src/file with space.py:5", description="x")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        assert self._result_uri(doc) == "src/file%20with%20space.py"

    def test_reserved_chars_percent_encoded(self, tmp_path: Path) -> None:
        f = _make_finding(file_path="src/a#b?c.py:1", description="x")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        # ``#`` would otherwise be interpreted as the URI fragment;
        # ``?`` would start the query string. Both must encode.
        uri = self._result_uri(doc)
        assert "%23" in uri  # '#'
        assert "%3F" in uri  # '?'

    def test_slash_preserved(self, tmp_path: Path) -> None:
        f = _make_finding(file_path="src/sub/auth.py:1", description="x")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        assert self._result_uri(doc) == "src/sub/auth.py"


class TestParseFilePathWhitespace:
    """``_parse_file_path`` must strip embedded whitespace (newline, tab,
    CR) before splitting on ``:``. A path like ``"src/foo.py\\n:42"``
    used to survive into the SARIF ``artifactLocation.uri`` and break
    path-equality matching for GitHub Code Scanning."""

    def test_embedded_newline_dropped(self) -> None:
        from advisor.sarif import _parse_file_path

        assert _parse_file_path("src/foo.py\n:42") == ("src/foo.py", 42)

    def test_embedded_tab_dropped(self) -> None:
        from advisor.sarif import _parse_file_path

        assert _parse_file_path("src/foo.py\t:42") == ("src/foo.py", 42)

    def test_embedded_cr_dropped(self) -> None:
        from advisor.sarif import _parse_file_path

        assert _parse_file_path("src/foo\rpath.py:42") == ("src/foopath.py", 42)

    def test_embedded_nul_dropped(self) -> None:
        """NUL bytes survive the original whitespace strip and confuse
        SARIF consumers that treat the URI as a C string (truncating at
        the first NUL). Drop them up-front."""
        from advisor.sarif import _parse_file_path

        assert _parse_file_path("src\x00/foo.py:42") == ("src/foo.py", 42)


class TestShortDescriptionWhitespace:
    """``shortDescription`` is rendered single-line in GitHub Code
    Scanning's rule list. ``_short_text`` must collapse newlines / CR /
    tabs to single spaces so an embedded newline doesn't survive into
    the rendered UI.
    """

    def test_newline_collapsed_to_space(self, tmp_path: Path) -> None:
        f = _make_finding(description="line one\nline two")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        run = doc["runs"][0]  # type: ignore[index]
        rules = run["tool"]["driver"]["rules"]
        assert "\n" not in rules[0]["shortDescription"]["text"]
        assert rules[0]["shortDescription"]["text"] == "line one line two"

    def test_crlf_collapsed_to_space(self, tmp_path: Path) -> None:
        f = _make_finding(description="a\r\nb\tc")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        run = doc["runs"][0]  # type: ignore[index]
        text = run["tool"]["driver"]["rules"][0]["shortDescription"]["text"]
        assert text == "a b c"


class TestCLIIntegration:
    def test_plan_writes_valid_sarif(self, tmp_path: Path, monkeypatch) -> None:
        import subprocess
        import sys

        fixture = tmp_path / "mini"
        fixture.mkdir()
        (fixture / "auth.py").write_text("API_KEY = 'abc'\n")

        sarif_out = tmp_path / "out.sarif"
        # Run via the installed console script so CLI wiring is exercised.
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "advisor",
                "plan",
                str(fixture),
                "--sarif",
                str(sarif_out),
                "--min-priority",
                "1",
            ],
            capture_output=True,
            text=True,
            cwd=str(tmp_path),
            env={**__import__("os").environ, "ADVISOR_NO_NUDGE": "1"},
        )
        assert result.returncode == 0, result.stderr
        assert sarif_out.exists()
        doc = json.loads(sarif_out.read_text())
        assert doc["version"] == SARIF_VERSION
        assert doc["runs"][0]["results"] == []


@settings(deadline=1000, max_examples=50)
@given(
    severity=st.sampled_from(["CRITICAL", "HIGH", "MEDIUM", "LOW"]),
    description=st.text(min_size=0, max_size=200),
)
def test_fuzz_synthesize_rule_id_is_stable(severity: str, description: str) -> None:
    a = synthesize_rule_id(severity, description)
    b = synthesize_rule_id(severity, description)
    assert a == b
    assert a.startswith(f"advisor/{severity.lower()}/")
