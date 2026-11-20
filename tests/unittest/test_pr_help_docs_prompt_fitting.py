"""Regression tests for verified /help_docs prompt sizing.

``/help_docs`` used to size the documentation text against a crude per-model
guess and dispatch the same prompt to every fallback model. Each model attempt
must instead be fitted against the exact rendered request so an over-budget
prompt is never sent.
"""

import math
from types import SimpleNamespace

import pytest

from pr_agent.algo.pr_processing import retry_with_fallback_models
from pr_agent.algo.token_budget import ModelContextPreparationError
from pr_agent.algo.token_handler import TokenHandler
from pr_agent.commands.pr_help_docs import (
    PredictionPreparator,
    PRHelpDocs,
    _clean_markdown_content_preserving_fences,
    aggregate_documentation_files_for_prompt_contents,
    clean_markdown_content,
    map_documentation_files_to_contents,
)
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_FIT_MODEL = "gpt-fit-test"
_LISTENER_XML = '<listener port="18443" mode="isolated-m7">localhost</listener>'


@pytest.fixture
def bounded_model():
    settings = get_settings()
    previous_model = settings.config.model
    previous_custom = settings.config.custom_model_max_tokens
    yield settings
    settings.set("config.model", previous_model)
    settings.set("config.custom_model_max_tokens", previous_custom)


def _preparator(window_tokens: int, snippets: str, bounded_model):
    bounded_model.set("config.model", _FIT_MODEL)
    bounded_model.set("config.custom_model_max_tokens", window_tokens)
    vars = {
        "docs_url": "https://example.com/org/repo",
        "question": "How do I configure this?",
        "snippets": snippets,
    }
    return PredictionPreparator(
        SimpleNamespace(),
        vars,
        get_settings().pr_help_docs_prompts.system,
        get_settings().pr_help_docs_prompts.user,
    )


def test_prediction_preparator_clips_snippets_to_verified_limit(bounded_model):
    full_snippets = "word " * 40_000
    preparator = _preparator(20_000, full_snippets, bounded_model)

    fitted = preparator._fit_snippets_for_model(_FIT_MODEL)

    assert fitted.input_tokens <= 20_000
    assert fitted.optional_text != full_snippets
    assert len(fitted.optional_text) < len(full_snippets)
    assert "(truncated)" in fitted.optional_text


def test_prediction_preparator_keeps_snippets_when_they_fit(bounded_model):
    short_snippets = "# Guide\n\nWelcome to the docs.\n"
    preparator = _preparator(200_000, short_snippets, bounded_model)

    fitted = preparator._fit_snippets_for_model(_FIT_MODEL)

    assert fitted.optional_text == short_snippets
    assert "(truncated)" not in fitted.optional_text


@pytest.mark.asyncio
async def test_prediction_preparator_dispatches_fitted_prompts(bounded_model):
    full_snippets = "word " * 40_000
    preparation = _preparator(20_000, full_snippets, bounded_model)
    dispatched = {}

    async def chat_completion(*, model, temperature, system, user):
        dispatched["model"] = model
        dispatched["system"] = system
        dispatched["user"] = user
        return "user_question: test", "stop"

    preparation.ai_handler.chat_completion = chat_completion

    response = await preparation(_FIT_MODEL)

    assert dispatched["model"] == _FIT_MODEL
    assert response == "user_question: test"
    assert "(truncated)" in dispatched["user"]
    assert len(dispatched["user"]) < len(full_snippets)


def test_prediction_preparator_raises_when_request_cannot_fit(bounded_model):
    preparator = _preparator(200, "", bounded_model)

    with pytest.raises(ValueError):
        preparator._fit_snippets_for_model(_FIT_MODEL)


def test_trim_docs_input_uses_verified_input_limit(bounded_model):
    bounded_model.set("config.model", _FIT_MODEL)
    bounded_model.set("config.custom_model_max_tokens", 20_000)
    command = PRHelpDocs.__new__(PRHelpDocs)
    command.vars = {
        "docs_url": "https://example.com/org/repo",
        "question": "How do I configure this?",
        "snippets": "",
    }
    command.ai_handler = SimpleNamespace()
    command.token_handler = TokenHandler(
        None,
        command.vars,
        get_settings().pr_help_docs_prompts.system,
        get_settings().pr_help_docs_prompts.user,
    )

    big_docs = "word " * 40_000
    assert command._trim_docs_input(big_docs, math.inf, only_return_if_trim_needed=True) is True

    trimmed = command._trim_docs_input(big_docs, math.inf, only_return_if_trim_needed=False)
    assert isinstance(trimmed, str)
    assert trimmed != big_docs
    limit = command._docs_input_token_limit()
    assert command.token_handler.count_tokens(trimmed, force_accurate=True) <= limit


def test_trim_docs_keeps_cleaned_text_when_it_fits(monkeypatch):
    command = PRHelpDocs.__new__(PRHelpDocs)
    command.token_handler = SimpleNamespace(count_tokens=lambda text, force_accurate=False: len(text))
    command._docs_input_token_limit = lambda: 16
    monkeypatch.setattr(
        "pr_agent.commands.pr_help_docs.clip_tokens",
        lambda *args, **kwargs: pytest.fail("cleaned text already fits"),
    )

    docs = "<!--" + "irrelevant " * 100 + "-->\nUseful text."
    assert command._trim_docs_input(docs, math.inf) == "Useful text."


def test_trim_docs_passes_cleaned_token_count_to_clipper(monkeypatch):
    command = PRHelpDocs.__new__(PRHelpDocs)
    command.token_handler = SimpleNamespace(count_tokens=lambda text, force_accurate=False: len(text))
    command._docs_input_token_limit = lambda: 10
    calls = []

    def clip(text, limit, *, num_input_tokens):
        calls.append((text, limit, num_input_tokens))
        return "clipped"

    monkeypatch.setattr("pr_agent.commands.pr_help_docs.clip_tokens", clip)
    docs = "<!--" + "irrelevant " * 100 + "-->\nUseful but longer documentation."
    cleaned = "Useful but longer documentation."

    assert command._trim_docs_input(docs, math.inf) == "clipped"
    assert calls == [(cleaned, 10, len(cleaned))]


@pytest.mark.parametrize(
    ("opening", "closing"),
    [
        ("```xml\n", "\n```"),
        ("~~~xml\n", "\n~~~~"),
        ("   ````xml\n", "\n  ````` \t"),
        ("````xml\n```\n~~~\n````suffix\n    ````\n> ````\n", "\n````"),
        ("```xml\n", ""),
        ("~~~xml `option`\n", "\n~~~"),
        ("```xml\r\n", "\r\n```\r\n"),
        ("```xml\nprefix\v```\n", "\n```"),
    ],
    ids=["backticks", "tildes", "indented", "false-closers", "unclosed", "tilde-info", "crlf", "literal-line-break"],
)
def test_corpus_cleanup_preserves_fenced_content(opening, closing):
    example = opening + _LISTENER_XML + "\n<!-- example comment -->\n\n\n![example](config.png)" + closing
    docs = "---\ntitle: Configuration\n---\n<!-- discard this -->\n# Guide\n\n" + example

    cleaned = _clean_markdown_content_preserving_fences(docs)

    assert cleaned == "# Guide\n\n" + example


def test_corpus_cleanup_preserves_bom_prefixed_fence():
    fenced = "\ufeff```xml\n" + _LISTENER_XML + "\n```"
    cleaned = _clean_markdown_content_preserving_fences(fenced + "\n<!-- discard -->\n# After")

    assert fenced in cleaned
    assert "discard" not in cleaned


@pytest.mark.parametrize("prefix", ["    ", "> ", "- "])
def test_corpus_cleanup_keeps_container_fences_conservatively(prefix):
    docs = "<!-- discard only if safe -->\n" + prefix + "```xml\n" + prefix + _LISTENER_XML + "\n" + prefix + "```"

    assert _clean_markdown_content_preserving_fences(docs) == docs


def test_corpus_cleanup_preserves_blocks_across_surrounding_markup():
    example = "```xml\n" + _LISTENER_XML + "\n```"
    docs = example + "\n---\nKeep this section.\n---\n\n" + example
    assert _clean_markdown_content_preserving_fences(docs) == docs

    cleaned = _clean_markdown_content_preserving_fences(docs + "\n<!-- discard after -->\n# After")
    assert docs in cleaned
    assert "discard after" not in cleaned
    assert cleaned.endswith("# After")

    wrapped = "<!--\n" + example + "\n-->"
    assert _clean_markdown_content_preserving_fences(wrapped) == wrapped


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("docs", "limit", "expected_content"),
    [
        ({"README.md": "<!--" + "irrelevant " * 100 + "-->\nUseful text."}, 128, "Useful text."),
        (
            {
                "/large.md": "<!--" + "irrelevant " * 80 + "-->\n# Unrelated",
                "/config.md": "# Listener configuration\n\n```xml\n" + _LISTENER_XML + "\n```",
            },
            256,
            _LISTENER_XML,
        ),
        (
            {
                "/config.md": "<!--" + "irrelevant " * 80 + "-->\n"
                "Use `` ``` `` or `~~~` for code fences.\n\n```xml\n" + _LISTENER_XML + "\n```",
            },
            256,
            _LISTENER_XML,
        ),
    ],
    ids=["comments", "fenced-xml", "inline-fence-notation"],
)
async def test_run_uses_cleaned_full_docs_without_unnecessary_ranking(monkeypatch, docs, limit, expected_content):
    command = PRHelpDocs.__new__(PRHelpDocs)
    command.question = "Where is this documented?"
    command.return_as_string = True
    command.vars = {"docs_url": "https://example.test", "question": command.question, "snippets": ""}
    command.ai_handler = SimpleNamespace()
    command.token_handler = SimpleNamespace(count_tokens=lambda text, force_accurate=False: len(text))
    command._docs_input_token_limit = lambda: limit
    command._gen_filenames_to_contents_map_from_repo = lambda: docs
    command._format_model_answer = lambda response, relevant_sections: response
    model_snippets = []
    assert len(aggregate_documentation_files_for_prompt_contents(docs)) > limit

    async def reject_ranking(*args):
        raise AssertionError("Ranking should not run when cleaned documentation fits")

    async def predict(preparator, **kwargs):
        model_snippets.append(preparator._vars["snippets"])
        return "unused"

    command._rank_docs_and_return_them_as_prompt = reject_ranking
    monkeypatch.setattr("pr_agent.commands.pr_help_docs.retry_with_fallback_models", predict)
    monkeypatch.setattr("pr_agent.commands.pr_help_docs.load_yaml", lambda response: {"response": "answer"})

    assert await command.run() == "answer"
    assert model_snippets == [command.vars["snippets"]]
    assert all(path in command.vars["snippets"] for path in docs)
    assert expected_content in command.vars["snippets"]
    assert "irrelevant" not in command.vars["snippets"]
    assert len(command.vars["snippets"]) <= limit


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", ["", "```xml\n" + _LISTENER_XML + "\n"], ids=["comment", "fenced-comment"])
async def test_run_keeps_document_boundaries_with_truncated_comments(tmp_path, monkeypatch, prefix):
    paths = []
    for name, content in (
        ("010-cut.md", prefix + "<!--" + "x" * 5000 + "-->"),
        ("020-guide.md", "# Guide\nmax_jobs = 7\n"),
        ("030-notes.md", "<!-- normal comment -->\n# Notes\n"),
    ):
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        paths.append(str(path))

    docs = map_documentation_files_to_contents(str(tmp_path), paths)
    assert "-->" not in docs["/010-cut.md"]
    assert "/020-guide.md" not in clean_markdown_content(
        aggregate_documentation_files_for_prompt_contents(docs)
    )

    command = PRHelpDocs.__new__(PRHelpDocs)
    command.question = "What is max_jobs?"
    command.return_as_string = True
    command.vars = {"docs_url": "https://example.test", "question": command.question, "snippets": ""}
    command.ai_handler = SimpleNamespace()
    command.token_handler = SimpleNamespace(count_tokens=lambda text, force_accurate=False: len(text))
    command._docs_input_token_limit = lambda: 150
    command._gen_filenames_to_contents_map_from_repo = lambda: docs
    command._format_model_answer = lambda response, relevant_sections: response
    rankings = []

    async def rank_docs(*args):
        rankings.append(True)
        return aggregate_documentation_files_for_prompt_contents({"/020-guide.md": docs["/020-guide.md"]})

    async def predict(*args, **kwargs):
        return "unused"

    command._rank_docs_and_return_them_as_prompt = rank_docs
    monkeypatch.setattr("pr_agent.commands.pr_help_docs.retry_with_fallback_models", predict)
    monkeypatch.setattr("pr_agent.commands.pr_help_docs.load_yaml", lambda response: {"response": "answer"})

    assert await command.run() == "answer"
    assert rankings == [True]
    assert "/020-guide.md" in command.vars["snippets"]
    assert "max_jobs = 7" in command.vars["snippets"]

    # Ranked documents must also retain file boundaries when clipping is needed.
    clipping_inputs = []

    def capture_clip(text, limit, *, num_input_tokens):
        clipping_inputs.append(text)
        return text

    monkeypatch.setattr("pr_agent.commands.pr_help_docs.clip_tokens", capture_clip)
    monkeypatch.setattr(
        "pr_agent.commands.pr_help_docs.load_yaml",
        lambda response: {"relevant_files_ranking": [{"idx": 0}, {"idx": 1}, {"idx": 2}]},
    )
    ranked = await PRHelpDocs._rank_docs_and_return_them_as_prompt(command, docs, math.inf)

    assert clipping_inputs
    assert all(path in ranked for path in docs)
    assert "/020-guide.md" in clipping_inputs[-1]
    assert "max_jobs = 7" in clipping_inputs[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "comment", ["", "<!--" + "irrelevant " * 80 + "-->\n"], ids=["already-fits", "cleanup-needed"]
)
async def test_ranked_docs_preserve_fitting_xml_example(monkeypatch, comment):
    xml_example = '```xml\n<listener port="18443" mode="isolated-m7">localhost</listener>\n```'
    docs = {
        "/large.md": "Unrelated information. " * 100,
        "/config.md": comment + "# Listener configuration\n\n" + xml_example,
    }
    limit = 256
    assert len(aggregate_documentation_files_for_prompt_contents(docs)) > limit
    selected_size = len(aggregate_documentation_files_for_prompt_contents({"/config.md": docs["/config.md"]}))
    assert (selected_size < limit) == (not comment)

    command = PRHelpDocs.__new__(PRHelpDocs)
    command.question = "How do I configure the listener?"
    command.return_as_string = True
    command.vars = {"docs_url": "https://example.test", "question": command.question, "snippets": ""}
    command.ai_handler = SimpleNamespace()
    command.token_handler = SimpleNamespace(count_tokens=lambda text, force_accurate=False: len(text))
    command._docs_input_token_limit = lambda: limit
    command._gen_filenames_to_contents_map_from_repo = lambda: docs
    command._format_model_answer = lambda response, relevant_sections: response

    responses = iter([{"relevant_files_ranking": [{"idx": 1}]}, {"response": "answer"}])
    model_calls = []

    async def predict(*args, **kwargs):
        model_calls.append(True)
        return "unused"

    monkeypatch.setattr("pr_agent.commands.pr_help_docs.retry_with_fallback_models", predict)
    monkeypatch.setattr("pr_agent.commands.pr_help_docs.load_yaml", lambda response: next(responses))

    assert await command.run() == "answer"
    assert len(model_calls) == 2
    assert "/config.md" in command.vars["snippets"]
    assert xml_example in command.vars["snippets"]


class _DeferredLimitAIHandler:
    def __init__(
        self,
        limits: dict[str, int],
        responses: dict[str, tuple[str, str]],
        prepare_errors: dict[str, Exception] | None = None,
    ):
        self._limits = limits
        self._responses = responses
        self._prepare_errors = prepare_errors or {}
        self.prepared_models: list[str] = []
        self.completed_models: list[str] = []

    async def prepare_model_token_limits(self, model: str) -> None:
        if model in self._prepare_errors:
            raise self._prepare_errors[model]
        self.prepared_models.append(model)

    def get_model_token_limits(self, model: str, *, ignore_max_model_tokens: bool = False) -> tuple[int, int | None]:
        if model not in self.prepared_models:
            raise RuntimeError(f"token limits requested before preparation for {model}")
        return self._limits[model], None

    async def chat_completion(self, *, model: str, temperature, system: str, user: str):
        self.completed_models.append(model)
        return self._responses[model]


@pytest.mark.asyncio
async def test_prediction_preparator_prepares_model_token_limits_before_fitting():
    primary = "openrouter/test/primary"
    fallback = "openrouter/test/fallback"
    snapshot = snapshot_settings([
        "config.model",
        "config.fallback_models",
        "model_routing.enable",
        "openai.fallback_deployments",
        "openai.deployment_id",
    ])
    try:
        settings = get_settings()
        settings.set("config.model", primary)
        settings.set("config.fallback_models", [fallback])
        settings.set("model_routing.enable", False)
        settings.set("openai.fallback_deployments", [])

        handler = _DeferredLimitAIHandler(
            limits={primary: 32_000, fallback: 32_000},
            responses={
                primary: ("response: partial", "length"),
                fallback: ("response: full answer", "stop"),
            },
        )
        preparator = PredictionPreparator(
            handler,
            {"docs_url": "https://example.test", "question": "How does it work?", "snippets": "# Guide\nDetails."},
            settings.pr_help_docs_prompts.system,
            settings.pr_help_docs_prompts.user,
        )

        response = await retry_with_fallback_models(preparator)

        assert response == "response: full answer"
        assert handler.prepared_models == [primary, fallback]
    finally:
        restore_settings(snapshot)


@pytest.mark.asyncio
async def test_help_docs_run_prepares_primary_model_token_limits_before_trimming(monkeypatch):
    primary = "openrouter/test/help-docs"
    snapshot = snapshot_settings(["config.model", "config.custom_model_max_tokens"])
    try:
        settings = get_settings()
        settings.set("config.model", primary)
        settings.set("config.custom_model_max_tokens", -1)

        handler = _DeferredLimitAIHandler(
            limits={primary: 32_000},
            responses={primary: ("response: answer\nquestion_is_relevant: 1\n", "stop")},
        )
        command = PRHelpDocs.__new__(PRHelpDocs)
        command.question = "How do I configure this?"
        command.return_as_string = True
        command.vars = {"docs_url": "https://example.test", "question": command.question, "snippets": ""}
        command.ai_handler = handler
        command.token_handler = TokenHandler(
            None,
            command.vars,
            settings.pr_help_docs_prompts.system,
            settings.pr_help_docs_prompts.user,
            ai_handler=handler,
        )
        command._gen_filenames_to_contents_map_from_repo = lambda: {"/guide.md": "# Guide\nUse config."}
        command._format_model_answer = lambda response, relevant_sections: response

        async def predict(preparator, **kwargs):
            return await preparator(primary)

        monkeypatch.setattr("pr_agent.commands.pr_help_docs.retry_with_fallback_models", predict)

        assert await command.run() == "answer"
        assert handler.prepared_models == [primary, primary]
    finally:
        restore_settings(snapshot)


@pytest.mark.asyncio
async def test_help_docs_run_falls_back_when_primary_preparation_fails(monkeypatch):
    primary = "openrouter/test/failing-primary"
    fallback = "openrouter/test/healthy-fallback"
    snapshot = snapshot_settings([
        "config.model",
        "config.fallback_models",
        "config.custom_model_max_tokens",
        "model_routing.enable",
        "openai.fallback_deployments",
        "openai.deployment_id",
    ])
    try:
        settings = get_settings()
        settings.set("config.model", primary)
        settings.set("config.fallback_models", [fallback])
        settings.set("config.custom_model_max_tokens", -1)
        settings.set("model_routing.enable", False)
        settings.set("openai.fallback_deployments", [])

        handler = _DeferredLimitAIHandler(
            limits={fallback: 32_000},
            responses={fallback: ("response: fallback answer\nquestion_is_relevant: 1\n", "stop")},
            prepare_errors={primary: ModelContextPreparationError("temporary OpenRouter outage")},
        )
        command = PRHelpDocs.__new__(PRHelpDocs)
        command.question = "How do I configure this?"
        command.return_as_string = True
        command.vars = {"docs_url": "https://example.test", "question": command.question, "snippets": ""}
        command.ai_handler = handler
        command.token_handler = TokenHandler(
            None,
            command.vars,
            settings.pr_help_docs_prompts.system,
            settings.pr_help_docs_prompts.user,
            ai_handler=handler,
        )
        command._gen_filenames_to_contents_map_from_repo = lambda: {"/guide.md": "# Guide\nUse config."}
        command._format_model_answer = lambda response, relevant_sections: response

        bound_models = []
        original_for_model = TokenHandler.for_model

        def track_for_model(self, model):
            bound_models.append(model)
            return original_for_model(self, model)

        monkeypatch.setattr(TokenHandler, "for_model", track_for_model)

        assert await command.run() == "fallback answer"
        assert handler.prepared_models == [fallback, fallback]
        assert handler.completed_models == [fallback]
        assert fallback in bound_models
        assert primary not in bound_models
    finally:
        restore_settings(snapshot)
