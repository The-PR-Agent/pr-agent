from unittest.mock import MagicMock

import pytest
import yaml

from pr_agent.algo.pr_processing import FallbackEligibleError
from pr_agent.tools.pr_add_docs import PRAddDocs


@pytest.mark.parametrize("line", [True, False, 1.9, 0, -1])
def test_add_docs_rejects_invalid_model_line_anchors(line):
    tool = object.__new__(PRAddDocs)
    tool.prediction = yaml.safe_dump({"Code Documentation": [{
        "relevant file": "app.py", "relevant line": line,
        "doc placement": "after", "documentation": "Explain the function.",
    }]})
    with pytest.raises(FallbackEligibleError):
        tool._prepare_pr_code_docs()


@pytest.mark.parametrize("placement", ["sideways", "", "above"])
def test_add_docs_does_not_reinterpret_invalid_placement_as_before(placement):
    tool = object.__new__(PRAddDocs)
    tool.prediction = yaml.safe_dump({"Code Documentation": [{
        "relevant file": "app.py", "relevant line": 1,
        "doc placement": placement, "documentation": "Explain the function.",
    }]})
    with pytest.raises(FallbackEligibleError):
        tool._prepare_pr_code_docs()


def test_direct_doc_publication_does_not_coerce_fractional_anchors():
    tool = object.__new__(PRAddDocs)
    tool.git_provider = MagicMock()
    tool.dedent_code = MagicMock(return_value="original\n# documentation")
    tool.push_inline_docs({"Code Documentation": [{
        "relevant file": "app.py", "relevant line": 1.9,
        "doc placement": "after", "documentation": "Explain the function.",
    }]})
    tool.dedent_code.assert_not_called()
    tool.git_provider.publish_code_suggestions.assert_not_called()
