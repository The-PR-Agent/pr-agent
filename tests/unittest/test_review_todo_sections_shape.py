"""Render todo_sections in every shape the prompt schema permits."""
import pytest

from pr_agent.algo.utils import convert_to_markdown_v2


class FakeGitProvider:
    def get_line_link(self, relevant_file, start, end=None):
        return "http://example.com/#L1"


BASE = {"estimated_effort_to_review_[1-5]": "2"}
DOCUMENTED = [{"relevant_file": "src/app.py", "line_number": 3, "content": "fix the parser"}]


def render(todo_sections, gfm_supported=True):
    data = {"review": dict(BASE, todo_sections=todo_sections)}
    return convert_to_markdown_v2(data, gfm_supported=gfm_supported,
                                  git_provider=FakeGitProvider())


@pytest.mark.parametrize("gfm_supported", [True, False])
def test_render_a_free_text_summary(gfm_supported):
    """Accept a plain string, which the schema declares as Union[List[TodoSection], str]."""
    out = render("Found 2 TODO comments in src/app.py", gfm_supported)

    assert "Found 2 TODO comments in src/app.py" in out


def test_render_a_list_of_plain_strings():
    """Accept a list of summaries, which is the other shape a model reaches for."""
    out = render(["fix the parser", "handle nulls"])

    assert "fix the parser" in out
    assert "handle nulls" in out


def test_render_the_documented_shape_unchanged():
    """Keep the documented list-of-objects rendering exactly as before."""
    out = render(DOCUMENTED)

    assert "src/app.py" in out
    assert "fix the parser" in out


def test_a_no_answer_still_reports_no_todo_sections():
    """Keep the 'No TODO sections' wording for the documented 'No' answer."""
    assert "No TODO sections" in render("No")


def test_skip_an_entry_that_carries_no_usable_text():
    """Drop an unusable entry rather than the whole review."""
    out = render([None, {"relevant_file": "src/app.py", "line_number": 3, "content": "fix"}])

    assert "fix" in out
