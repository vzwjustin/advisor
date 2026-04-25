"""Tests for advisor.rank module."""

import pytest

from advisor.rank import PRIORITY_KEYWORDS, RankedFile, rank_files, rank_to_prompt


class TestRankFiles:
    def test_ranks_auth_file_highest(self):
        files = ["src/utils.py", "src/auth.py", "src/models.py"]
        ranked = rank_files(files)

        assert ranked[0].path == "src/auth.py"
        assert ranked[0].priority == 5

    def test_ranks_by_content_when_reader_provided(self):
        files = ["src/app.py", "src/constants.py"]
        content = {
            "src/app.py": "from flask import request\ndef handle_login(password):",
            "src/constants.py": "MAX_RETRIES = 3\nTIMEOUT = 30",
        }
        ranked = rank_files(files, read_fn=lambda p: content[p])

        assert ranked[0].path == "src/app.py"
        assert ranked[0].priority > ranked[1].priority

    def test_skips_pycache_and_binary_files(self):
        files = [
            "src/__pycache__/app.cpython-311.pyc",
            "src/app.py",
            "assets/logo.png",
        ]
        ranked = rank_files(files)

        paths = [r.path for r in ranked]
        assert "src/app.py" in paths
        assert len(paths) == 1

    def test_skips_claude_worktrees(self):
        files = [
            ".claude/worktrees/copy/advisor/rank.py",
            "advisor/rank.py",
        ]
        ranked = rank_files(files)

        assert [r.path for r in ranked] == ["advisor/rank.py"]

    def test_returns_empty_for_empty_input(self):
        assert rank_files([]) == []

    def test_sorted_descending_by_priority(self):
        files = ["src/util.py", "src/auth.py", "src/api.py"]
        ranked = rank_files(files)

        priorities = [r.priority for r in ranked]
        assert priorities == sorted(priorities, reverse=True)

    def test_reasons_are_deduplicated(self):
        ranked = rank_files(["src/auth_token_login.py"])
        rf = ranked[0]
        assert len(rf.reasons) == len(set(rf.reasons))

    def test_immutability(self):
        ranked = rank_files(["src/auth.py"])
        rf = ranked[0]
        # frozen dataclass should raise on mutation
        with pytest.raises(AttributeError):
            rf.priority = 1  # type: ignore


class TestRankToPrompt:
    def test_formats_markdown(self):
        ranked = [
            RankedFile(path="src/auth.py", priority=5, reasons=("auth", "token")),
            RankedFile(path="src/util.py", priority=1, reasons=("util",)),
        ]
        prompt = rank_to_prompt(ranked)

        assert "## File Priority Ranking" in prompt
        assert "P5" in prompt
        assert "src/auth.py" in prompt

    def test_respects_top_n(self):
        ranked = [
            RankedFile(path=f"src/f{i}.py", priority=5 - i, reasons=("x",)) for i in range(20)
        ]
        prompt = rank_to_prompt(ranked, top_n=3)

        assert prompt.count(". **P") == 3

    def test_empty_ranked_list(self):
        prompt = rank_to_prompt([])
        assert "## File Priority Ranking" in prompt


class TestLoadAdvisorignoreWarnsOnError:
    def test_warns_when_file_contains_invalid_utf8(self, tmp_path):
        import warnings

        from advisor.rank import ADVISORIGNORE_FILENAME, load_advisorignore

        (tmp_path / ADVISORIGNORE_FILENAME).write_bytes(b"\xff\xfe invalid")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = load_advisorignore(tmp_path)

        assert result == []
        assert any(issubclass(w.category, UserWarning) for w in caught)
        assert any("could not read" in str(w.message) for w in caught)

    def test_missing_file_is_silent(self, tmp_path):
        import warnings

        from advisor.rank import load_advisorignore

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = load_advisorignore(tmp_path)

        assert result == []
        assert not any(issubclass(w.category, UserWarning) for w in caught)


class TestMatchesAnyPattern:
    def test_wildcard_pattern_does_not_match_dir_component_with_extension(self):
        from advisor.rank import _matches_any_pattern

        assert _matches_any_pattern("scripts.py/handler.txt", ["*.py"]) is False

    def test_wildcard_pattern_matches_filename(self):
        from advisor.rank import _matches_any_pattern

        assert _matches_any_pattern("src/foo.py", ["*.py"]) is True

    def test_bare_word_pattern_matches_dir_component(self):
        from advisor.rank import _matches_any_pattern

        assert _matches_any_pattern("src/tests/foo.py", ["tests"]) is True

    def test_bare_word_pattern_does_not_match_unrelated_file(self):
        from advisor.rank import _matches_any_pattern

        assert _matches_any_pattern("src/tests.py", ["tests"]) is False


class TestDoubleStarGlob:
    """``**`` recursive globs must descend into subdirs."""

    def test_double_star_matches_nested(self):
        from advisor.rank import _matches_any_pattern

        assert _matches_any_pattern("src/a/b/c/foo.py", ["src/**/*.py"]) is True

    def test_double_star_matches_direct_child(self):
        from advisor.rank import _matches_any_pattern

        assert _matches_any_pattern("src/foo.py", ["src/**/*.py"]) is True

    def test_double_star_does_not_match_different_tree(self):
        from advisor.rank import _matches_any_pattern

        assert _matches_any_pattern("tests/foo.py", ["src/**/*.py"]) is False

    def test_double_star_bracket_negation_only_no_error(self):
        # `**/[!].py` produced `[^]` (invalid regex) before the fix.
        # Must not raise re.error and must not match an unrelated file.
        from advisor.rank import _matches_any_pattern

        assert _matches_any_pattern("a/foo.py", ["**/[!].py"]) is False


@pytest.mark.parametrize(
    "priority,keyword",
    [(priority, kw) for priority, kws in PRIORITY_KEYWORDS.items() for kw in kws],
)
def test_priority_keywords_match_their_own_tier(priority, keyword):
    """Every declared keyword must rank its own filename at its own tier.

    Catches accidental tier re-assignments during refactors. We put the
    keyword into the *content* rather than the path so non-word filename
    separators (``_``) don't break the word-boundary match (``auth_foo``
    is not a word boundary for ``\\bauth\\b``). This also exercises the
    full content-scoring path that the combined regex optimizes.
    """
    ranked = rank_files(
        [f"/file_{priority}.py"],
        read_fn=lambda _p, kw=keyword: f"# contains a {kw} reference",
    )
    assert ranked[0].priority >= priority, (
        f"{keyword!r} should rank at priority >= {priority}, got {ranked[0].priority}"
    )


class TestCombinedRegexParityWithPerTier:
    """Smoke tests for the combined-regex scoring path (perf optimization)."""

    def test_auth_on_word_boundary_scores_p5(self):
        # `auth.py` -> `auth` is bounded by `/` and `.` (non-word chars),
        # so \bauth\b matches cleanly.
        ranked = rank_files(["/src/auth.py"])
        assert ranked[0].priority == 5

    def test_helper_only_scores_p1(self):
        ranked = rank_files(["/src/helper.py"])
        assert ranked[0].priority == 1

    def test_no_keyword_scores_p1(self):
        ranked = rank_files(["/src/zzz_qqq_xyzzy.py"])
        assert ranked[0].priority == 1
        assert ranked[0].reasons == ()


class TestAdvisorIgnore:
    """Cover the uncommon branches of ``.advisorignore`` handling:
    warning on unreadable file, ``**`` globs, malformed regex, and
    directory-pattern (trailing ``/``) matches.
    """

    def test_unreadable_advisorignore_warns_and_returns_empty(self, tmp_path):
        import warnings

        from advisor.rank import load_advisorignore

        # A directory is not readable as text -> OSError path
        target = tmp_path / ".advisorignore"
        target.mkdir()
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            patterns = load_advisorignore(tmp_path)
        assert patterns == []
        assert any("could not read" in str(w.message) for w in caught)

    def test_advisorignore_missing_returns_empty(self, tmp_path):
        from advisor.rank import load_advisorignore

        # No file at all -> empty list, no warning
        assert load_advisorignore(tmp_path) == []

    def test_advisorignore_strips_comments_and_blanks(self, tmp_path):
        from advisor.rank import load_advisorignore

        (tmp_path / ".advisorignore").write_text("# comment\n\n  *.log  \n  \n# another\nvendor/\n")
        assert load_advisorignore(tmp_path) == ["*.log", "vendor/"]

    def test_double_star_recursive_glob(self):
        ranked = rank_files(
            ["src/a/b/c/d.py", "src/top.py", "tests/x.py"],
            ignore_patterns=["src/**/*.py"],
        )
        paths = {r.path for r in ranked}
        assert paths == {"tests/x.py"}

    def test_leading_double_star_matches_any_prefix(self):
        ranked = rank_files(
            ["deep/nested/generated/file.py", "generated/file.py", "src/ok.py"],
            ignore_patterns=["**/generated/*.py"],
        )
        assert {r.path for r in ranked} == {"src/ok.py"}

    def test_directory_pattern_with_trailing_slash(self):
        ranked = rank_files(
            ["vendor/lib.py", "src/vendor_note.py", "src/ok.py"],
            ignore_patterns=["vendor/"],
        )
        # `vendor/` only matches a path *component* equal to "vendor";
        # the `src/vendor_note.py` file is kept because "vendor_note" != "vendor".
        assert {r.path for r in ranked} == {"src/vendor_note.py", "src/ok.py"}

    def test_malformed_double_star_pattern_does_not_crash(self):
        # Unclosed bracket would produce a bad regex — the caller must
        # survive it and move on.
        ranked = rank_files(
            ["src/a.py"],
            ignore_patterns=["src/**/[unclosed"],
        )
        # File is not ignored because the pattern was invalid
        assert [r.path for r in ranked] == ["src/a.py"]

    def test_bare_word_pattern_matches_path_component(self):
        ranked = rank_files(
            ["src/node_modules/x.py", "src/app.py"],
            ignore_patterns=["node_modules"],
        )
        assert {r.path for r in ranked} == {"src/app.py"}

    def test_read_fn_errors_are_swallowed(self):
        def bad(_path: str) -> str:
            raise OSError("nope")

        # Should not raise — content is treated as empty and scoring
        # falls back to path-based keywords.
        ranked = rank_files(["src/auth.py"], read_fn=bad)
        assert ranked[0].priority == 5  # still matched on "auth" in path

    def test_double_star_ignore_regex_compiled_once_per_rank(self, monkeypatch):
        import advisor.rank as rank_module

        calls = 0
        real = rank_module._double_star_to_regex

        def counted(pattern: str):
            nonlocal calls
            calls += 1
            return real(pattern)

        monkeypatch.setattr(rank_module, "_double_star_to_regex", counted)

        ranked = rank_files(
            ["src/a.py", "src/nested/b.py", "tests/c.py"],
            ignore_patterns=["src/**/*.py"],
        )

        assert [r.path for r in ranked] == ["tests/c.py"]
        assert calls == 1


class TestLanguageAwareKeywords:
    """E2 — per-language keyword sets extend the core priority map."""

    def test_language_for_path_python(self):
        from advisor.rank import language_for_path

        assert language_for_path("src/auth.py") == "python"

    def test_language_for_path_javascript(self):
        from advisor.rank import language_for_path

        assert language_for_path("src/auth.js") == "javascript"
        assert language_for_path("src/auth.jsx") == "javascript"
        # .ts/.tsx share the JS/TS bucket — keyword overlap is high
        assert language_for_path("src/auth.ts") == "javascript"

    def test_language_for_path_go(self):
        from advisor.rank import language_for_path

        assert language_for_path("cmd/main.go") == "go"

    def test_language_for_path_rust(self):
        from advisor.rank import language_for_path

        assert language_for_path("src/main.rs") == "rust"

    def test_language_for_path_unknown(self):
        from advisor.rank import language_for_path

        assert language_for_path("README.md") is None

    def test_js_file_picks_up_js_specific_keyword(self):
        """JS-specific keywords like 'eval' / 'innerHTML' raise priority."""
        ranked = rank_files(
            ["src/handler.js"],
            read_fn=lambda _: "document.body.innerHTML = userInput;",
        )
        # innerHTML should be caught by the JS extension set; content alone
        # (without "auth"/"login"/"password") wouldn't otherwise be P3+
        assert ranked[0].priority >= 3

    def test_language_extra_keywords_contains_js(self):
        from advisor.rank import LANGUAGE_EXTRA_KEYWORDS

        assert "javascript" in LANGUAGE_EXTRA_KEYWORDS
        assert "go" in LANGUAGE_EXTRA_KEYWORDS
        assert "rust" in LANGUAGE_EXTRA_KEYWORDS
