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
    _strip_controls,
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


class TestPartialFingerprints:
    """``partialFingerprints.primaryLocationLineHash`` is GitHub Code
    Scanning's per-result dedup key. Two distinct findings (same rule,
    different file or different line) MUST produce distinct fingerprints,
    or GHCS collapses them into a single alert and half the findings
    disappear from the UI.

    Regression net for B1 (fingerprint previously keyed only on rule_id,
    which is file-agnostic by design).
    """

    @staticmethod
    def _fp(doc: dict[str, object], idx: int = 0) -> str:
        runs = doc["runs"]
        assert isinstance(runs, list)
        results = runs[0]["results"]  # type: ignore[index]
        return results[idx]["partialFingerprints"]["primaryLocationLineHash"]

    def test_same_rule_different_files_distinct_fingerprints(self, tmp_path: Path) -> None:
        findings = [
            _make_finding(file_path="src/a.py:5", severity="HIGH", description="same bug"),
            _make_finding(file_path="src/b.py:10", severity="HIGH", description="same bug"),
        ]
        doc = findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)
        # Same rule_id (same severity + description) means they SHOULD
        # group as one rule, but the per-result fingerprints MUST differ.
        assert doc["runs"][0]["results"][0]["ruleId"] == doc["runs"][0]["results"][1]["ruleId"]
        assert self._fp(doc, 0) != self._fp(doc, 1)

    def test_same_rule_same_file_different_lines_distinct_fingerprints(
        self, tmp_path: Path
    ) -> None:
        findings = [
            _make_finding(file_path="src/a.py:5", severity="HIGH", description="same bug"),
            _make_finding(file_path="src/a.py:99", severity="HIGH", description="same bug"),
        ]
        doc = findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)
        assert self._fp(doc, 0) != self._fp(doc, 1)

    def test_file_level_finding_distinct_from_line_zero(self, tmp_path: Path) -> None:
        """No-line finding (``path``) must have a distinct fingerprint
        from a ``path:0`` finding even though both clamp to startLine=1
        in the region block. Defense-in-depth — runners shouldn't emit
        ``:0`` but if they do, dedup must still be correct."""
        findings = [
            _make_finding(file_path="src/a.py", severity="HIGH", description="bug"),
            _make_finding(file_path="src/a.py:0", severity="HIGH", description="bug"),
        ]
        doc = findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)
        assert self._fp(doc, 0) != self._fp(doc, 1)

    def test_identical_findings_share_fingerprint(self, tmp_path: Path) -> None:
        """Run-to-run stability: re-scanning the SAME finding (same file,
        same line, same rule) MUST produce the same fingerprint so GHCS
        recognizes the existing alert instead of opening a duplicate."""
        f1 = _make_finding(file_path="src/a.py:5", severity="HIGH", description="bug")
        f2 = _make_finding(file_path="src/a.py:5", severity="HIGH", description="bug")
        doc = findings_to_sarif([f1, f2], tool_version="0.5.0", target_dir=tmp_path)
        assert self._fp(doc, 0) == self._fp(doc, 1)


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

        assert _parse_file_path("src/foo.py\n:42") == ("src/foo.py", 42, None, None)

    def test_embedded_tab_dropped(self) -> None:
        from advisor.sarif import _parse_file_path

        assert _parse_file_path("src/foo.py\t:42") == ("src/foo.py", 42, None, None)

    def test_embedded_cr_dropped(self) -> None:
        from advisor.sarif import _parse_file_path

        assert _parse_file_path("src/foo\rpath.py:42") == ("src/foopath.py", 42, None, None)

    def test_embedded_nul_dropped(self) -> None:
        """NUL bytes survive the original whitespace strip and confuse
        SARIF consumers that treat the URI as a C string (truncating at
        the first NUL). Drop them up-front."""
        from advisor.sarif import _parse_file_path

        assert _parse_file_path("src\x00/foo.py:42") == ("src/foo.py", 42, None, None)


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


class TestControlCharSanitization:
    """LLM-emitted text can contain NUL / C0 controls (binary evidence quotes,
    prompt-injection, etc.). ``json.dumps`` escapes 0x00–0x1F to ``\\u00XX``
    so the file stays valid JSON, but several SARIF consumers (notably
    GitHub Code Scanning historically) treat string values as C strings and
    silently truncate at the first NUL — corrupting rule grouping and
    dropping evidence from the UI. Strip at the source.
    """

    def test_nul_stripped_from_short_description(self, tmp_path: Path) -> None:
        f = _make_finding(description="auth bypass\x00<dropped tail>")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        rules = doc["runs"][0]["tool"]["driver"]["rules"]  # type: ignore[index]
        text = rules[0]["shortDescription"]["text"]
        assert "\x00" not in text
        # Tail must survive — the bug a NUL-truncating consumer would cause.
        assert "<dropped tail>" in text

    def test_nul_stripped_from_full_description(self, tmp_path: Path) -> None:
        f = _make_finding(description="line one\nline two\x00after-nul")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        rules = doc["runs"][0]["tool"]["driver"]["rules"]  # type: ignore[index]
        text = rules[0]["fullDescription"]["text"]
        assert "\x00" not in text
        # Block fields preserve real newlines — only controls are stripped.
        assert "\n" in text
        assert "after-nul" in text

    def test_nul_stripped_from_help_text(self, tmp_path: Path) -> None:
        f = _make_finding(fix="use env\x00var")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        rules = doc["runs"][0]["tool"]["driver"]["rules"]  # type: ignore[index]
        text = rules[0]["help"]["text"]
        assert "\x00" not in text
        assert text == "use envvar"

    def test_nul_stripped_from_message_text(self, tmp_path: Path) -> None:
        f = _make_finding(description="\x01\x02alert\x00here")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        results = doc["runs"][0]["results"]  # type: ignore[index]
        text = results[0]["message"]["text"]
        assert "\x00" not in text
        assert "\x01" not in text
        assert "\x02" not in text
        assert "alert" in text and "here" in text

    def test_nul_stripped_from_properties_evidence(self, tmp_path: Path) -> None:
        f = _make_finding(evidence="grep showed\x00something")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        props = doc["runs"][0]["results"][0]["properties"]  # type: ignore[index]
        assert "\x00" not in props["evidence"]
        assert props["evidence"] == "grep showedsomething"

    def test_nul_stripped_from_properties_fix(self, tmp_path: Path) -> None:
        f = _make_finding(fix="apply\x00patch")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        props = doc["runs"][0]["results"][0]["properties"]  # type: ignore[index]
        assert "\x00" not in props["fix"]
        assert props["fix"] == "applypatch"

    def test_del_byte_stripped(self, tmp_path: Path) -> None:
        """U+007F (DEL) is technically printable in some terminals but is a
        control character per Unicode and breaks the same consumers."""
        f = _make_finding(description="contains\x7fdel")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        rules = doc["runs"][0]["tool"]["driver"]["rules"]  # type: ignore[index]
        assert "\x7f" not in rules[0]["fullDescription"]["text"]

    def test_block_fields_keep_tab_and_crlf(self, tmp_path: Path) -> None:
        """Block-rendered fields preserve \\t, \\n, \\r so legitimately
        multi-line descriptions still render correctly downstream."""
        f = _make_finding(description="a\tb\nc\r\nd")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        rules = doc["runs"][0]["tool"]["driver"]["rules"]  # type: ignore[index]
        results = doc["runs"][0]["results"]  # type: ignore[index]
        # fullDescription / message.text keep block whitespace.
        for text in (rules[0]["fullDescription"]["text"], results[0]["message"]["text"]):
            assert "\t" in text
            assert "\n" in text
            assert "\r" in text

    def test_short_description_strips_all_whitespace_controls(self, tmp_path: Path) -> None:
        """``shortDescription`` is single-line in the GitHub UI. Existing
        ``_short_text`` collapses python-whitespace via ``str.split()``;
        adding the strip ensures NUL (which is NOT whitespace) is also
        removed before the collapse is observable.
        """
        f = _make_finding(description="x\x00y\tz")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        rules = doc["runs"][0]["tool"]["driver"]["rules"]  # type: ignore[index]
        text = rules[0]["shortDescription"]["text"]
        assert "\x00" not in text
        # ``_short_text`` already collapsed \t to a space via split/join.
        assert "\t" not in text

    def test_unicode_above_ascii_preserved(self, tmp_path: Path) -> None:
        """Non-ASCII (U+0080+) must survive — only C0 + DEL are stripped."""
        f = _make_finding(description="café — résumé €", fix="naïve fix")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        rules = doc["runs"][0]["tool"]["driver"]["rules"]  # type: ignore[index]
        results = doc["runs"][0]["results"]  # type: ignore[index]
        assert "café" in rules[0]["fullDescription"]["text"]
        assert "résumé" in rules[0]["fullDescription"]["text"]
        assert "naïve" in rules[0]["help"]["text"]
        assert "café" in results[0]["message"]["text"]


class TestEmptyPathPostParseSkip:
    """B2 regression: a ``file_path = ":42"`` survives the pre-parse
    non-empty check (its ``strip()`` is non-empty) but ``_parse_file_path``
    peels the leading colon and returns an empty path, which then
    resolves to ``"."`` and emits a SARIF result pointing at %SRCROOT%
    itself. Skip such results."""

    def test_colon_only_line_skipped(self, tmp_path: Path) -> None:
        f = _make_finding(file_path=":42")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        assert doc["runs"][0]["results"] == []

    def test_dot_only_path_skipped(self, tmp_path: Path) -> None:
        """``"."`` resolves to %SRCROOT% itself — same misleading shape."""
        f = _make_finding(file_path=".")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        assert doc["runs"][0]["results"] == []


class TestNegativeLineClamped:
    """B3 regression: a ``file_path="foo.py:-5"`` used to leave ``-5``
    embedded in the URI as ``%3A-5``. The parser now peels the leading
    ``-`` as part of the numeric token and the existing startLine clamp
    handles the negative value."""

    def test_negative_line_clamped_to_one(self, tmp_path: Path) -> None:
        f = _make_finding(file_path="foo.py:-5")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        loc = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]
        assert loc["artifactLocation"]["uri"] == "foo.py"
        assert loc["region"]["startLine"] == 1


class TestTargetResolveOnce:
    """B4 regression: ``target_dir.resolve()`` previously ran once per
    finding (O(N) syscalls). Now resolved once outside the loop."""

    def test_resolve_called_once_for_batch(self, tmp_path: Path, monkeypatch) -> None:
        # Wrap Path.resolve to count invocations on our specific target_dir.
        original_resolve = Path.resolve
        call_count = {"n": 0}

        def counting_resolve(self: Path, *args, **kwargs) -> Path:
            if self == tmp_path:
                call_count["n"] += 1
            return original_resolve(self, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", counting_resolve)

        findings = [_make_finding(file_path=f"src/f{i}.py:1") for i in range(10)]
        findings_to_sarif(findings, tool_version="0.5.0", target_dir=tmp_path)

        # findings_to_sarif resolves once at loop top + once for the
        # originalUriBaseIds URI = 2 calls total. The OLD code resolved
        # once per finding = 11 calls for this batch.
        assert call_count["n"] <= 2, (
            f"expected target_dir.resolve() ≤2 times for 10 findings, got {call_count['n']}"
        )


class TestColumnRegionEmitted:
    """F1: ``_parse_file_path`` extracts trailing ``:col[:end-col]``
    already; SARIF 2.1.0 ``region`` supports ``startColumn`` /
    ``endColumn``. Emit them when the runner provided them so GHCS can
    highlight the precise span instead of the whole line."""

    def test_line_col_endcol_emitted(self, tmp_path: Path) -> None:
        f = _make_finding(file_path="foo.py:10:5:15")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        region = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
        assert region["startLine"] == 10
        assert region["startColumn"] == 5
        assert region["endColumn"] == 15

    def test_line_col_only_no_endcol(self, tmp_path: Path) -> None:
        f = _make_finding(file_path="foo.py:10:5")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        region = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
        assert region["startLine"] == 10
        assert region["startColumn"] == 5
        assert "endColumn" not in region

    def test_line_only_no_column_keys(self, tmp_path: Path) -> None:
        f = _make_finding(file_path="foo.py:10")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        region = doc["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["region"]
        assert region["startLine"] == 10
        assert "startColumn" not in region
        assert "endColumn" not in region


class TestSeverityTag:
    """F2: rule ``properties.tags`` exposes the severity bucket so the
    GHCS UI filter pane can group alerts without parsing custom result
    fields."""

    def test_high_severity_tag_present(self, tmp_path: Path) -> None:
        f = _make_finding(severity="HIGH")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        tags = doc["runs"][0]["tool"]["driver"]["rules"][0]["properties"]["tags"]
        assert "severity:high" in tags

    def test_critical_severity_tag_present(self, tmp_path: Path) -> None:
        f = _make_finding(severity="CRITICAL", description="another bug")
        doc = findings_to_sarif([f], tool_version="0.5.0", target_dir=tmp_path)
        tags = doc["runs"][0]["tool"]["driver"]["rules"][0]["properties"]["tags"]
        assert "severity:critical" in tags


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


def test_strip_controls_strips_bidi_on_both_paths():
    # Bidi formatting / override / isolate / mark code points must be
    # dropped on BOTH the inline path (``keep_block_whitespace=False``,
    # used for ``shortDescription`` / ``message.text``) and the block
    # path (``keep_block_whitespace=True``, used by ``pr_comment._sanitize``
    # to preserve newlines inside Evidence / Fix). Without the block-path
    # branch a Finding description renders into a GitHub PR comment that
    # visually misrepresents the named file or severity to a human
    # reviewer — the "trojan source" attack class.
    assert _strip_controls("text‮evil", keep_block_whitespace=False) == "textevil"
    assert _strip_controls("text‮evil", keep_block_whitespace=True) == "textevil"


# ---------------------------------------------------------------------------
# Regression tests for Wave 3 — H1, H2, H3
# ---------------------------------------------------------------------------


def test_parse_file_path_rejects_double_minus(tmp_path: Path) -> None:
    """H1: _parse_file_path must not raise ValueError on 'path:--5'.

    Previously _is_int_token stripped ALL leading '-' via lstrip('-'), so
    '--5'.lstrip('-') == '5' (isdigit True), '--5' was accepted as a numeric
    token, and int('--5') raised ValueError aborting SARIF emission.
    """
    from advisor.sarif import _parse_file_path

    # Must not raise; double-minus token is not a valid int token and
    # should be left in the path component.
    path, line, _col, _end = _parse_file_path("src/foo.py:--5")
    # '--5' is not a valid int-token after the fix, so no line is extracted
    # and the token stays embedded in the path.
    assert line is None
    assert "--5" in path or path == "src/foo.py"


def test_synthesize_rule_id_handles_lone_surrogates() -> None:
    """H2 (sarif site): synthesize_rule_id must not raise on lone surrogates."""
    result = synthesize_rule_id("HIGH", "description with surrogate \ud800")
    assert result.startswith("advisor/high/")


def test_resolve_relative_handles_oserror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H3: _resolve_relative must re-raise OSError from Path.resolve() as ValueError.

    Path.resolve() can raise OSError on symlink loops (ELOOP) or permission
    errors (EACCES). Previously only ValueError was caught, so OSError
    propagated uncaught and aborted SARIF emission.
    """
    import pathlib

    from advisor.sarif import _resolve_relative

    target_resolved = tmp_path.resolve()  # resolve before patch

    def raise_oserror(self: pathlib.Path, *args: object, **kwargs: object) -> pathlib.Path:
        raise OSError("simulated ELOOP")

    monkeypatch.setattr(pathlib.Path, "resolve", raise_oserror)

    # Should raise ValueError (not OSError) because the fix catches both.
    with pytest.raises(ValueError):
        _resolve_relative("/abs/src/auth.py", tmp_path, target_resolved=target_resolved)

