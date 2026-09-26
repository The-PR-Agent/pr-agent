from unittest.mock import Mock

import pytest

from pr_agent.algo.skills_loader import Skill, format_skills_context
from pr_agent.algo.token_budget import clip_tokens
from pr_agent.algo.token_handler import TokenEncoder, TokenHandler


@pytest.fixture
def encoder(monkeypatch):
    def encode(text, *, disallowed_special="all"):
        if disallowed_special != () and "<|endoftext|>" in text:
            raise ValueError("Encountered text corresponding to disallowed special token")
        return list(text)

    encoder = Mock()
    encoder.encode.side_effect = encode
    monkeypatch.setattr(TokenEncoder, "get_token_encoder", lambda model=None: encoder)
    return encoder


@pytest.mark.parametrize("max_tokens", [60, 10_000])
@pytest.mark.parametrize("delete_last_line", [False, True])
def test_special_token_literals_do_not_bypass_clipping(encoder, max_tokens, delete_last_line):
    text = "line containing <|endoftext|>\n" * 20
    expected = clip_tokens(
        text, max_tokens, num_input_tokens=len(text), delete_last_line=delete_last_line,
    )

    assert clip_tokens(text, max_tokens, delete_last_line=delete_last_line) == expected
    encoder.encode.assert_called_once_with(text, disallowed_special=())


@pytest.mark.parametrize("max_tokens", [-1, 0])
@pytest.mark.parametrize("delete_last_line", [False, True])
def test_non_positive_budget_skips_special_token_encoding(encoder, max_tokens, delete_last_line):
    text = "line containing <|endoftext|>\n" * 20

    assert clip_tokens(text, max_tokens, delete_last_line=delete_last_line) == ""
    encoder.encode.assert_not_called()


def test_special_token_clipping_preserves_no_marker_option(encoder):
    text = "<|endoftext|> " * 100

    assert clip_tokens(text, 20, add_three_dots=False) == text[:18]
    encoder.encode.assert_called_once_with(text, disallowed_special=())


@pytest.mark.parametrize("text", [None, "", "ordinary text", "<|endoftext|>"])
def test_empty_or_precounted_text_does_not_call_encoder(encoder, text):
    assert clip_tokens(text, 100, num_input_tokens=len(text or "")) == text
    encoder.encode.assert_not_called()


@pytest.mark.parametrize(
    ("system", "user"),
    [
        ("system <|endoftext|>", "user"),
        ("system", "user <|endoftext|>"),
        ("system {{ text }}", "user {{ text }}"),
        ("system", "user"),
    ],
)
def test_initial_prompt_count_treats_special_token_literals_as_text(encoder, system, user):
    text = "<|endoftext|>"
    handler = TokenHandler(object(), {"text": text}, system, user, model="test-model")
    rendered = [template.replace("{{ text }}", text) for template in (system, user)]

    assert handler.prompt_tokens == sum(map(len, rendered))
    assert encoder.encode.call_count == 2
    for call, prompt in zip(encoder.encode.call_args_list, rendered, strict=True):
        assert call.args == (prompt,)
        assert call.kwargs == {"disallowed_special": ()}


def test_skills_context_treats_special_token_literals_as_text():
    skill = Skill(
        name="tokenizer-guidance",
        description="Use when reviewing tokenizer code.",
        body="Document the literal <|endoftext|> token spelling.",
    )

    context = format_skills_context([skill], max_tokens=1_000)

    assert "<|endoftext|>" in context
