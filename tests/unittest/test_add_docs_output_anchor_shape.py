from unittest.mock import MagicMock

import pytest
import yaml

from pr_agent.algo.pr_processing import FallbackEligibleError
from pr_agent.commands.pr_add_docs import PRAddDocs


@pytest.mark.parametrize("line", [True, False, 1.9, 0, -1])
def test_add_docs_rejects_invalid_model_line_anchors(line):
    command = object.__new__(PRAddDocs)
    command.prediction = yaml.safe_dump({"Code Documentation": [{
        "relevant file": "app.py", "relevant line": line,
        "doc placement": "after", "documentation": "Explain the function.",
    }]})
    with pytest.raises(FallbackEligibleError):
        command._prepare_pr_code_docs()


@pytest.mark.parametrize("placement", ["sideways", "", "above"])
def test_add_docs_does_not_reinterpret_invalid_placement_as_before(placement):
    command = object.__new__(PRAddDocs)
    command.prediction = yaml.safe_dump({"Code Documentation": [{
        "relevant file": "app.py", "relevant line": 1,
        "doc placement": placement, "documentation": "Explain the function.",
    }]})
    with pytest.raises(FallbackEligibleError):
        command._prepare_pr_code_docs()


def test_direct_doc_publication_does_not_coerce_fractional_anchors():
    command = object.__new__(PRAddDocs)
    command.git_provider = MagicMock()
    command.dedent_code = MagicMock(return_value="original\n# documentation")
    command.push_inline_docs({"Code Documentation": [{
        "relevant file": "app.py", "relevant line": 1.9,
        "doc placement": "after", "documentation": "Explain the function.",
    }]})
    command.dedent_code.assert_not_called()
    command.git_provider.publish_code_suggestions.assert_not_called()


@pytest.mark.parametrize("line", [1, "1"])
def test_add_docs_preserves_positive_integer_anchor_compatibility(line):
    command = object.__new__(PRAddDocs)
    command.prediction = yaml.safe_dump({"Code Documentation": [{
        "relevant file": "app.py", "relevant line": line,
        "doc placement": "before", "documentation": "Explain the function.",
    }]})
    assert command._prepare_pr_code_docs()["Code Documentation"][0]["relevant line"] == line
