import subprocess
import sys
import warnings

from pydantic.warnings import PydanticDeprecatedSince20

from pr_agent.tools.pr_similar_issue import Corpus, IssueLevel, Metadata, Record


def test_metadata_config_uses_configdict_without_deprecation_warning():
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import warnings\n"
            "from pydantic.warnings import PydanticDeprecatedSince20\n"
            "warnings.simplefilter('error', PydanticDeprecatedSince20)\n"
            "from pr_agent.tools.pr_similar_issue import Metadata\n",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    with warnings.catch_warnings(record=True) as recorded_warnings:
        warnings.simplefilter("always")
        meta = Metadata(repo="test/repo", level=IssueLevel.COMMENT)

    assert not any(issubclass(w.category, PydanticDeprecatedSince20) for w in recorded_warnings)
    assert meta.level == "comment"
    assert meta.model_config.get("use_enum_values") is True


def test_metadata_record_and_corpus_integration():
    meta = Metadata(repo="org/repo", username="@user", level=IssueLevel.ISSUE)
    record = Record(id="rec-1", text="some text", metadata=meta)
    corpus = Corpus()
    corpus.append(record)

    assert len(corpus.documents) == 1
    assert corpus.documents[0].metadata.repo == "org/repo"
    assert corpus.documents[0].metadata.level == "issue"
