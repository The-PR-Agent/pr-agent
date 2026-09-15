"""Regression tests for the question-mode /help documentation corpus."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pr_agent.tools import pr_help_message
from pr_agent.tools.pr_help_message import HELP_DOCS_UNAVAILABLE_MESSAGE, PRHelpMessage


def _write_document(root: Path, relative_path: str, content: str) -> Path:
    document = root / relative_path
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_text(content, encoding="utf-8")
    return document


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [
        ("tools/review.md", True),
        ("tools/review.txt", False),
        ("core-abilities/compression_strategy.md", False),
        ("finetuning_benchmark/sample.md", False),
        ("nested/finetuning_benchmark/sample.md", False),
    ],
)
def test_help_doc_inclusion_predicate(relative_path, expected):
    assert pr_help_message._is_help_doc_included(pr_help_message.PurePosixPath(relative_path)) is expected


def test_packaged_help_docs_are_filtered_and_priority_sorted(tmp_path, monkeypatch):
    package_root = tmp_path / "pr_agent"
    docs_root = package_root / "_help_docs"
    _write_document(docs_root, "index.md", "root")
    _write_document(docs_root, "tools/review.md", "review")
    _write_document(docs_root, "reference/other.md", "other")
    _write_document(docs_root, "core-abilities/compression_strategy.md", "excluded")
    _write_document(docs_root, "finetuning_benchmark/sample.md", "excluded")
    monkeypatch.setattr(pr_help_message, "package_files", lambda _package: package_root)

    prompt, available_docs_files = pr_help_message._load_help_docs_prompt()

    assert prompt.index("/index.md") < prompt.index("/tools/review.md") < prompt.index("/reference/other.md")
    assert "compression_strategy" not in prompt
    assert "finetuning_benchmark" not in prompt
    assert available_docs_files == {"index.md", "tools/review.md", "reference/other.md"}


def test_source_checkout_is_used_when_packaged_docs_are_absent(tmp_path, monkeypatch):
    package_root = tmp_path / "installed" / "pr_agent"
    package_root.mkdir(parents=True)
    source_module = tmp_path / "checkout" / "pr_agent" / "tools" / "pr_help_message.py"
    _write_document(tmp_path / "checkout" / "docs" / "docs", "tools/review.md", "source checkout")
    monkeypatch.setattr(pr_help_message, "package_files", lambda _package: package_root)
    monkeypatch.setattr(pr_help_message, "__file__", str(source_module))

    prompt, available_docs_files = pr_help_message._load_help_docs_prompt()

    assert "/tools/review.md" in prompt
    assert "source checkout" in prompt
    assert available_docs_files == {"tools/review.md"}


def test_source_fallback_preserves_packaged_resource_traceback(tmp_path, monkeypatch):
    source_module = tmp_path / "checkout" / "pr_agent" / "tools" / "pr_help_message.py"
    source_docs = tmp_path / "checkout" / "docs" / "docs"
    _write_document(source_docs, "tools/review.md", "source checkout")
    logger = Mock()

    def fail_package_lookup(_package):
        raise OSError("broken resource provider")

    monkeypatch.setattr(pr_help_message, "package_files", fail_package_lookup)
    monkeypatch.setattr(pr_help_message, "__file__", str(source_module))
    monkeypatch.setattr(pr_help_message, "get_logger", lambda: logger)

    assert pr_help_message._get_help_docs_root() == source_docs
    logger.opt.assert_called_once_with(exception=True)
    logger.opt.return_value.debug.assert_called_once_with(
        "Unable to inspect packaged /help documentation"
    )


def test_traversal_failures_preserve_tracebacks(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    broken_document = _write_document(docs_root, "broken.md", "unreadable metadata")
    original_is_dir = Path.is_dir
    logger = Mock()

    def is_dir(path):
        if path == broken_document:
            raise OSError("broken stat")
        return original_is_dir(path)

    monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(pr_help_message, "get_logger", lambda: logger)

    assert pr_help_message._iter_help_docs(docs_root) == []
    logger.opt.assert_called_once_with(exception=True)
    logger.opt.return_value.error.assert_called_once_with(
        f"Error while inspecting the documentation path {broken_document}"
    )


def test_directory_listing_failure_preserves_traceback(tmp_path, monkeypatch):
    docs_root = tmp_path / "docs"
    docs_root.mkdir()
    original_iterdir = Path.iterdir
    logger = Mock()

    def iterdir(path):
        if path == docs_root:
            raise OSError("broken listing")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    monkeypatch.setattr(pr_help_message, "get_logger", lambda: logger)

    assert pr_help_message._iter_help_docs(docs_root) == []
    logger.opt.assert_called_once_with(exception=True)
    logger.opt.return_value.error.assert_called_once_with(
        f"Error while listing the documentation directory {docs_root}"
    )


def test_unreadable_document_is_skipped_when_another_document_loads(tmp_path, monkeypatch):
    package_root = tmp_path / "pr_agent"
    docs_root = package_root / "_help_docs"
    _write_document(docs_root, "good.md", "readable")
    bad_document = _write_document(docs_root, "bad.md", "unreadable")
    _write_document(docs_root, "empty.md", " \n\t")
    original_read_text = Path.read_text
    logger = Mock()

    def read_text(document, *args, **kwargs):
        if document == bad_document:
            raise OSError("permission denied")
        return original_read_text(document, *args, **kwargs)

    monkeypatch.setattr(pr_help_message, "package_files", lambda _package: package_root)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(pr_help_message, "get_logger", lambda: logger)

    prompt, available_docs_files = pr_help_message._load_help_docs_prompt()

    assert "readable" in prompt
    assert "/bad.md" not in prompt
    assert "/empty.md" not in prompt
    assert available_docs_files == {"good.md"}
    logger.opt.assert_called_once_with(exception=True)
    logger.opt.return_value.error.assert_called_once_with(
        f"Error while reading the documentation file {bad_document}"
    )


@pytest.mark.asyncio
async def test_missing_corpus_fails_before_model_attempt(tmp_path, monkeypatch):
    package_root = tmp_path / "installed" / "pr_agent"
    package_root.mkdir(parents=True)
    retry = AsyncMock()
    logger = Mock()
    monkeypatch.setattr(pr_help_message, "package_files", lambda _package: package_root)
    monkeypatch.setattr(
        pr_help_message,
        "__file__",
        str(tmp_path / "checkout" / "pr_agent" / "tools" / "pr_help_message.py"),
    )
    monkeypatch.setattr(pr_help_message, "retry_with_fallback_models", retry)
    monkeypatch.setattr(pr_help_message, "get_logger", lambda: logger)
    monkeypatch.setattr(
        pr_help_message,
        "get_settings",
        lambda: SimpleNamespace(config=SimpleNamespace(publish_output=True)),
    )

    tool = PRHelpMessage.__new__(PRHelpMessage)
    tool.git_provider = SimpleNamespace(
        pr_url="https://example.com/org/repo/pull/1",
        publish_comment=Mock(),
    )
    tool.ai_handler = SimpleNamespace(chat_completion=AsyncMock())
    tool.question_str = "How does review work?"
    tool.return_as_string = False
    tool.vars = {"question": tool.question_str, "snippets": ""}

    with pytest.raises(FileNotFoundError):
        await tool.run()
    retry.assert_not_awaited()
    tool.ai_handler.chat_completion.assert_not_awaited()
    tool.git_provider.publish_comment.assert_called_once_with(HELP_DOCS_UNAVAILABLE_MESSAGE)
    logger.exception.assert_called_once()
    assert "Unable to load the PR-Agent help documentation" in logger.exception.call_args.args[0]


@pytest.mark.parametrize("failure_method", ["resolve", "is_dir"])
@pytest.mark.asyncio
async def test_source_fallback_failure_notifies_before_model_attempt(tmp_path, monkeypatch, failure_method):
    package_root = tmp_path / "installed" / "pr_agent"
    package_root.mkdir(parents=True)
    source_module = tmp_path / "checkout" / "pr_agent" / "tools" / "pr_help_message.py"
    source_error = OSError("broken source checkout")
    retry = AsyncMock()
    logger = Mock()
    source_docs = source_module.resolve().parents[2] / "docs" / "docs"

    monkeypatch.setattr(pr_help_message, "package_files", lambda _package: package_root)
    monkeypatch.setattr(pr_help_message, "__file__", str(source_module))
    if failure_method == "resolve":
        original_resolve = Path.resolve

        def resolve(path, *args, **kwargs):
            if path == source_module:
                raise source_error
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", resolve)
    else:
        original_is_dir = Path.is_dir

        def is_dir(path):
            if path == source_docs:
                raise source_error
            return original_is_dir(path)

        monkeypatch.setattr(Path, "is_dir", is_dir)
    monkeypatch.setattr(pr_help_message, "retry_with_fallback_models", retry)
    monkeypatch.setattr(pr_help_message, "get_logger", lambda: logger)
    monkeypatch.setattr(
        pr_help_message,
        "get_settings",
        lambda: SimpleNamespace(config=SimpleNamespace(publish_output=True)),
    )

    tool = PRHelpMessage.__new__(PRHelpMessage)
    tool.git_provider = SimpleNamespace(
        pr_url="https://example.com/org/repo/pull/1",
        publish_comment=Mock(),
    )
    tool.ai_handler = SimpleNamespace(chat_completion=AsyncMock())
    tool.question_str = "How does review work?"
    tool.return_as_string = False
    tool.vars = {"question": tool.question_str, "snippets": ""}

    with pytest.raises(FileNotFoundError) as raised:
        await tool.run()

    assert raised.value.__cause__ is source_error
    retry.assert_not_awaited()
    tool.ai_handler.chat_completion.assert_not_awaited()
    tool.git_provider.publish_comment.assert_called_once_with(HELP_DOCS_UNAVAILABLE_MESSAGE)
    logger.opt.assert_called_once_with(exception=True)
    logger.opt.return_value.error.assert_called_once_with(
        "Unable to inspect source-tree /help documentation"
    )
    logger.exception.assert_called_once_with("Unable to load the PR-Agent help documentation")


@pytest.mark.asyncio
async def test_unreadable_corpus_fails_before_model_attempt(tmp_path, monkeypatch):
    package_root = tmp_path / "pr_agent"
    docs_root = package_root / "_help_docs"
    unreadable_document = _write_document(docs_root, "tools/review.md", "unreadable")
    retry = AsyncMock()
    logger = Mock()
    original_read_text = Path.read_text

    def read_text(document, *args, **kwargs):
        if document == unreadable_document:
            raise OSError("permission denied")
        return original_read_text(document, *args, **kwargs)

    monkeypatch.setattr(pr_help_message, "package_files", lambda _package: package_root)
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(pr_help_message, "retry_with_fallback_models", retry)
    monkeypatch.setattr(pr_help_message, "get_logger", lambda: logger)
    monkeypatch.setattr(
        pr_help_message,
        "get_settings",
        lambda: SimpleNamespace(config=SimpleNamespace(publish_output=False)),
    )

    tool = PRHelpMessage.__new__(PRHelpMessage)
    tool.git_provider = SimpleNamespace(
        pr_url="https://example.com/org/repo/pull/1",
        publish_comment=Mock(),
    )
    tool.ai_handler = SimpleNamespace(chat_completion=AsyncMock())
    tool.question_str = "How does review work?"
    tool.return_as_string = False
    tool.vars = {"question": tool.question_str, "snippets": ""}

    with pytest.raises(FileNotFoundError):
        await tool.run()
    retry.assert_not_awaited()
    tool.ai_handler.chat_completion.assert_not_awaited()
    tool.git_provider.publish_comment.assert_not_called()
    logger.opt.assert_called_once_with(exception=True)
    logger.opt.return_value.error.assert_called_once_with(
        f"Error while reading the documentation file {unreadable_document}"
    )
    logger.exception.assert_called_once()
    assert "Unable to load the PR-Agent help documentation" in logger.exception.call_args.args[0]


@pytest.mark.asyncio
async def test_empty_corpus_fails_before_model_attempt(tmp_path, monkeypatch):
    package_root = tmp_path / "pr_agent"
    docs_root = package_root / "_help_docs"
    _write_document(docs_root, "empty.md", "")
    _write_document(docs_root, "whitespace.md", " \n\t")
    retry = AsyncMock()
    logger = Mock()
    monkeypatch.setattr(pr_help_message, "package_files", lambda _package: package_root)
    monkeypatch.setattr(pr_help_message, "retry_with_fallback_models", retry)
    monkeypatch.setattr(pr_help_message, "get_logger", lambda: logger)
    monkeypatch.setattr(
        pr_help_message,
        "get_settings",
        lambda: SimpleNamespace(config=SimpleNamespace(publish_output=False)),
    )

    tool = PRHelpMessage.__new__(PRHelpMessage)
    tool.git_provider = SimpleNamespace(
        pr_url="https://example.com/org/repo/pull/1",
        publish_comment=Mock(),
    )
    tool.ai_handler = SimpleNamespace(chat_completion=AsyncMock())
    tool.question_str = "How does review work?"
    tool.return_as_string = False
    tool.vars = {"question": tool.question_str, "snippets": ""}

    with pytest.raises(FileNotFoundError):
        await tool.run()
    retry.assert_not_awaited()
    tool.ai_handler.chat_completion.assert_not_awaited()
    tool.git_provider.publish_comment.assert_not_called()
    logger.exception.assert_called_once()
    assert "Unable to load the PR-Agent help documentation" in logger.exception.call_args.args[0]


@pytest.mark.asyncio
async def test_notification_failure_preserves_original_corpus_error(monkeypatch):
    original_error = FileNotFoundError("missing corpus")
    retry = AsyncMock()
    logger = Mock()

    def fail_load():
        raise original_error

    monkeypatch.setattr(pr_help_message, "_load_help_docs_prompt", fail_load)
    monkeypatch.setattr(pr_help_message, "retry_with_fallback_models", retry)
    monkeypatch.setattr(pr_help_message, "get_logger", lambda: logger)
    monkeypatch.setattr(
        pr_help_message,
        "get_settings",
        lambda: SimpleNamespace(config=SimpleNamespace(publish_output=True)),
    )

    tool = PRHelpMessage.__new__(PRHelpMessage)
    tool.git_provider = SimpleNamespace(
        pr_url="https://example.com/org/repo/pull/1",
        publish_comment=Mock(side_effect=RuntimeError("provider unavailable")),
    )
    tool.ai_handler = SimpleNamespace(chat_completion=AsyncMock())
    tool.question_str = "How does review work?"
    tool.return_as_string = False
    tool.vars = {"question": tool.question_str, "snippets": ""}

    with pytest.raises(FileNotFoundError) as raised:
        await tool.run()

    assert raised.value is original_error
    retry.assert_not_awaited()
    tool.ai_handler.chat_completion.assert_not_awaited()
    tool.git_provider.publish_comment.assert_called_once_with(HELP_DOCS_UNAVAILABLE_MESSAGE)
    assert logger.exception.call_count == 2
    assert logger.exception.call_args_list[0].args == ("Unable to load the PR-Agent help documentation",)
    assert logger.exception.call_args_list[1].args == (
        "Unable to publish the help documentation failure message",
    )
