from types import SimpleNamespace

import pr_agent.algo.token_budget as token_budget_module
import pr_agent.algo.token_handler as token_handler_module


def test_for_prompt_attempt_skips_discarded_initial_prompt_count(monkeypatch):
    settings = SimpleNamespace(config=SimpleNamespace(model="primary-model"))
    monkeypatch.setattr(token_handler_module, "get_settings", lambda use_context=True: settings)
    monkeypatch.setattr(
        token_handler_module.TokenEncoder,
        "get_token_encoder",
        lambda model=None: SimpleNamespace(
            encode=lambda text, disallowed_special=(): list(text),
            decode=lambda tokens: "".join(tokens),
        ),
    )
    monkeypatch.setattr(token_budget_module, "get_max_tokens", lambda *_args, **_kwargs: 10_000)

    warning_calls = []
    monkeypatch.setattr(
        token_budget_module,
        "warn_if_artifact_context_prompt_is_invalid",
        lambda *args: warning_calls.append(args),
    )
    calls = []
    original = token_handler_module.TokenHandler._get_system_user_tokens

    def counted(self, pr, encoder, variables, system, user):
        calls.append((pr, system, user))
        return original(self, pr, encoder, variables, system, user)

    monkeypatch.setattr(token_handler_module.TokenHandler, "_get_system_user_tokens", counted)

    variables = {"title": "PR", "diff": ""}
    token_handler_module.TokenHandler(
        object(),
        variables,
        "System {{ title }}",
        "User {{ diff }}",
        model="attempt-model",
    )
    assert len(calls) == 1

    calls.clear()
    variables["artifact_context"] = {
        "instructions": "Flag failures",
        "label": "ci.log",
        "content": "FAILED",
        "start_marker": "<START>",
        "end_marker": "<END>",
    }
    budget = token_budget_module.AttemptTokenBudget.for_prompt_attempt(
        "attempt-model",
        object(),
        variables,
        "System {{ title }}",
        "User {{ diff }}",
        ai_handler=object(),
    )

    assert calls == []
    assert len(warning_calls) == 1
    assert warning_calls[0][0] == "System {{ title }}"
    assert warning_calls[0][1] == "User {{ diff }}"
    assert warning_calls[0][2] is variables
    assert warning_calls[0][3:] == ("User ",)
    assert budget.prompt_tokens == (
        len("System PR")
        + len("User ")
        + 2 * token_budget_module.MESSAGE_FRAMING_TOKEN_ALLOWANCE
        + token_budget_module.REPLY_FRAMING_TOKEN_ALLOWANCE
    )
