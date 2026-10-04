"""Render todo_sections in every shape the prompt schema permits."""
import pytest

from pr_agent.algo.utils import convert_to_markdown_v2


class FakeGitProvider:
    def get_line_link(self, relevant_file, start, end=None):
        return "http://example.com/#L1"


class LineAwareGitProvider:
    def get_line_link(self, relevant_file, start, end=None):
        if start == -1:
            return f"http://example.com/{relevant_file}"
        return f"http://example.com/{relevant_file}#L{start}"


BASE = {"estimated_effort_to_review_[1-5]": "2"}
DOCUMENTED = [{"relevant_file": "src/app.py", "line_number": 3, "content": "fix the parser"}]


def render(todo_sections, gfm_supported=True, git_provider=None):
    data = {"review": dict(BASE, todo_sections=todo_sections)}
    return convert_to_markdown_v2(data, gfm_supported=gfm_supported,
                                  git_provider=git_provider or FakeGitProvider())


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


@pytest.mark.parametrize("line_number", [None, "", "unknown", 0, -1, True, float("inf")])
def test_link_an_entry_without_a_usable_line_number_to_its_file(line_number):
    entry = {"relevant_file": "src/app.py", "line_number": line_number, "content": "fix the parser"}

    out = render([entry], git_provider=LineAwareGitProvider())

    assert "<li><a href='http://example.com/src/app.py'>src/app.py</a>: fix the parser</li>" in out


def test_link_an_entry_that_omits_the_line_number_to_its_file():
    entry = {"relevant_file": "src/app.py", "content": "fix the parser"}

    out = render([entry], gfm_supported=False, git_provider=LineAwareGitProvider())

    assert "- [src/app.py](http://example.com/src/app.py): fix the parser" in out


def test_keep_a_line_number_the_model_wrote_as_a_string():
    entry = {"relevant_file": "src/app.py", "line_number": "12", "content": "fix the parser"}

    out = render([entry], git_provider=LineAwareGitProvider())

    assert "<li><a href='http://example.com/src/app.py#L12'>src/app.py [12]</a>: fix the parser</li>" in out
