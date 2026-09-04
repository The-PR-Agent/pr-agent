from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from jinja2 import Environment, StrictUndefined

from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.utils import load_yaml
from pr_agent.config_loader import get_settings
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


def _make_large_pr_instance(diff_files=None):
    """Create a PRDescription instance configured for _prepare_prediction testing."""
    with patch.object(PRDescription, '__init__', lambda self, *a, **kw: None):
        obj = PRDescription.__new__(PRDescription)
    obj.pr_id = "1"
    obj.user_description = ""
    obj.keys_fix = KEYS_FIX
    obj.git_provider = MagicMock()
    obj.git_provider.pr = MagicMock()
    obj.git_provider.get_diff_files.return_value = diff_files or [
        FilePatchInfo("", "", "", "src/file1.py"),
        FilePatchInfo("", "", "", "src/file2.py"),
    ]
    obj.token_handler = MagicMock()
    obj.vars = {
        "title": "Test PR",
        "branch": "feature",
        "description": "old desc",
        "language": "Python",
        "diff": "",
        "extra_instructions": "",
        "skills_context": "",
        "repo_context": "",
        "commit_messages_str": "feat: initial",
        "enable_custom_labels": False,
        "custom_labels_class": "",
        "enable_semantic_files_types": True,
        "related_tickets": "",
        "include_file_summary_changes": True,
        "duplicate_prompt_examples": False,
        "enable_pr_diagram": False,
        "enable_pr_description": True,
    }
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

    @pytest.mark.parametrize("diagram", [
        "Mention the ```mermaid marker inline without a diagram.",
        "```mermaid-extra\ngraph LR\nA --> B\n```",
    ])
    def test_mermaid_marker_that_is_not_a_fence_returns_empty(self, diagram):
        assert sanitize_diagram(diagram) == ''

    def test_fence_info_string_after_mermaid_is_kept(self):
        diagram = sanitize_diagram('```mermaid title="flow"\ngraph LR\nA --> B\n```')

        assert diagram == '\n```mermaid title="flow"\ngraph LR\nA --> B\n```'

    def test_leading_and_trailing_prose_around_mermaid_fence_is_ignored(self):
        diagram = sanitize_diagram(
            'Here is the requested diagram:\n'
            '```mermaid\n'
            'graph LR\n'
            'A --> B\n'
            '```\n'
            'The diagram shows the main flow.'
        )

        assert diagram == '\n```mermaid\ngraph LR\nA --> B\n```'

    def test_backticks_inside_a_label_do_not_end_the_fence(self):
        diagram = sanitize_diagram('```mermaid\ngraph LR\nA["```file```"] --> B\n```')

        assert diagram == '\n```mermaid\ngraph LR\nA["file"] --> B\n```'

    def test_longer_closing_fence_is_preserved(self):
        diagram = sanitize_diagram('```mermaid\ngraph LR\nA --> B\n````\nTrailing prose')

        assert diagram == '\n```mermaid\ngraph LR\nA --> B\n````'

    @pytest.mark.parametrize(("label", "quoted_label"), [
        ("Parse (safe)", "Parse (safe)"),
        ("status: ready", "status: ready"),
        ('say "hello"', "say #quot;hello#quot;"),
    ])
    def test_unquoted_square_node_labels_are_quoted(self, label, quoted_label):
        diagram = sanitize_diagram(f"```mermaid\ngraph LR\nA[{label}]\n```")

        assert diagram == f'\n```mermaid\ngraph LR\nA["{quoted_label}"]\n```'

    @pytest.mark.parametrize(("body", "expected"), [
        ('A["Use items[index]"] --> B[Next]', 'A["Use items[index]"] --> B["Next"]'),
        ('A["array]"] --> B[Next]', 'A["array]"] --> B["Next"]'),
        ('A -- "array[index]" --> B[Next]', 'A -- "array[index]" --> B["Next"]'),
    ])
    def test_square_brackets_inside_existing_labels_are_preserved(self, body, expected):
        diagram = sanitize_diagram(f"```mermaid\ngraph LR\n{body}\n```")

        assert diagram == f"\n```mermaid\ngraph LR\n{expected}\n```"

    @pytest.mark.parametrize("node", [
        "A[(Database)]",
        "B[[Subprocess]]",
        "C[/Input/]",
    ])
    def test_non_rectangular_node_shapes_are_preserved(self, node):
        diagram = sanitize_diagram(f"```mermaid\ngraph LR\n{node}\n```")

        assert diagram == f"\n```mermaid\ngraph LR\n{node}\n```"

    def test_escaped_quotes_inside_a_quoted_label_become_entities(self):
        diagram = sanitize_diagram('```mermaid\ngraph LR\nA["say \\"hi\\""]\n```')

        assert diagram == '\n```mermaid\ngraph LR\nA["say #quot;hi#quot;"]\n```'


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


class TestPRDescriptionLargePR:

    def test_large_pr_prompt_sections_loaded(self):
        """Verify both prompt sections are registered, present in settings, and expose system and user."""
        settings = get_settings()
        assert "pr_description_only_files_prompts" in settings
        assert "pr_description_only_description_prompts" in settings

        files_prompts = settings.pr_description_only_files_prompts
        assert isinstance(files_prompts.system, str) and len(files_prompts.system) > 0
        assert isinstance(files_prompts.user, str) and len(files_prompts.user) > 0

        desc_prompts = settings.pr_description_only_description_prompts
        assert isinstance(desc_prompts.system, str) and len(desc_prompts.system) > 0
        assert isinstance(desc_prompts.user, str) and len(desc_prompts.user) > 0

    def test_large_pr_handling_gate_logic(self):
        """Verify the large-PR gate condition evaluates correctly with loaded settings and isolated configs."""
        real_settings = get_settings()
        is_active = (
            real_settings.pr_description.get("enable_large_pr_handling", True)
            and "pr_description_only_files_prompts" in real_settings
        )
        assert is_active is True

        # Test isolated settings dictionary without mutating global Dynaconf state
        class FakeSettings:
            def __init__(self, enable_large=True, has_section=True):
                self.pr_description = {"enable_large_pr_handling": enable_large}
                self._has_section = has_section

            def __contains__(self, item):
                return self._has_section if item == "pr_description_only_files_prompts" else False

        assert (
            FakeSettings(enable_large=True, has_section=True).pr_description.get("enable_large_pr_handling", True)
            and "pr_description_only_files_prompts" in FakeSettings(enable_large=True, has_section=True)
        )
        assert not (
            FakeSettings(enable_large=False, has_section=True).pr_description.get("enable_large_pr_handling", True)
            and "pr_description_only_files_prompts" in FakeSettings(enable_large=False, has_section=True)
        )
        assert not (
            FakeSettings(enable_large=True, has_section=False).pr_description.get("enable_large_pr_handling", True)
            and "pr_description_only_files_prompts" in FakeSettings(enable_large=True, has_section=False)
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("async_calls", [True, False])
    async def test_prepare_prediction_large_pr_multi_patch_flow(self, monkeypatch, async_calls):
        """Force _prepare_prediction() into the large-PR branch and verify chunk + header flow."""
        obj = _make_large_pr_instance()
        monkeypatch.setattr(get_settings().pr_description, "async_ai_calls", async_calls)

        recorded_prompts = []

        async def mock_get_prediction(model, patches_diff, prompt="pr_description_prompt"):
            recorded_prompts.append((prompt, patches_diff))
            if prompt == "pr_description_only_files_prompts":
                if "file1" in patches_diff:
                    return """```yaml
pr_files:
- filename: |
    src/file1.py
  changes_title: |
    Add feature 1
  changes_summary: |
    - Detail 1
  label: |
    enhancement
```"""
                else:
                    return """```yaml
pr_files:
- filename: |
    src/file2.py
  changes_title: |
    Fix bug in file 2
  changes_summary: |
    - Detail 2
  label: |
    bug fix
```"""
            elif prompt == "pr_description_only_description_prompts":
                return """```yaml
type:
- Enhancement
- Bug fix
title: |
  Combined PR Title
description: |
  - Point 1
  - Point 2
```"""
            raise ValueError(f"Unexpected prompt: {prompt}")

        obj._get_prediction = AsyncMock(side_effect=mock_get_prediction)

        chunks = [
            ["diff --git a/src/file1.py b/src/file1.py\n... file1 ..."],
            ["diff --git a/src/file2.py b/src/file2.py\n... file2 ..."],
        ]

        with patch("pr_agent.tools.pr_description.get_pr_diff", return_value="") as mock_diff, patch(
            "pr_agent.tools.pr_description.get_pr_diff_multiple_patchs",
            return_value=(chunks, [10, 10], [], [], {}, [["src/file1.py"], ["src/file2.py"]]),
        ) as mock_multi:
            await obj._prepare_prediction("gpt-4o")

            # Verify get_pr_diff was called with exact production arguments
            mock_diff.assert_called_once()
            _, diff_kwargs = mock_diff.call_args
            assert diff_kwargs.get("large_pr_handling") is True
            assert diff_kwargs.get("return_remaining_files") is True

            # Verify get_pr_diff_multiple_patchs was invoked
            mock_multi.assert_called_once()

        # Verify calls to prompts
        prompts_called = [p for p, _ in recorded_prompts]
        # Chunk predictions used files prompt
        assert prompts_called.count("pr_description_only_files_prompts") == 2
        # Final pass used description prompt
        assert prompts_called.count("pr_description_only_description_prompts") == 1
        # Negative assertion: standard single-prompt path was NOT called
        assert "pr_description_prompt" not in prompts_called

        # Verify final prediction YAML structure and parsing
        assert obj.prediction is not None
        assert "pr_files:" in obj.prediction
        parsed = load_yaml(obj.prediction, keys_fix_yaml=obj.keys_fix)
        assert isinstance(parsed, dict)
        assert parsed["title"].strip() == "Combined PR Title"
        assert parsed["type"] == ["Enhancement", "Bug fix"]
        assert len(parsed["pr_files"]) == 2

        file_names = [f["filename"].strip() for f in parsed["pr_files"]]
        assert file_names == ["src/file1.py", "src/file2.py"]

        # Downstream _prepare_data populates obj.data properly
        obj._prepare_data()
        assert obj.data["title"].strip() == "Combined PR Title"
        assert [f["filename"].strip() for f in obj.data["pr_files"]] == ["src/file1.py", "src/file2.py"]

    @pytest.mark.asyncio
    async def test_prepare_prediction_normal_diff_uses_single_prompt(self):
        """Verify normal diff under token limit does NOT fall into the large-PR multi-patch path."""
        obj = _make_large_pr_instance()

        recorded_prompts = []

        async def mock_get_prediction(model, patches_diff, prompt="pr_description_prompt"):
            recorded_prompts.append((prompt, patches_diff))
            return """```yaml
type:
- Enhancement
title: |
  Normal PR Title
pr_files:
- filename: |
    src/file1.py
  changes_title: |
    Normal change
  label: |
    enhancement
```"""

        obj._get_prediction = AsyncMock(side_effect=mock_get_prediction)

        with patch("pr_agent.tools.pr_description.get_pr_diff", return_value="normal diff content") as mock_diff, patch(
            "pr_agent.tools.pr_description.get_pr_diff_multiple_patchs"
        ) as mock_multi:
            await obj._prepare_prediction("gpt-4o")

            mock_diff.assert_called_once()
            mock_multi.assert_not_called()

        prompts_called = [p for p, _ in recorded_prompts]
        assert prompts_called == ["pr_description_prompt"]
        assert "pr_description_only_files_prompts" not in prompts_called
        assert "pr_description_only_description_prompts" not in prompts_called

    @pytest.mark.asyncio
    async def test_prepare_prediction_large_pr_with_diagram(self):
        """Verify changes_diagram in header prediction flows into final prediction and parsed data."""
        obj = _make_large_pr_instance([FilePatchInfo("", "", "", "src/file1.py")])
        obj.vars["enable_pr_diagram"] = True

        async def mock_get_prediction(model, patches_diff, prompt="pr_description_prompt"):
            if prompt == "pr_description_only_files_prompts":
                return """pr_files:
- filename: src/file1.py
  changes_title: Add diagram support
  label: enhancement"""
            elif prompt == "pr_description_only_description_prompts":
                return """type:
- Enhancement
title: Diagram PR
description: Adds mermaid diagram
changes_diagram: |
  ```mermaid
  flowchart LR
    A --> B
  ```"""
            raise ValueError(prompt)

        obj._get_prediction = AsyncMock(side_effect=mock_get_prediction)

        with patch("pr_agent.tools.pr_description.get_pr_diff", return_value=""), patch(
            "pr_agent.tools.pr_description.get_pr_diff_multiple_patchs",
            return_value=([["diff"]], [10], [], [], {}, [["src/file1.py"]]),
        ):
            await obj._prepare_prediction("gpt-4o")

        assert "changes_diagram:" in obj.prediction
        obj._prepare_data()
        assert "changes_diagram" in obj.data
        assert "```mermaid" in obj.data["changes_diagram"]
        assert [f["filename"].strip() for f in obj.data["pr_files"]] == ["src/file1.py"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("prompt_name", [
        "pr_description_only_files_prompts",
        "pr_description_only_description_prompts",
    ])
    async def test_prompt_templates_render_with_strict_undefined(self, prompt_name):
        """Render both new prompts through the real _get_prediction() code path.

        Only ai_handler.chat_completion is mocked; the Jinja rendering with
        StrictUndefined happens for real, so a misspelled variable name would
        raise UndefinedError and fail the test.
        """
        obj = _make_large_pr_instance()
        # Set up an ai_handler whose chat_completion captures rendered prompts
        rendered = {}

        async def capture_chat(*, model, temperature, system, user):
            rendered["system"] = system
            rendered["user"] = user
            return ("type:\n- Enhancement\ntitle: stub", "stop")

        obj.ai_handler = MagicMock()
        obj.ai_handler.chat_completion = AsyncMock(side_effect=capture_chat)

        # Call the real _get_prediction which renders templates via StrictUndefined
        await obj._get_prediction("gpt-4o", "sample diff content", prompt=prompt_name)

        # Both system and user must have been rendered without StrictUndefined errors
        assert "system" in rendered and len(rendered["system"]) > 0
        assert "user" in rendered and len(rendered["user"]) > 0

        # Structural checks per prompt type
        if prompt_name == "pr_description_only_files_prompts":
            # Files-only prompt must request pr_files / FileDescription
            assert "pr_files" in rendered["system"] or "FileDescription" in rendered["system"]
            # Must receive the diff
            assert "sample diff content" in rendered["user"]
            # Must NOT request overall PR title/type/description as output fields
            assert "PRDescriptionHeaders" not in rendered["system"]
        else:
            # Description-only prompt must request header fields
            assert "PRDescriptionHeaders" in rendered["system"]
            assert "PRType" in rendered["system"]
            # Must receive the walkthrough as diff
            assert "sample diff content" in rendered["user"]
            # Must NOT request pr_files output
            assert "FileDescription" not in rendered["system"]
            assert "pr_files" not in rendered["system"].split("PRDescriptionHeaders")[1]

    def test_prompt_jinja_variables_match_production_vars(self):
        """Cross-check every Jinja variable referenced in both new prompts
        against the production self.vars keys from _make_large_pr_instance."""
        env = Environment(undefined=StrictUndefined, autoescape=True)
        settings = get_settings()
        production_vars = _make_large_pr_instance().vars
        # _get_prediction adds 'diff' from patches_diff, so ensure it's present
        production_vars["diff"] = "placeholder diff"

        for prompt_name in [
            "pr_description_only_files_prompts",
            "pr_description_only_description_prompts",
        ]:
            system_tpl = settings.get(prompt_name, {}).get("system", "")
            user_tpl = settings.get(prompt_name, {}).get("user", "")

            # These must not raise UndefinedError
            rendered_sys = env.from_string(system_tpl).render(production_vars)
            rendered_usr = env.from_string(user_tpl).render(production_vars)

            assert len(rendered_sys) > 0, f"{prompt_name} system rendered empty"
            assert len(rendered_usr) > 0, f"{prompt_name} user rendered empty"
