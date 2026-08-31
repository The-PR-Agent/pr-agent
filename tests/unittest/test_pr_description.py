from unittest.mock import MagicMock, patch

import pytest
import yaml

from pr_agent.algo.types import FilePatchInfo
from pr_agent.tools.pr_description import (
    PRDescription,
    _longest_diagram_chain,
    _parse_diagram_edges,
    apply_diagram_direction,
    sanitize_diagram,
)

KEYS_FIX = ["filename:", "language:", "changes_summary:", "changes_title:", "description:", "title:"]

# Chains named relative to the default pr_diagram_direction_threshold of 5 nodes.
SHORT_CHAIN = 'A --> B --> C'
THRESHOLD_CHAIN = 'A --> B --> C --> D --> E'
LONG_CHAIN = 'A --> B --> C --> D --> E --> F'

def _make_instance(prediction_yaml: str):
    """Create a PRDescription instance, bypassing __init__."""
    with patch.object(PRDescription, '__init__', lambda self, *a, **kw: None):
        obj = PRDescription.__new__(PRDescription)
    obj.prediction = prediction_yaml
    obj.keys_fix = KEYS_FIX
    obj.user_description = ""
    return obj


def _mock_settings(pr_diagram_direction: str = 'adaptive', pr_diagram_direction_threshold: int = 5):
    """Mock get_settings used by _prepare_data."""
    settings = MagicMock()
    settings.pr_description.add_original_user_description = False
    settings.pr_description.pr_diagram_direction = pr_diagram_direction
    settings.pr_description.pr_diagram_direction_threshold = pr_diagram_direction_threshold
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

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_long_chain_diagram_is_flipped_during_prepare_data(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings()
        body = LONG_CHAIN
        obj = _make_instance(_prediction_with_diagram(f'```mermaid\nflowchart LR\n{body}\n```'))
        obj._prepare_data()
        assert obj.data['changes_diagram'] == f'\n```mermaid\nflowchart TD\n{body}\n```'

    @patch('pr_agent.tools.pr_description.get_settings')
    def test_pinned_direction_is_respected_during_prepare_data(self, mock_get_settings):
        mock_get_settings.return_value = _mock_settings(pr_diagram_direction='LR')
        body = LONG_CHAIN
        obj = _make_instance(_prediction_with_diagram(f'```mermaid\nflowchart LR\n{body}\n```'))
        obj._prepare_data()
        assert obj.data['changes_diagram'] == f'\n```mermaid\nflowchart LR\n{body}\n```'

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

    def test_unquoted_middle_label_does_not_create_a_node(self):
        assert _parse_diagram_edges(['A -- calls --> B']) == [('A', 'B')]

    def test_open_link_still_chains(self):
        # `---` is a real link rather than a label opener, so B stays a node.
        assert _parse_diagram_edges(['A --- B --> C']) == [('A', 'B'), ('B', 'C')]

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


def _adapt(diagram: str, direction: str = 'adaptive', threshold: int = 5) -> str:
    """Call the SUT with the settings configuration.toml ships, unless a test overrides them."""
    return apply_diagram_direction(diagram, direction, threshold)


def test_shipped_defaults_match_what_these_tests_assume():
    """Guards the test defaults above against drift in configuration.toml."""
    from pr_agent.config_loader import get_settings
    assert get_settings().pr_description.pr_diagram_direction == 'adaptive'
    assert get_settings().pr_description.pr_diagram_direction_threshold == 5


class TestApplyDiagramDirection:

    @pytest.mark.parametrize('body', [
        pytest.param(f'flowchart LR\n{SHORT_CHAIN}', id='short_chain'),
        pytest.param(f'flowchart LR\n{THRESHOLD_CHAIN}', id='chain_exactly_at_threshold'),
        pytest.param('flowchart LR\n' + '\n'.join(f'A --> {node}' for node in 'BCDEFGHI'), id='wide_fan_out'),
        pytest.param('sequenceDiagram\nA->>B: hello', id='not_a_flowchart'),
        pytest.param('flowchart LR\nA["only a node"]', id='no_edges'),
        pytest.param(f'flowchart LR\n{LONG_CHAIN} --> A', id='cycle'),
    ])
    def test_diagram_is_left_untouched(self, body):
        diagram = _fenced(body)
        assert _adapt(diagram) == diagram

    @pytest.mark.parametrize('header_in, header_out', [
        pytest.param('flowchart LR', 'flowchart TD', id='flowchart'),
        pytest.param('graph LR', 'graph TD', id='graph_alias'),
        pytest.param('  graph LR;', '  graph TD;', id='indent_and_semicolon_preserved'),
    ])
    def test_long_chain_becomes_vertical(self, header_in, header_out):
        assert _adapt(_fenced(f'{header_in}\n{LONG_CHAIN}')) == _fenced(f'{header_out}\n{LONG_CHAIN}')

    def test_vertical_short_diagram_is_flipped_back_to_horizontal(self):
        assert _adapt(_fenced('flowchart TD\nA --> B')) == _fenced('flowchart LR\nA --> B')

    def test_explicit_direction_pins_and_ignores_shape(self):
        diagram = _fenced(f'flowchart LR\n{LONG_CHAIN}')
        assert _adapt(diagram, direction='LR') == diagram
        assert _adapt(_fenced('flowchart LR\nA --> B'), direction='TD') == _fenced('flowchart TD\nA --> B')

    @pytest.mark.parametrize('direction', ['adaptive', 'ADAPTIVE', ' adaptive ', 'sideways', '', None])
    def test_unrecognised_direction_falls_back_to_adaptive(self, direction):
        assert _adapt(_fenced(f'flowchart LR\n{LONG_CHAIN}'), direction=direction) == \
            _fenced(f'flowchart TD\n{LONG_CHAIN}')

    def test_custom_threshold_is_honoured(self):
        assert _adapt(_fenced(f'flowchart LR\n{SHORT_CHAIN}'), threshold=2) == \
            _fenced(f'flowchart TD\n{SHORT_CHAIN}')

    def test_unparseable_threshold_leaves_diagram_untouched(self):
        diagram = _fenced(f'flowchart LR\n{LONG_CHAIN}')
        assert _adapt(diagram, threshold='not-a-number') == diagram

    def test_subgraph_edges_are_counted(self):
        body = 'subgraph one\nA --> B --> C\nend\nsubgraph two\nC --> D --> E --> F\nend'
        assert _adapt(_fenced(f'flowchart LR\n{body}')) == _fenced(f'flowchart TD\n{body}')

    def test_unquoted_edge_labels_do_not_inflate_the_chain(self):
        # Five real nodes joined by unquoted labels: the labels must not count as nodes.
        body = 'A -- calls --> B -- reads --> C -- writes --> D -- returns --> E'
        diagram = _fenced(f'flowchart LR\n{body}')
        assert _adapt(diagram) == diagram
