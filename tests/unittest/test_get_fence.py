from pr_agent.algo.utils import _get_fence


class TestGetFence:
    def test_empty_content_returns_backtick_minimum(self):
        """Verify that empty content uses the minimum three-backtick fence."""
        assert _get_fence("") == "```"

    def test_plain_text_returns_backtick_minimum(self):
        """Verify that plain content uses the minimum three-backtick fence."""
        assert _get_fence("hello world") == "```"

    def test_single_backtick_returns_minimum(self):
        """Keep the minimum fence when content contains single backticks."""
        assert _get_fence("use `code` here") == "```"

    def test_double_backtick_returns_minimum(self):
        """Keep the minimum fence when content contains two-backtick runs."""
        assert _get_fence("``two``") == "```"

    def test_triple_backtick_in_content_prefers_tilde(self):
        """Prefer three tildes over four backticks for a triple-backtick run."""
        assert _get_fence("``` code block ```") == "~~~"

    def test_long_backtick_run_prefers_tilde(self):
        """Prefer the shorter tilde fence for a long backtick run."""
        content = "`" * 20  # 20 consecutive backticks
        result = _get_fence(content)
        # tilde fence is ~~~ (length 3); backtick fence would be 21 chars
        assert result == "~~~"

    def test_long_tilde_run_prefers_backtick(self):
        """Prefer the shorter backtick fence for a long tilde run."""
        content = "~" * 20  # 20 consecutive tildes
        result = _get_fence(content)
        # backtick fence is ``` (length 3); tilde fence would be 21 chars
        assert result == "```"

    def test_both_long_runs_picks_shorter(self):
        """Choose the shorter safe fence when both characters have long runs."""
        # 10 backticks → backtick fence = 11; 5 tildes → tilde fence = 6
        content = "`" * 10 + " " + "~" * 5
        result = _get_fence(content)
        assert result == "~~~~~~"  # 6 tildes is shorter than 11 backticks

    def test_equal_length_runs_prefers_backtick(self):
        """Prefer backticks when both safe fences have equal lengths."""
        content = "```" + " " + "~~~"  # both have run of 3 → fence length 4
        result = _get_fence(content)
        assert result == "````"

    def test_minimum_fence_length_is_three(self):
        """Require at least three fence characters for plain content."""
        result = _get_fence("no special chars here")
        assert len(result) >= 3

    def test_fence_does_not_appear_in_content_backtick(self):
        """Choose a fence absent from content containing backtick runs."""
        content = "```" * 5
        fence = _get_fence(content)
        assert fence not in content

    def test_fence_does_not_appear_in_content_tilde(self):
        """Choose a fence absent from content containing tilde runs."""
        content = "~~~" * 5
        fence = _get_fence(content)
        assert fence not in content

    def test_pathological_backtick_content_fence_is_short(self):
        """Keep the chosen fence short despite one hundred consecutive backticks."""
        content = "`" * 100
        fence = _get_fence(content)
        assert len(fence) <= 4  # tilde fence will be ~~~ (3 chars)
