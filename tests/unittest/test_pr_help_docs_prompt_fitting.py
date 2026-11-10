"""Regression tests for verified /help_docs prompt sizing.

``/help_docs`` used to size the documentation text against a crude per-model
guess and dispatch the same prompt to every fallback model. Each model attempt
must instead be fitted against the exact rendered request so an over-budget
prompt is never sent.
"""

import math
from types import SimpleNamespace

import pytest

from pr_agent.algo.token_handler import TokenHandler
from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_help_docs import (
    PredictionPreparator,
    PRHelpDocs,
    _clean_markdown_content_preserving_fences,
    aggregate_documentation_files_for_prompt_contents,
    clean_markdown_content,
    map_documentation_files_to_contents,
)

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
    tool = PRHelpDocs.__new__(PRHelpDocs)
    tool.vars = {
        "docs_url": "https://example.com/org/repo",
        "question": "How do I configure this?",
        "snippets": "",
    }
    tool.ai_handler = SimpleNamespace()
    tool.token_handler = TokenHandler(
        None,
        tool.vars,
        get_settings().pr_help_docs_prompts.system,
        get_settings().pr_help_docs_prompts.user,
    )

    big_docs = "word " * 40_000
    assert tool._trim_docs_input(big_docs, math.inf, only_return_if_trim_needed=True) is True

    trimmed = tool._trim_docs_input(big_docs, math.inf, only_return_if_trim_needed=False)
    assert isinstance(trimmed, str)
    assert trimmed != big_docs
    limit = tool._docs_input_token_limit()
    assert tool.token_handler.count_tokens(trimmed, force_accurate=True) <= limit


def test_trim_docs_keeps_cleaned_text_when_it_fits(monkeypatch):
    tool = PRHelpDocs.__new__(PRHelpDocs)
    tool.token_handler = SimpleNamespace(count_tokens=lambda text, force_accurate=False: len(text))
    tool._docs_input_token_limit = lambda: 16
    monkeypatch.setattr(
        "pr_agent.tools.pr_help_docs.clip_tokens",
        lambda *args, **kwargs: pytest.fail("cleaned text already fits"),
    )

    docs = "<!--" + "irrelevant " * 100 + "-->\nUseful text."
    assert tool._trim_docs_input(docs, math.inf) == "Useful text."


def test_trim_docs_passes_cleaned_token_count_to_clipper(monkeypatch):
    tool = PRHelpDocs.__new__(PRHelpDocs)
    tool.token_handler = SimpleNamespace(count_tokens=lambda text, force_accurate=False: len(text))
    tool._docs_input_token_limit = lambda: 10
    calls = []

    def clip(text, limit, *, num_input_tokens):
        calls.append((text, limit, num_input_tokens))
        return "clipped"

    monkeypatch.setattr("pr_agent.tools.pr_help_docs.clip_tokens", clip)
    docs = "<!--" + "irrelevant " * 100 + "-->\nUseful but longer documentation."
    cleaned = "Useful but longer documentation."

    assert tool._trim_docs_input(docs, math.inf) == "clipped"
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
    tool = PRHelpDocs.__new__(PRHelpDocs)
    tool.question = "Where is this documented?"
    tool.return_as_string = True
    tool.vars = {"docs_url": "https://example.test", "question": tool.question, "snippets": ""}
    tool.ai_handler = SimpleNamespace()
    tool.token_handler = SimpleNamespace(count_tokens=lambda text, force_accurate=False: len(text))
    tool._docs_input_token_limit = lambda: limit
    tool._gen_filenames_to_contents_map_from_repo = lambda: docs
    tool._format_model_answer = lambda response, relevant_sections: response
    model_snippets = []
    assert len(aggregate_documentation_files_for_prompt_contents(docs)) > limit

    async def reject_ranking(*args):
        raise AssertionError("Ranking should not run when cleaned documentation fits")

    async def predict(preparator, **kwargs):
        model_snippets.append(preparator._vars["snippets"])
        return "unused"

    tool._rank_docs_and_return_them_as_prompt = reject_ranking
    monkeypatch.setattr("pr_agent.tools.pr_help_docs.retry_with_fallback_models", predict)
    monkeypatch.setattr("pr_agent.tools.pr_help_docs.load_yaml", lambda response: {"response": "answer"})

    assert await tool.run() == "answer"
    assert model_snippets == [tool.vars["snippets"]]
    assert all(path in tool.vars["snippets"] for path in docs)
    assert expected_content in tool.vars["snippets"]
    assert "irrelevant" not in tool.vars["snippets"]
    assert len(tool.vars["snippets"]) <= limit


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

    tool = PRHelpDocs.__new__(PRHelpDocs)
    tool.question = "What is max_jobs?"
    tool.return_as_string = True
    tool.vars = {"docs_url": "https://example.test", "question": tool.question, "snippets": ""}
    tool.ai_handler = SimpleNamespace()
    tool.token_handler = SimpleNamespace(count_tokens=lambda text, force_accurate=False: len(text))
    tool._docs_input_token_limit = lambda: 150
    tool._gen_filenames_to_contents_map_from_repo = lambda: docs
    tool._format_model_answer = lambda response, relevant_sections: response
    rankings = []

    async def rank_docs(*args):
        rankings.append(True)
        return aggregate_documentation_files_for_prompt_contents({"/020-guide.md": docs["/020-guide.md"]})

    async def predict(*args, **kwargs):
        return "unused"

    tool._rank_docs_and_return_them_as_prompt = rank_docs
    monkeypatch.setattr("pr_agent.tools.pr_help_docs.retry_with_fallback_models", predict)
    monkeypatch.setattr("pr_agent.tools.pr_help_docs.load_yaml", lambda response: {"response": "answer"})

    assert await tool.run() == "answer"
    assert rankings == [True]
    assert "/020-guide.md" in tool.vars["snippets"]
    assert "max_jobs = 7" in tool.vars["snippets"]

    # Ranked documents must also retain file boundaries when clipping is needed.
    clipping_inputs = []

    def capture_clip(text, limit, *, num_input_tokens):
        clipping_inputs.append(text)
        return text

    monkeypatch.setattr("pr_agent.tools.pr_help_docs.clip_tokens", capture_clip)
    monkeypatch.setattr(
        "pr_agent.tools.pr_help_docs.load_yaml",
        lambda response: {"relevant_files_ranking": [{"idx": 0}, {"idx": 1}, {"idx": 2}]},
    )
    ranked = await PRHelpDocs._rank_docs_and_return_them_as_prompt(tool, docs, math.inf)

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

    tool = PRHelpDocs.__new__(PRHelpDocs)
    tool.question = "How do I configure the listener?"
    tool.return_as_string = True
    tool.vars = {"docs_url": "https://example.test", "question": tool.question, "snippets": ""}
    tool.ai_handler = SimpleNamespace()
    tool.token_handler = SimpleNamespace(count_tokens=lambda text, force_accurate=False: len(text))
    tool._docs_input_token_limit = lambda: limit
    tool._gen_filenames_to_contents_map_from_repo = lambda: docs
    tool._format_model_answer = lambda response, relevant_sections: response

    responses = iter([{"relevant_files_ranking": [{"idx": 1}]}, {"response": "answer"}])
    model_calls = []

    async def predict(*args, **kwargs):
        model_calls.append(True)
        return "unused"

    monkeypatch.setattr("pr_agent.tools.pr_help_docs.retry_with_fallback_models", predict)
    monkeypatch.setattr("pr_agent.tools.pr_help_docs.load_yaml", lambda response: next(responses))

    assert await tool.run() == "answer"
    assert len(model_calls) == 2
    assert "/config.md" in tool.vars["snippets"]
    assert xml_example in tool.vars["snippets"]
