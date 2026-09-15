from pr_agent.algo.utils import _get_fence


class TestGetFence:
    def test_empty_content_returns_backtick_minimum(self):
        """No runs of either character → minimum 3-backtick fence."""
        assert _get_fence("") == "```"

    def test_plain_text_returns_backtick_minimum(self):
        """Content with no backticks or tildes → minimum 3-backtick fence."""
        assert _get_fence("hello world") == "```"

    def test_single_backtick_returns_minimum(self):
        """A single backtick in content → run of 1, fence is still 3."""
        assert _get_fence("use `code` here") == "```"

    def test_double_backtick_returns_minimum(self):
        """A run of 2 backticks → fence is still 3."""
        assert _get_fence("``two``") == "```"

    def test_triple_backtick_in_content_prefers_tilde(self):
        """A run of 3 backticks → backtick fence would be 4, tilde fence is 3, so tilde wins."""
        assert _get_fence("``` code block ```") == "~~~"

    def test_long_backtick_run_prefers_tilde(self):
        """Content with a very long backtick run → tilde fence wins because it's shorter."""
        content = "`" * 20  # 20 consecutive backticks
        result = _get_fence(content)
        # tilde fence is ~~~ (length 3); backtick fence would be 21 chars
        assert result == "~~~"

    def test_long_tilde_run_prefers_backtick(self):
        """Content with a very long tilde run → backtick fence wins because it's shorter."""
        content = "~" * 20  # 20 consecutive tildes
        result = _get_fence(content)
        # backtick fence is ``` (length 3); tilde fence would be 21 chars
        assert result == "```"

    def test_both_long_runs_picks_shorter(self):
        """Both characters have long runs → the one with the shorter safe fence wins."""
        # 10 backticks → backtick fence = 11; 5 tildes → tilde fence = 6
        content = "`" * 10 + " " + "~" * 5
        result = _get_fence(content)
        assert result == "~~~~~~"  # 6 tildes is shorter than 11 backticks

    def test_equal_length_runs_prefers_backtick(self):
        """When both fences would be the same length, backtick is returned."""
        content = "```" + " " + "~~~"  # both have run of 3 → fence length 4
        result = _get_fence(content)
        assert result == "````"

    def test_minimum_fence_length_is_three(self):
        """Even with zero occurrences of either character the fence is at least 3 chars."""
        result = _get_fence("no special chars here")
        assert len(result) >= 3

    def test_fence_does_not_appear_in_content_backtick(self):
        """The returned fence string must not be present verbatim inside the content."""
        content = "```" * 5
        fence = _get_fence(content)
        assert fence not in content

    def test_fence_does_not_appear_in_content_tilde(self):
        """The returned fence string must not be present verbatim inside the content."""
        content = "~~~" * 5
        fence = _get_fence(content)
        assert fence not in content

    def test_pathological_backtick_content_fence_is_short(self):
        """With 100 consecutive backticks the chosen fence should be at most 4 chars long."""
        content = "`" * 100
        fence = _get_fence(content)
        assert len(fence) <= 4  # tilde fence will be ~~~ (3 chars)
