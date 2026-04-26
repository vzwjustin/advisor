"""Property-based fuzz tests for parser, renderer, and glob translation.

These extend the existing per-feature fuzz tests with broader invariants:

* ``format_pr_comment`` must never emit unescaped script/iframe/on-attribute
  payloads, must produce balanced ``<details>`` markup, and must respect
  the GitHub body cap regardless of input.
* ``parse_findings_from_text`` must round-trip every well-formed
  ``Finding`` produced by ``format_findings_block``.
* ``_double_star_to_regex`` must either compile to a regex or fall back
  to the inert ``r"$.^"`` matcher — never raise from the public glob
  caller (``_compile_ignore_patterns``).

Hypothesis is an optional test dep; skip if unavailable.
"""

from __future__ import annotations

import re

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from advisor.pr_comment import _GITHUB_BODY_LIMIT, format_pr_comment
from advisor.rank import _compile_ignore_patterns
from advisor.verify import (
    Finding,
    format_findings_block,
    parse_findings_from_text,
)

# Test budget: keep total wall-clock under ~2s on slow CI by capping
# example count and disabling per-example deadlines (Windows runners
# routinely miss 200 ms on first call). ``derandomize=True`` makes any
# discovered failure reproducible across machines.
_FUZZ_SETTINGS = settings(
    max_examples=150,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)


# ─────────────────────────────────────────────────────────────────────
# format_pr_comment HTML safety invariants
# ─────────────────────────────────────────────────────────────────────


# A Finding strategy with intentionally hostile field content. ``severity``
# is a free string here (not the canonical 4-tuple) because format_pr_comment
# must be robust to malformed severities flowing in from the audit transcript
# parser, which has no allowlist.
_finding_strategy = st.builds(
    Finding,
    file_path=st.text(min_size=1, max_size=80),
    severity=st.text(min_size=0, max_size=40),
    description=st.text(min_size=0, max_size=300),
    evidence=st.text(min_size=0, max_size=300),
    fix=st.text(min_size=0, max_size=300),
    rule_id=st.one_of(st.none(), st.text(min_size=1, max_size=80)),
)


# Patterns that must NEVER appear in the rendered output as raw HTML —
# any of these would mean a user field broke out of escaping. We check
# the literal lowercase substring; HTML-escaped forms (``&lt;script&gt;``)
# are fine and don't trip these patterns.
_INJECTION_PATTERNS = (
    "<script",
    "<iframe",
    "<img ",
    "<svg",
    "<object",
    "<embed",
    "<style",
    " onerror=",
    " onclick=",
    " onload=",
    "javascript:",
)


def _strip_evidence_blocks(rendered: str) -> str:
    """Return rendered output with the evidence fenced code blocks removed.

    Evidence sits inside a ``` fenced code block, where GitHub renders
    content as literal text — so HTML-shaped strings appear verbatim by
    design (HTML-escaping there would surface ``&lt;`` noise to readers).
    For HTML-injection invariants, we strip those blocks before scanning.
    """
    # Greedy across newlines. The wrapper emits exactly one ``` open and
    # one ``` close per finding for evidence; user-supplied triple-backtick
    # runs in the evidence body are pre-replaced with ''' so they can't
    # mis-balance the fence.
    return re.sub(r"```\n.*?\n```", "[EVIDENCE_BLOCK]", rendered, flags=re.DOTALL)


@_FUZZ_SETTINGS
@given(st.lists(_finding_strategy, min_size=0, max_size=12))
def test_pr_comment_never_emits_unescaped_html_payloads(findings: list[Finding]) -> None:
    out = format_pr_comment(findings)
    out_no_evidence = _strip_evidence_blocks(out).lower()
    for needle in _INJECTION_PATTERNS:
        assert needle not in out_no_evidence, (
            f"pr_comment leaked unescaped {needle!r} into HTML body (input findings: {findings!r})"
        )


@_FUZZ_SETTINGS
@given(st.lists(_finding_strategy, min_size=1, max_size=10))
def test_pr_comment_details_tags_are_balanced(findings: list[Finding]) -> None:
    """Every rendered finding contributes exactly one ``<details>`` and one
    ``</details>`` to the output. User-supplied ``<details>``-shaped text is
    HTML-entity-escaped (or fence-neutralized inside evidence) so it can't
    create extra opens/closes that GitHub would parse.
    """
    out = format_pr_comment(findings)
    open_count = out.lower().count("<details>")
    close_count = out.lower().count("</details>")
    assert open_count == close_count, (
        f"unbalanced <details>/</details> ({open_count} vs {close_count}); input: {findings!r}"
    )
    # The rendered count is at most len(findings) — could be lower under
    # body-cap truncation.
    assert open_count <= len(findings)


@_FUZZ_SETTINGS
@given(st.lists(_finding_strategy, min_size=0, max_size=20))
def test_pr_comment_respects_github_body_cap(findings: list[Finding]) -> None:
    """The truncation logic must always emit a body shorter than the
    GitHub PR body limit, regardless of how large any single finding is.
    """
    out = format_pr_comment(findings)
    assert len(out) < _GITHUB_BODY_LIMIT, f"output {len(out)} chars >= cap {_GITHUB_BODY_LIMIT}"


# ─────────────────────────────────────────────────────────────────────
# parse_findings_from_text round-trip with format_findings_block
# ─────────────────────────────────────────────────────────────────────


# Reduced strategy: round-trip relies on two things the parser enforces.
#  1. Field content must not contain the literal block delimiters the
#     parser uses to split blocks (``### Finding`` headers, ``## ``
#     boundaries, the bold-key prefixes). Those are structural to the
#     format — embedding them in a value is the exact case
#     ``parse_findings_with_drift`` mitigates separately, not a
#     round-trip property the format claims to support.
#  2. Required fields, after the parser's leading/trailing strip, must
#     be non-empty. ``_dict_to_finding`` drops findings with an empty
#     required field rather than fabricating defaults. A whitespace-
#     only value (``' '``) parses as ``''`` and is dropped — that's
#     intentional, so we filter it out of the round-trip strategy.
_safe_text = st.text(
    alphabet=st.characters(
        min_codepoint=0x20,
        max_codepoint=0x7E,
        blacklist_characters="`",  # backticks are stripped by _extract_value
    ),
    min_size=1,
    max_size=80,
).filter(
    lambda s: (
        bool(s.strip())
        and not any(
            marker in s
            for marker in (
                "### Finding",
                "## ",
                "**File**",
                "**Severity**",
                "**Description**",
                "**Evidence**",
                "**Fix**",
                "**Rule**",
                "- ",
                "* ",
                "**",
            )
        )
    )
)


_round_trip_finding = st.builds(
    Finding,
    file_path=_safe_text,
    severity=_safe_text,
    description=_safe_text,
    evidence=_safe_text,
    fix=_safe_text,
    rule_id=st.one_of(st.none(), _safe_text),
)


@_FUZZ_SETTINGS
@given(st.lists(_round_trip_finding, min_size=1, max_size=8))
def test_format_findings_block_round_trips(findings: list[Finding]) -> None:
    """Findings written via ``format_findings_block`` must parse back via
    ``parse_findings_from_text`` to the same field values."""
    block = format_findings_block(findings)
    parsed = parse_findings_from_text(block)
    assert len(parsed) == len(findings), (
        f"round-trip lost or duplicated entries: in {len(findings)} → out {len(parsed)}"
    )
    for original, got in zip(findings, parsed, strict=True):
        assert got.file_path == original.file_path.strip()
        assert got.severity == original.severity.strip()
        assert got.description == original.description.strip()
        assert got.evidence == original.evidence.strip()
        assert got.fix == original.fix.strip()
        # rule_id round-trips when present; absent means parser gets None.
        if original.rule_id:
            assert got.rule_id == original.rule_id.strip()
        else:
            assert got.rule_id is None


# ─────────────────────────────────────────────────────────────────────
# _double_star_to_regex robustness via the public ignore-pattern compile
# ─────────────────────────────────────────────────────────────────────


# Glob alphabet covers wildcards (* ? [ ] !), separators (/), dot, and
# basic alphanumerics. Mixing in bracket / negation characters exercises
# the character-class branch where the helper has historically been
# fragile.
_glob_alphabet = st.text(
    alphabet=st.sampled_from("abc/.*?[]!^- "),
    min_size=1,
    max_size=24,
)


@_FUZZ_SETTINGS
@given(st.lists(_glob_alphabet, min_size=1, max_size=8))
def test_compile_ignore_patterns_never_raises(patterns: list[str]) -> None:
    """The public glob compile path must tolerate any user-authored
    ``.advisorignore`` / suppressions glob without raising — malformed
    translations fall back to the inert ``r"$.^"`` matcher per the
    documented contract.
    """
    matchers = _compile_ignore_patterns(patterns)
    assert len(matchers) == len(patterns)
    for matcher, pattern in zip(matchers, patterns, strict=True):
        # Either the glob translated successfully (recursive_re is a
        # compiled pattern) or it fell back to the inert matcher.
        # ``None`` is also valid for patterns without ``**``.
        assert matcher.pattern == pattern
