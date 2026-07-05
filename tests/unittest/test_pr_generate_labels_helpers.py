"""Unit tests for pr_agent/tools/pr_generate_labels.py (PRGenerateLabels).

Instances are built with ``__new__`` and only the attributes needed by the
method under test are populated — no git provider, no TokenHandler, no model
calls. The focus is the prediction-parsing path:
``_prepare_data`` (YAML -> dict) and ``_prepare_labels`` (dict -> label list).
"""

from pr_agent.tools.pr_generate_labels import PRGenerateLabels


def _make_tool(data=None, variables=None) -> PRGenerateLabels:
    obj = PRGenerateLabels.__new__(PRGenerateLabels)
    obj.pr_id = "owner/repo/1"
    if data is not None:
        obj.data = data
    if variables is not None:
        obj.variables = variables
    return obj


class TestPrepareLabels:
    def test_labels_as_list(self):
        tool = _make_tool(data={"labels": ["Bug fix", "Tests"]}, variables={})
        assert tool._prepare_labels() == ["Bug fix", "Tests"]

    def test_labels_as_list_are_stripped(self):
        tool = _make_tool(data={"labels": ["  Bug fix ", "\tTests\n"]}, variables={})
        assert tool._prepare_labels() == ["Bug fix", "Tests"]

    def test_labels_as_comma_separated_string(self):
        tool = _make_tool(data={"labels": "Bug fix, Tests ,Enhancement"}, variables={})
        assert tool._prepare_labels() == ["Bug fix", "Tests", "Enhancement"]

    def test_missing_labels_key_returns_empty_list(self):
        tool = _make_tool(data={"something_else": 1}, variables={})
        assert tool._prepare_labels() == []

    def test_labels_of_unsupported_type_returns_empty_list(self):
        # neither list nor str -> both isinstance branches are skipped
        tool = _make_tool(data={"labels": {"a": 1}}, variables={})
        assert tool._prepare_labels() == []

    def test_lowercase_labels_are_mapped_back_to_original_case(self):
        variables = {
            "labels_minimal_to_labels_dict": {
                "bug fix": "Bug Fix",
                "tests": "Tests",
            }
        }
        tool = _make_tool(data={"labels": "bug fix, tests, other"}, variables=variables)
        assert tool._prepare_labels() == ["Bug Fix", "Tests", "other"]

    def test_no_minimal_dict_in_variables_keeps_labels_as_is(self):
        tool = _make_tool(data={"labels": ["bug fix"]}, variables={})
        assert tool._prepare_labels() == ["bug fix"]

    def test_missing_variables_attribute_is_swallowed(self):
        # self.variables was never set (e.g. prediction path short-circuited);
        # the AttributeError must be caught and the parsed labels returned anyway
        tool = _make_tool(data={"labels": ["Bug fix"]})
        assert not hasattr(tool, "variables")
        assert tool._prepare_labels() == ["Bug fix"]


class TestPrepareData:
    def test_parses_canned_yaml_prediction(self):
        tool = _make_tool()
        tool.prediction = "labels:\n  - Bug fix\n  - Enhancement\n"
        tool._prepare_data()
        assert tool.data == {"labels": ["Bug fix", "Enhancement"]}

    def test_parses_prediction_with_surrounding_whitespace(self):
        tool = _make_tool()
        tool.prediction = "\n\nlabels: Bug fix, Tests\n\n"
        tool._prepare_data()
        assert tool.data == {"labels": "Bug fix, Tests"}

    def test_end_to_end_yaml_prediction_to_labels(self):
        tool = _make_tool(variables={"labels_minimal_to_labels_dict": {"bug fix": "Bug fix"}})
        tool.prediction = "labels:\n  - bug fix\n  - documentation\n"
        tool._prepare_data()
        assert tool._prepare_labels() == ["Bug fix", "documentation"]
