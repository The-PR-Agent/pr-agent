from unittest.mock import MagicMock, patch

import pytest
import yaml

from pr_agent.algo.types import FilePatchInfo
from pr_agent.tools.pr_description import (PRDescription,
                                           _longest_diagram_chain,
                                           _parse_diagram_edges,
                                           apply_diagram_direction,
                                           sanitize_diagram)

KEYS_FIX = ["filename:", "language:", "changes_summary:", "changes_title:", "description:", "title:"]

def _make_instance(prediction_yaml: str):
    """Create a PRDescription instance, bypassing __init__."""
    with patch.object(PRDescription, '__init__', lambda self, *a, **kw: None):
        obj = PRDescription.__new__(PRDescription)
    obj.prediction = prediction_yaml
    obj.keys_fix = KEYS_FIX
    obj.user_description = ""
    return obj


def _mock_settings():
    """Mock get_settings used by _prepare_data."""
    settings = MagicMock()
    settings.pr_description.add_original_user_description = False
    return settings


def _prediction_with_diagram(diagram_value: str) -> str:
    """Build a minimal YAML prediction string that includes changes_diagram."""
    return yaml.dump({
        'title': 'test',
        'description': 'test',
        'changes_diagram': diagram_value,
    })


class TestPRDescriptionDiagram:

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_diagram_not_starting_with_fence_is_removed(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings()
        obj = _make_instance(_prediction_with_diagram('graph LR\nA --> B'))
        obj._prepare_data()
        assert 'changes_diagram' not in obj.data

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_diagram_missing_closing_fence_is_appended(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings()
        obj = _make_instance(_prediction_with_diagram('```mermaid\ngraph LR\nA --> B'))
        obj._prepare_data()
        assert obj.data['changes_diagram'] == '\n```mermaid\ngraph LR\nA --> B\n```'

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_backticks_inside_label_are_removed(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings()
        obj = _make_instance(_prediction_with_diagram('```mermaid\ngraph LR\nA["`file`"] --> B\n```'))
        obj._prepare_data()
        assert obj.data['changes_diagram'] == '\n```mermaid\ngraph LR\nA["file"] --> B\n```'

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_backticks_outside_label_are_kept(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings()
        obj = _make_instance(_prediction_with_diagram('```mermaid\ngraph LR\nA["`file`"] -->|`edge`| B\n```'))
        obj._prepare_data()
        assert obj.data['changes_diagram'] == '\n```mermaid\ngraph LR\nA["file"] -->|`edge`| B\n```'

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_normal_diagram_only_adds_newline(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings()
        obj = _make_instance(_prediction_with_diagram('```mermaid\ngraph LR\nA["file.py"] --> B["output"]\n```'))
        obj._prepare_data()
        assert obj.data['changes_diagram'] == '\n```mermaid\ngraph LR\nA["file.py"] --> B["output"]\n```'

    def test_none_input_returns_empty(self):
        assert sanitize_diagram(None) == ''

    def test_non_string_input_returns_empty(self):
        assert sanitize_diagram(123) == ''

    def test_non_mermaid_fence_returns_empty(self):
        assert sanitize_diagram('```python\nprint("hello")\n```') == ''


class TestPRDescriptionCore:
    def test_prepare_file_labels_groups_valid_files_and_skips_incomplete_entries(self):
        obj = _make_instance("")
        obj.pr_id = "1"
        obj.vars = {"include_file_summary_changes": True}
        obj.data = {
            "pr_files": [
                {
                    "filename": "src/app.py",
                    "changes_title": "Add cache",
                    "changes_summary": "Adds a bounded cache.",
                    "label": "backend",
                },
                {
                    "filename": "src/skip.py",
                    "changes_title": "Missing summary",
                    "label": "backend",
                },
                {
                    "filename": "docs/readme.md",
                    "changes_title": "Update docs",
                    "changes_summary": "Clarifies setup.",
                    "label": "docs",
                },
            ]
        }

        labels = obj._prepare_file_labels()

        assert labels == {
            "backend": [("src/app.py", "Add cache", "Adds a bounded cache.")],
            "docs": [("docs/readme.md", "Update docs", "Clarifies setup.")],
        }

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_prepare_pr_answer_with_markers_replaces_plain_and_comment_markers(self, mock_get_settings):
        settings = MagicMock()
        settings.pr_description.generate_ai_title = True
        settings.pr_description.include_generated_by_header = False
        mock_get_settings.return_value = settings
        obj = _make_instance("")
        obj.pr_id = "1"
        obj.vars = {"title": "Original title"}
        obj.file_label_dict = {}
        obj.git_provider = MagicMock()
        obj.git_provider.last_commit_id.sha = "abc123"
        obj.user_description = (
            "pr_agent:type\n"
            "pr_agent:summary\n"
            "<!-- pr_agent:diagram -->\n"
        )
        obj.data = {
            "title": "AI title",
            "type": "Bug fix",
            "description": "Fixes the cache invalidation bug.",
            "changes_diagram": "\n```mermaid\ngraph LR\nA --> B\n```",
        }

        title, body, walkthrough, file_changes = obj._prepare_pr_answer_with_markers()

        assert title == "AI title"
        assert "Bug fix" in body
        assert "Fixes the cache invalidation bug." in body
        assert "```mermaid" in body
        assert walkthrough == ""
        assert file_changes == []

    @pytest.mark.asyncio
    async def test_extend_uncovered_files_adds_missing_diff_files_to_prediction(self):
        obj = _make_instance("")
        obj.pr_id = "1"
        obj.git_provider = MagicMock()
        obj.git_provider.get_diff_files.return_value = [
            FilePatchInfo("", "", "", "shown.py"),
            FilePatchInfo("", "", "", "missing.py"),
        ]
        prediction = """
pr_files:
  - filename: shown.py
    changes_title: Existing summary
    label: backend
"""

        extended = await obj.extend_uncovered_files(prediction)
        loaded = yaml.safe_load(extended)

        assert [file["filename"].strip() for file in loaded["pr_files"]] == ["shown.py", "missing.py"]
        assert loaded["pr_files"][1]["label"].strip() == "additional files"


class TestDiagramEdgeParsing:

    def test_simple_edge(self):
        assert _parse_diagram_edges(['A --> B']) == [('A', 'B')]

    def test_chained_statement_becomes_consecutive_edges(self):
        assert _parse_diagram_edges(['A --> B --> C']) == [('A', 'B'), ('B', 'C')]

    def test_node_shapes_are_stripped(self):
        assert _parse_diagram_edges(['A["file.py"] --> B("output")']) == [('A', 'B')]

    def test_quoted_middle_label_does_not_create_a_node(self):
        assert _parse_diagram_edges(['A -- "calls" --> B']) == [('A', 'B')]

    def test_pipe_edge_label_does_not_create_a_node(self):
        assert _parse_diagram_edges(['A -->|calls| B']) == [('A', 'B')]

    def test_arrow_inside_a_label_is_not_an_edge(self):
        assert _parse_diagram_edges(['A["a --> b"]']) == []

    @pytest.mark.parametrize('line', ['A --- B', 'A -.-> B', 'A ==> B', 'A --o B'])
    def test_arrow_variants(self, line):
        assert _parse_diagram_edges([line]) == [('A', 'B')]

    def test_fan_out_shorthand_expands(self):
        assert _parse_diagram_edges(['A --> B & C']) == [('A', 'B'), ('A', 'C')]

    def test_structural_statements_are_ignored(self):
        lines = ['subgraph one', 'direction LR', 'A --> B', 'end', 'style A fill:#fff', '%% A --> Z']
        assert _parse_diagram_edges(lines) == [('A', 'B')]

    def test_fence_and_frontmatter_lines_produce_no_edges(self):
        assert _parse_diagram_edges(['```', '---', 'config:', '---']) == []


class TestLongestDiagramChain:

    def test_empty_graph_is_zero(self):
        assert _longest_diagram_chain([]) == 0

    def test_chain_length_counts_nodes(self):
        assert _longest_diagram_chain([('A', 'B'), ('B', 'C')]) == 3

    def test_fan_out_is_two_regardless_of_width(self):
        edges = [('A', chr(ord('B') + i)) for i in range(8)]
        assert _longest_diagram_chain(edges) == 2

    def test_longest_branch_wins(self):
        assert _longest_diagram_chain([('A', 'B'), ('B', 'C'), ('C', 'D'), ('A', 'E')]) == 4

    def test_cycle_raises(self):
        with pytest.raises(ValueError):
            _longest_diagram_chain([('A', 'B'), ('B', 'A')])


def _fenced(body: str) -> str:
    return f'\n```mermaid\n{body}\n```'


class TestApplyDiagramDirection:

    def test_short_chain_stays_horizontal(self):
        diagram = _fenced('flowchart LR\nA --> B --> C')
        assert apply_diagram_direction(diagram) == diagram

    def test_long_chain_becomes_vertical(self):
        body = 'A --> B --> C --> D --> E --> F'
        assert apply_diagram_direction(_fenced(f'flowchart LR\n{body}')) == _fenced(f'flowchart TD\n{body}')

    def test_chain_exactly_at_threshold_stays_horizontal(self):
        diagram = _fenced('flowchart LR\nA --> B --> C --> D --> E')
        assert apply_diagram_direction(diagram) == diagram

    def test_wide_fan_out_stays_horizontal(self):
        body = 'A --> B\nA --> C\nA --> D\nA --> E\nA --> F\nA --> G\nA --> H\nA --> I'
        diagram = _fenced(f'flowchart LR\n{body}')
        assert apply_diagram_direction(diagram) == diagram

    def test_graph_alias_is_rewritten_too(self):
        body = 'A --> B --> C --> D --> E --> F'
        assert apply_diagram_direction(_fenced(f'graph LR\n{body}')) == _fenced(f'graph TD\n{body}')

    def test_vertical_short_diagram_is_flipped_back_to_horizontal(self):
        assert apply_diagram_direction(_fenced('flowchart TD\nA --> B')) == _fenced('flowchart LR\nA --> B')

    def test_explicit_direction_pins_and_ignores_shape(self):
        diagram = _fenced('flowchart LR\nA --> B --> C --> D --> E --> F')
        assert apply_diagram_direction(diagram, direction='LR') == diagram
        assert apply_diagram_direction(_fenced('flowchart LR\nA --> B'), direction='TD') == \
            _fenced('flowchart TD\nA --> B')

    def test_custom_threshold_is_honoured(self):
        body = 'A --> B --> C'
        assert apply_diagram_direction(_fenced(f'flowchart LR\n{body}'), threshold=2) == \
            _fenced(f'flowchart TD\n{body}')

    def test_indentation_and_trailing_semicolon_are_preserved(self):
        body = 'A --> B --> C --> D --> E --> F'
        assert apply_diagram_direction(_fenced(f'  graph LR;\n{body}')) == _fenced(f'  graph TD;\n{body}')

    def test_sequence_diagram_is_untouched(self):
        diagram = _fenced('sequenceDiagram\nA->>B: hello')
        assert apply_diagram_direction(diagram) == diagram

    def test_diagram_without_edges_is_untouched(self):
        diagram = _fenced('flowchart LR\nA["only a node"]')
        assert apply_diagram_direction(diagram) == diagram

    def test_cyclic_graph_is_untouched(self):
        diagram = _fenced('flowchart LR\nA --> B --> C --> D --> E --> F --> A')
        assert apply_diagram_direction(diagram) == diagram

    def test_unparseable_threshold_leaves_diagram_untouched(self):
        diagram = _fenced('flowchart LR\nA --> B --> C --> D --> E --> F')
        assert apply_diagram_direction(diagram, threshold='not-a-number') == diagram

    def test_subgraph_edges_are_counted(self):
        body = 'subgraph one\nA --> B --> C\nend\nsubgraph two\nC --> D --> E --> F\nend'
        assert apply_diagram_direction(_fenced(f'flowchart LR\n{body}')) == _fenced(f'flowchart TD\n{body}')
