from pr_agent.algo.utils import replace_code_tags


def test_wraps_paired_backticks():
    assert replace_code_tags("Use `foo` and `bar`") == "Use <code>foo</code> and <code>bar</code>"


def test_escapes_html_inside_and_outside_code():
    assert replace_code_tags("Rename `<old>` to <new>") == "Rename <code>&lt;old&gt;</code> to &lt;new&gt;"


def test_unpaired_backtick_stays_literal():
    assert replace_code_tags("Quote the `name argument") == "Quote the `name argument"


def test_trailing_unpaired_backtick_after_a_span_stays_literal():
    assert replace_code_tags("Call `run()` before `setup") == "Call <code>run()</code> before `setup"


def test_double_backtick_span_keeps_inner_backtick():
    assert replace_code_tags("Match ``a`b`` exactly") == "Match <code>a`b</code> exactly"


def test_runs_of_different_lengths_do_not_close_each_other():
    assert replace_code_tags("``x` y``") == "<code>x` y</code>"


def test_text_without_backticks_is_only_escaped():
    assert replace_code_tags("a < b & c") == "a &lt; b &amp; c"
