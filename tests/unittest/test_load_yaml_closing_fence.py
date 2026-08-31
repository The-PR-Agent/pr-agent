"""Strip a closing code fence only when it closes a fence the response opened."""
from pr_agent.algo.utils import load_yaml

ANSWER_ENDING_IN_A_FENCE = (
    "response: |\n"
    "  Use this:\n"
    "  ```python\n"
    "  print(1)\n"
    "  ```\n"
)


def test_keep_a_closing_fence_that_belongs_to_the_content():
    """An unfenced response must keep the fence its own answer opened."""
    answer = load_yaml(ANSWER_ENDING_IN_A_FENCE)["response"]

    assert answer.count("```") == 2


def test_strip_the_wrapper_fence_the_model_was_primed_to_emit():
    """The prompts end with an open ```yaml, so a wrapped response must still unwrap."""
    wrapped = "```yaml\nresponse: |\n  hello\n```"

    assert load_yaml(wrapped)["response"].strip() == "hello"


def test_strip_the_wrapper_fence_around_content_that_also_ends_in_one():
    """Unwrap the outer fence while leaving the inner one intact."""
    wrapped = "```yaml\n" + ANSWER_ENDING_IN_A_FENCE + "```"

    assert load_yaml(wrapped)["response"].count("```") == 2


def test_an_unfenced_response_parses_unchanged():
    """Keep the ordinary unfenced case working."""
    assert load_yaml("response: |\n  hello\n")["response"].strip() == "hello"


def test_a_bare_yaml_prefix_is_still_removed():
    """Keep removing the bare 'yaml' prefix some models emit."""
    assert load_yaml("yaml\nresponse: |\n  hello\n")["response"].strip() == "hello"
