from dataclasses import replace
from types import SimpleNamespace

import pytest

import pr_agent.algo.token_budget as token_budget_module
import pr_agent.algo.token_handler as token_handler_module
from pr_agent.algo.ai_handlers.langchain_ai_handler import LangChainOpenAIHandler
from pr_agent.algo.ai_handlers.openai_ai_handler import OpenAIHandler


class FakeTokenHandler:
    def __init__(self, prompt_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.counted = []

    def count_tokens(self, text):
        self.counted.append(text)
        return len(text)


@pytest.fixture
def token_settings(monkeypatch):
    settings = SimpleNamespace(
        config=SimpleNamespace(model="primary-model"),
        get=lambda key, default=None: {
            "config.model_token_count_estimate_factor": 0,
            "config.image_input_token_allowance": 4096,
            "openai.key": "test-key",
            "anthropic.key": None,
        }.get(key, default),
    )
    monkeypatch.setattr(token_handler_module, "get_settings", lambda use_context=True: settings)
    monkeypatch.setattr(
        token_handler_module.TokenEncoder,
        "get_token_encoder",
        lambda model=None: SimpleNamespace(
            model=model,
            encode=lambda text, disallowed_special=(): list(text),
            decode=lambda tokens: "".join(tokens),
        ),
    )
    return settings


def test_for_attempt_uses_requested_window_and_preserves_fake_handler(monkeypatch):
    calls = []
    fake_handler = FakeTokenHandler(prompt_tokens=17)

    def get_window(model, ignore_max_model_tokens=False):
        calls.append((model, ignore_max_model_tokens))
        return 12_345

    monkeypatch.setattr(token_budget_module, "get_max_tokens", get_window)

    budget = token_budget_module.AttemptTokenBudget.for_attempt(
        "fallback-model",
        fake_handler,
        ignore_max_model_tokens=True,
    )

    assert budget.model == "fallback-model"
    assert budget.source_token_handler is fake_handler
    assert budget.token_handler is fake_handler
    assert budget.context_window == 12_345
    assert budget.prompt_tokens == 17
    assert calls == [("fallback-model", True)]


def test_for_attempt_binds_real_handler_without_mutating_source(monkeypatch, token_settings):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)
    variables = {"title": "PR"}
    source = token_handler_module.TokenHandler(object(), variables, "system {{ title }}", "user")

    budget = token_budget_module.AttemptTokenBudget.for_attempt("fallback-model", source)

    assert source.model == "primary-model"
    assert budget.source_token_handler is source
    assert budget.token_handler is not source
    assert budget.token_handler.model == "fallback-model"
    assert budget.token_handler.vars is variables
    assert budget.prompt_tokens == len("system PR") + len("user")


def test_for_prompt_attempt_counts_content_and_request_framing(monkeypatch, token_settings):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)
    variables = {"title": "PR", "diff": ""}

    budget = token_budget_module.AttemptTokenBudget.for_prompt_attempt(
        "fallback-model",
        object(),
        variables,
        "system {{ title }}",
        "user {{ diff }}",
        ai_handler=object(),
    )

    assert budget.token_handler.model == "fallback-model"
    assert budget.token_handler.vars is variables
    assert budget.prompt_tokens == (
        len("system PRuser ")
        + 2 * token_budget_module.MESSAGE_FRAMING_TOKEN_ALLOWANCE
        + token_budget_module.REPLY_FRAMING_TOKEN_ALLOWANCE
    )


def test_reserves_are_resolved_independently_for_each_default(monkeypatch):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)
    calls = []

    def reserve(model, default):
        calls.append((model, default))
        return default + 500

    budget = token_budget_module.AttemptTokenBudget.for_attempt(
        "openrouter/model",
        FakeTokenHandler(),
        output_token_reserve=reserve,
    )

    assert budget.resolve_output_reserve(1_500, preserve_minimum=True) == 2_000
    assert budget.resolve_output_reserve(1_000, preserve_minimum=True) == 1_500
    assert calls == [("openrouter/model", 1_500), ("openrouter/model", 1_000)]


@pytest.mark.parametrize(
    ("reported", "preserve_minimum", "expected"),
    [
        (100, False, 100),
        (100, True, 1_500),
        (1_200, True, 1_500),
        (1_500, True, 1_500),
        (5_000, True, 5_000),
    ],
)
def test_reserve_floor_is_an_explicit_consumer_policy(monkeypatch, reported, preserve_minimum, expected):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)
    budget = token_budget_module.AttemptTokenBudget.for_attempt(
        "model",
        FakeTokenHandler(),
        output_token_reserve=lambda _model, _default: reported,
    )

    assert (
        budget.resolve_output_reserve(
            1_500,
            preserve_minimum=preserve_minimum,
        )
        == expected
    )


def test_available_tokens_subtracts_prompt_once_and_clamps(monkeypatch):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 1_000)
    budget = token_budget_module.AttemptTokenBudget.for_attempt(
        "model",
        FakeTokenHandler(prompt_tokens=100),
        output_token_reserve=lambda _model, _default: 200,
    )

    assert budget.available_tokens(1_500) == 700
    assert budget.available_tokens(1_500, prompt_tokens=0) == 800
    assert budget.available_tokens(1_500, prompt_tokens=900) == 0


def test_input_token_limit_uses_attempt_reserve_and_extra_headroom(monkeypatch):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 1_000)
    budget = token_budget_module.AttemptTokenBudget.for_attempt(
        "model",
        FakeTokenHandler(),
        output_token_reserve=lambda _model, _default: 250,
    )

    assert budget.input_token_limit(100, additional_input_reserve=50) == 700
    assert budget.input_token_limit(300, preserve_minimum=True) == 700
    assert budget.input_token_limit(100, additional_input_reserve=True) == 750


def test_require_input_capacity_rejects_an_exhausted_attempt():
    handler = FakeTokenHandler(prompt_tokens=900)
    budget = token_budget_module.AttemptTokenBudget("small-model", handler, handler, 1_000)

    assert budget.require_input_capacity(99) == 1
    with pytest.raises(ValueError, match="leaves no input capacity"):
        budget.require_input_capacity(100)


@pytest.mark.parametrize(
    ("window", "reserve", "prompt", "expected"),
    [(1_000, 1_200, 0, -200), (1_000, 900, 200, -100), (1_000, 900, 100, 0), (1_000, 900, 99, 1)],
)
def test_raw_capacity_distinguishes_exhausted_from_exact_boundary(window, reserve, prompt, expected):
    handler = FakeTokenHandler(prompt_tokens=prompt)
    budget = token_budget_module.AttemptTokenBudget(
        "model",
        handler,
        handler,
        window,
        output_token_reserve=lambda model, default: reserve,
    )

    assert budget.available_tokens(1_500, clamp=False) == expected
    assert budget.available_tokens(1_500) == max(expected, 0)


def test_frozen_budget_can_refresh_callback_without_rebinding(monkeypatch):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)
    source = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget.for_attempt(
        "model",
        source,
        output_token_reserve=lambda _model, _default: 1_500,
    )

    refreshed = replace(
        budget,
        output_token_reserve=lambda _model, _default: 5_000,
    )

    assert refreshed is not budget
    assert refreshed.source_token_handler is source
    assert refreshed.token_handler is budget.token_handler
    assert refreshed.resolve_output_reserve(1_500) == 5_000


def test_count_tokens_delegates_without_breaking_simple_fakes(monkeypatch):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)
    source = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget.for_attempt("model", source)

    assert budget.count_tokens("abcd") == 4
    assert source.counted == ["abcd"]


@pytest.mark.parametrize("normalization", [None, ("normalized system", "normalized user")])
def test_prepare_request_normalizes_and_counts_the_dispatched_messages(monkeypatch, normalization):
    handler = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget("attempt-model", handler, handler, 1_000)
    observed = []

    class AIHandler:
        def normalize_request_prompts(self, model, system_prompt, user_prompt):
            observed.append((model, system_prompt, user_prompt))
            if normalization is None:
                raise RuntimeError("provider normalization unavailable")
            return normalization

    prepared = budget.prepare_request(AIHandler(), "system", "user", optional_text="diff")

    expected_prompts = normalization or ("system", "user")
    assert (prepared.system_prompt, prepared.user_prompt) == expected_prompts
    assert prepared.optional_text == "diff"
    assert prepared.input_tokens == (
        sum(map(len, expected_prompts))
        + 2 * token_budget_module.MESSAGE_FRAMING_TOKEN_ALLOWANCE
        + token_budget_module.REPLY_FRAMING_TOKEN_ALLOWANCE
    )
    assert observed[0] == ("attempt-model", "system", "user")
    assert handler.counted == list(expected_prompts)


def test_prepare_request_counts_image_message_with_conservative_floor(monkeypatch):
    handler = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget("attempt-model", handler, handler, 10_000)
    settings = SimpleNamespace(
        get=lambda key, default=None: {
            "config.image_input_token_allowance": 4096,
        }.get(key, default)
    )

    monkeypatch.setattr(token_budget_module, "get_settings", lambda: settings)

    prepared = budget.prepare_request(
        object(),
        "system",
        "user",
        image_path="https://example.test/image.png",
    )

    assert handler.counted == ["system", "user", "https://example.test/image.png"]
    assert prepared.input_tokens == (
        len("systemuserhttps://example.test/image.png")
        + 2 * token_budget_module.MESSAGE_FRAMING_TOKEN_ALLOWANCE
        + token_budget_module.REPLY_FRAMING_TOKEN_ALLOWANCE
        + 4096
    )


def test_prepare_request_counts_the_handler_final_message_shape(monkeypatch):
    handler = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget("attempt-model", handler, handler, 10_000)
    class AIHandler:
        def build_request_messages(self, model, system_prompt, user_prompt, *, image_path=None):
            assert model == "attempt-model"
            assert image_path is None
            return [{"role": "user", "content": f"{system_prompt}\n\n\n{user_prompt}"}]

    prepared = budget.prepare_request(AIHandler(), "system", "user")

    assert prepared.input_tokens == (
        len("system\n\n\nuser")
        + token_budget_module.MESSAGE_FRAMING_TOKEN_ALLOWANCE
        + token_budget_module.REPLY_FRAMING_TOKEN_ALLOWANCE
    )
    assert handler.counted == ["system\n\n\nuser"]


@pytest.mark.parametrize("handler_class", [OpenAIHandler, LangChainOpenAIHandler])
def test_prepare_request_counts_text_only_for_handlers_that_ignore_images(
    monkeypatch,
    token_settings,
    handler_class,
):
    handler = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget("attempt-model", handler, handler, 10_000)
    monkeypatch.setattr(token_budget_module, "get_settings", lambda: token_settings)

    prepared = budget.prepare_request(
        handler_class.__new__(handler_class),
        "system",
        "user",
        image_path="https://example.test/image.png",
    )

    assert prepared.input_tokens == (
        len("systemuser")
        + 2 * token_budget_module.MESSAGE_FRAMING_TOKEN_ALLOWANCE
        + token_budget_module.REPLY_FRAMING_TOKEN_ALLOWANCE
    )
    assert handler.counted == ["system", "user"]


@pytest.mark.parametrize("allowance", [None, -1, True, "4096"])
def test_image_allowance_must_be_a_non_negative_integer(monkeypatch, allowance):
    handler = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget("attempt-model", handler, handler, 10_000)
    settings = SimpleNamespace(
        get=lambda key, default=None: (
            allowance if key == "config.image_input_token_allowance" else default
        )
    )
    monkeypatch.setattr(token_budget_module, "get_settings", lambda: settings)

    with pytest.raises(ValueError, match="image_input_token_allowance"):
        budget.prepare_request(
            object(),
            "system",
            "user",
            image_path="https://example.test/image.png",
        )


def test_count_request_tokens_uses_attempt_tokenizer_and_ignores_estimate_factor(monkeypatch):
    handler = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget("attempt-model", handler, handler, 1_000)
    settings = SimpleNamespace(
        get=lambda key, default=None: {
            "config.model_token_count_estimate_factor": 0.25,
        }.get(key, default)
    )
    monkeypatch.setattr(token_budget_module, "get_settings", lambda: settings)

    count = budget.count_request_tokens("1234", "123456")

    expected = 10 + 2 * token_budget_module.MESSAGE_FRAMING_TOKEN_ALLOWANCE
    expected += token_budget_module.REPLY_FRAMING_TOKEN_ALLOWANCE
    assert count == expected
    assert handler.counted == ["1234", "123456"]


def test_count_request_tokens_counts_the_provided_message_shape():
    handler = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget("attempt-model", handler, handler, 1_000)
    messages = [{"role": "user", "content": "combined"}]

    assert budget.count_request_tokens("ignored", "ignored", messages=messages) == 40
    assert handler.counted == ["combined"]


@pytest.mark.parametrize(
    ("keep", "expected_start", "expected_end"),
    [("prefix", "abc", "...(truncated)\n"), ("suffix", "\n...(truncated)", "rst")],
)
def test_fit_optional_text_preserves_the_requested_side_and_exact_prompts(
    monkeypatch, keep, expected_start, expected_end
):
    handler = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget("attempt-model", handler, handler, 82)

    fitted = budget.fit_optional_text(
        "abcdefghijklmnopqrst",
        lambda optional: ("fixed", f"body:{optional}"),
        ai_handler=object(),
        default_output_tokens=5,
        keep=keep,
    )

    assert fitted.optional_text.startswith(expected_start)
    assert fitted.optional_text.endswith(expected_end)
    assert fitted.system_prompt == "fixed"
    assert fitted.user_prompt == f"body:{fitted.optional_text}"
    assert fitted.input_tokens <= 77


def test_fit_optional_text_keeps_marker_when_no_content_character_fits(monkeypatch):
    handler = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget("attempt-model", handler, handler, 57)

    fitted = budget.fit_optional_text(
        "abcdefghi",
        lambda optional: ("s", f"u:{optional}"),
        ai_handler=object(),
        default_output_tokens=1,
        truncation_marker="[cut]",
    )

    assert fitted.optional_text == "[cut]"
    assert fitted.input_tokens == 56


def test_fit_optional_text_rejects_unmarked_empty_replacement(monkeypatch):
    handler = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget("attempt-model", handler, handler, 54)

    with pytest.raises(ValueError, match="truncation marker"):
        budget.fit_optional_text(
            "optional",
            lambda optional: ("s", f"u:{optional}"),
            ai_handler=object(),
            default_output_tokens=1,
            truncation_marker="[cut]",
        )


def test_fit_optional_text_truncates_only_at_attempt_token_boundaries(monkeypatch):
    handler = FakeTokenHandler()
    handler.encoder = SimpleNamespace(
        encode=lambda text, disallowed_special=(): text.split("|"),
        decode=lambda tokens: "|".join(tokens),
    )
    budget = token_budget_module.AttemptTokenBudget("attempt-model", handler, handler, 75)

    fitted = budget.fit_optional_text(
        "alpha|bravo|charlie|delta",
        lambda optional: ("sys", optional),
        ai_handler=object(),
        default_output_tokens=2,
        truncation_marker="[cut]",
    )

    retained = fitted.optional_text.removesuffix("[cut]").rstrip("|")
    assert retained in {"alpha", "alpha|bravo", "alpha|bravo|charlie"}
    assert fitted.input_tokens <= 73


def test_fit_optional_text_rejects_required_prompt_that_cannot_fit(monkeypatch):
    handler = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget("attempt-model", handler, handler, 58)

    with pytest.raises(ValueError, match="required prompt"):
        budget.fit_optional_text(
            "optional",
            lambda optional: ("required-system", f"required-user:{optional}"),
            ai_handler=object(),
            default_output_tokens=1,
        )


def test_fit_optional_text_rejects_unknown_retention_policy():
    handler = FakeTokenHandler()
    budget = token_budget_module.AttemptTokenBudget("attempt-model", handler, handler, 100)

    with pytest.raises(ValueError, match="retention policy"):
        budget.fit_optional_text(
            "optional",
            lambda optional: ("system", optional),
            ai_handler=object(),
            default_output_tokens=10,
            keep="middle",
        )


def test_fit_prompt_variable_does_not_mutate_shared_variables(monkeypatch, token_settings):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 78)
    variables = {"diff": "", "title": "PR"}
    budget = token_budget_module.AttemptTokenBudget.for_prompt_attempt(
        "fallback-model",
        object(),
        variables,
        "{{ title }}",
        "{{ diff }}",
        ai_handler=object(),
    )

    fitted = budget.fit_prompt_variable(
        variables,
        "diff",
        "abcdefghijklmnopqrstuvwxyz",
        ai_handler=object(),
        default_output_tokens=5,
    )

    assert fitted.optional_text != "abcdefghijklmnopqrstuvwxyz"
    assert variables == {"diff": "", "title": "PR"}
    assert fitted.system_prompt == "PR"
    assert fitted.user_prompt == fitted.optional_text


def test_matches_accepts_source_or_own_bound_handler_only(monkeypatch, token_settings):
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)
    source = token_handler_module.TokenHandler(object(), {}, "system", "user")
    budget = token_budget_module.AttemptTokenBudget.for_attempt("fallback-model", source)
    equivalent_handler = source.for_model("fallback-model")

    assert budget.matches("fallback-model", source)
    assert budget.matches("fallback-model", budget.token_handler)
    assert not budget.matches("different-model", source)
    assert not budget.matches("fallback-model", equivalent_handler)
    assert not budget.matches("fallback-model", object())
