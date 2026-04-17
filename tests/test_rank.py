"""Tests for advisor.rank module."""

from advisor.rank import RankedFile, rank_files, rank_to_prompt


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
        try:
            rf.priority = 1  # type: ignore
            assert False, "Should have raised"
        except AttributeError:
            pass


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
            RankedFile(path=f"src/f{i}.py", priority=5 - i, reasons=("x",))
            for i in range(20)
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
        assert _matches_any_pattern('scripts.py/handler.txt', ['*.py']) is False

    def test_wildcard_pattern_matches_filename(self):
        from advisor.rank import _matches_any_pattern
        assert _matches_any_pattern('src/foo.py', ['*.py']) is True

    def test_bare_word_pattern_matches_dir_component(self):
        from advisor.rank import _matches_any_pattern
        assert _matches_any_pattern('src/tests/foo.py', ['tests']) is True

    def test_bare_word_pattern_does_not_match_unrelated_file(self):
        from advisor.rank import _matches_any_pattern
        assert _matches_any_pattern('src/tests.py', ['tests']) is False
