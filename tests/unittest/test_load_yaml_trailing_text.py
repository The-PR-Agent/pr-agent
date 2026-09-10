"""Drop a sign-off the model added after the wrapper's closing fence."""
from pr_agent.algo.utils import load_yaml

SIGN_OFF = "\n\nI reviewed the diff and found nothing else worth flagging."

# The prompts ask for YAML "and nothing else", but 8 of the 12 user prompts end
# with a dangling open ```yaml, so a reply carries either both fences or, more
# often, a closing one only.
WRAPPED = "```yaml\ncode_suggestions: []\n```"
PRIMED = "code_suggestions: []\n```"


def test_a_wrapped_mapping_survives_a_sign_off():
    """The snippet fallback cannot recover this: its pattern requires the
    closing fence to end the response."""
    assert load_yaml(WRAPPED + SIGN_OFF, first_key="code_suggestions",
                     last_key="label") == {"code_suggestions": []}


def test_a_primed_mapping_survives_a_sign_off():
    """A reply with a closing fence and no opening one cannot be recovered by
    any fence pattern, since there is no pair to match."""
    assert load_yaml(PRIMED + SIGN_OFF, first_key="code_suggestions",
                     last_key="label") == {"code_suggestions": []}


def test_a_sign_off_is_not_folded_into_a_block_scalar():
    """Worse than a parse failure: a single block scalar absorbs the fence and
    the remark as part of its value, so the answer is published corrupted."""
    wrapped = "```yaml\nresponse: |\n  hello\n```" + SIGN_OFF

    assert load_yaml(wrapped)["response"].strip() == "hello"


def test_an_answer_following_a_fenced_example_is_not_truncated():
    """Dropping text after that fence would leave a plain scalar, so the
    response must be left alone for the fallbacks to handle."""
    assert load_yaml("```\nexample\n```\nresponse: hello") == {}


def test_a_fence_inside_a_block_scalar_is_not_treated_as_the_wrapper():
    """Only a fence at content level closes the wrapper."""
    answer = load_yaml("response: |\n  Use this:\n  ```python\n  print(1)\n  ```\n")

    assert answer["response"].count("```") == 2


def test_trailing_whitespace_after_the_fence_changes_nothing():
    assert load_yaml("```yaml\nresponse: |\n  hello\n```  \n\n")["response"].strip() == "hello"


def test_payload_continuing_past_the_fence_still_fails_loudly():
    """A stray fence mid-answer must not yield a partial mapping. Failing keeps the
    retry that a truncated answer would quietly replace."""
    assert load_yaml("a: 1\n```\nb: 2") == {}


def test_a_list_continuing_past_the_fence_still_fails_loudly():
    """The /improve shape: a suggestion list truncated to its first item would be
    published as if it were complete."""
    reply = "code_suggestions:\n  - one\n```\n  - two"

    assert load_yaml(reply, first_key="code_suggestions", last_key="label") == {}


def test_a_nested_mapping_continuing_past_the_fence_still_fails_loudly():
    assert load_yaml("review:\n  score: 1\n```\n  effort: 2") == {}


def test_a_continuation_followed_by_a_sign_off_still_fails_loudly():
    """The tail is a mapping and then prose, so the tail as a whole does not parse.
    Only the shape of its first line marks it as more answer rather than a remark."""
    reply = ("code_suggestions:\n  - one\n```\nsecurity_concerns: 'No'\nscore: 8\n\n"
             "Hope this helps.")

    assert load_yaml(reply, first_key="code_suggestions", last_key="security_concerns") == {}


def test_a_crlf_reply_is_repaired():
    """Provider payloads arrive with CRLF, so the fence line ends with \\r."""
    reply = "```yaml\r\nresponse: |\r\n  hello\r\n```\r\n\r\nThat is my answer."

    assert load_yaml(reply)["response"].strip() == "hello"


def test_a_sign_off_that_reads_as_a_mapping_is_left_alone():
    """The cost of the rule above: nothing tells 'Note: ...' apart from payload, so
    such a reply keeps the pre-existing behaviour rather than risking a truncation."""
    assert load_yaml("a: 1\n```\n\nNote: nothing else stood out.") == {}


def test_a_second_fenced_block_is_left_to_the_existing_behaviour():
    """Nothing follows the last fence, so there is no sign-off to drop."""
    reply = "```yaml\nresponse: |\n  hello\n```\n\n```\nprint(1)\n```"

    assert "hello" in load_yaml(reply)["response"]
