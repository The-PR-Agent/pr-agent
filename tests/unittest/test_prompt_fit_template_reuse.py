from types import SimpleNamespace

import pytest
from jinja2 import UndefinedError
from jinja2.sandbox import SecurityError

from pr_agent.algo.token_budget import AttemptTokenBudget, SandboxedEnvironment


@pytest.mark.parametrize("optional_text", ["short <&> diff", "line <&>\n" * 100])
@pytest.mark.parametrize("keep", ["prefix", "suffix"])
def test_prompt_fitting_compiles_templates_once(monkeypatch, optional_text, keep):
    handler = SimpleNamespace(
        system="Review {{ title }}: <&>",
        user="{{ title }}\n{{ diff }}",
        count_tokens=len,
    )
    budget = AttemptTokenBudget("test-model", handler, handler, 200)
    variables = {"title": "PR <&>", "diff": "original"}
    compiled = []
    from_string = SandboxedEnvironment.from_string

    def record_compile(self, source, *args, **kwargs):
        compiled.append(source)
        return from_string(self, source, *args, **kwargs)

    monkeypatch.setattr(SandboxedEnvironment, "from_string", record_compile)
    fitted = budget.fit_prompt_variable(
        variables, "diff", optional_text, ai_handler=object(), default_output_tokens=10, keep=keep,
    )

    assert compiled == [handler.system, handler.user]
    assert variables == {"title": "PR <&>", "diff": "original"}
    assert fitted.input_tokens <= 190
    assert fitted.system_prompt == "Review PR <&>: <&>"
    assert fitted.user_prompt == f"PR <&>\n{fitted.optional_text}"
    if len(optional_text) > 200:
        assert fitted.optional_text != optional_text
    else:
        assert fitted.optional_text == optional_text

    def render_uncached(candidate):
        return budget.render_prompt_templates({**variables, "diff": candidate})

    assert fitted == budget.fit_optional_text(
        optional_text, render_uncached, ai_handler=object(), default_output_tokens=10, keep=keep,
    )


def test_prompt_fitting_does_not_reuse_templates_or_variables_between_calls():
    handler = SimpleNamespace(system="{{ title }}", user="{{ diff }}", count_tokens=len)
    budget = AttemptTokenBudget("test-model", handler, handler, 200)
    first = budget.fit_prompt_variable(
        {"title": "first"}, "diff", "one", ai_handler=object(), default_output_tokens=10,
    )
    handler.system = "updated {{ title }}"
    handler.user = "updated {{ diff }}"
    second = budget.fit_prompt_variable(
        {"title": "second"}, "diff", "two", ai_handler=object(), default_output_tokens=10,
    )

    assert (first.system_prompt, first.user_prompt) == ("first", "one")
    assert (second.system_prompt, second.user_prompt) == ("updated second", "updated two")


@pytest.mark.parametrize(
    ("template", "error"),
    [("{{ missing }}", UndefinedError), ("{{ value.__class__ }}", SecurityError)],
)
def test_prompt_fitting_preserves_strict_sandbox_rendering(template, error):
    handler = SimpleNamespace(system=template, user="{{ diff }}", count_tokens=len)
    budget = AttemptTokenBudget("test-model", handler, handler, 200)

    with pytest.raises(error):
        budget.fit_prompt_variable(
            {"value": object()}, "diff", "one", ai_handler=object(), default_output_tokens=10,
        )
