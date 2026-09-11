"""Render a security_concerns value the model returned as a list instead of a string."""
import pytest

from pr_agent.algo.utils import convert_to_markdown_v2


class FakeGitProvider:
    def get_line_link(self, relevant_file, start, end=None):
        return "http://example.com/#L1"


BASE = {"estimated_effort_to_review_[1-5]": "2"}


def render(security_concerns, gfm_supported=True):
    data = {"review": dict(BASE, security_concerns=security_concerns)}
    return convert_to_markdown_v2(data, gfm_supported=gfm_supported,
                                  git_provider=FakeGitProvider())


@pytest.mark.parametrize("gfm_supported", [True, False])
def test_render_a_list_of_concerns_without_raising(gfm_supported):
    """Keep the review alive when the model lists concerns instead of writing one string."""
    out = render(["SQL injection: user input reaches the query",
                  "XSS: the title is rendered unescaped"], gfm_supported)

    assert "SQL injection" in out
    assert "XSS" in out


def test_render_a_single_concern_string_unchanged():
    """Keep the documented string form rendering exactly as before."""
    out = render("SQL injection: user input reaches the query")

    assert "SQL injection" in out


@pytest.mark.parametrize("value", ["No", "no", "none", "false"])
def test_a_no_answer_still_reports_no_concerns(value):
    """Keep the 'no concerns' wording for every value is_value_no accepts."""
    assert "No security concerns identified" in render(value)


@pytest.mark.parametrize("value", ["", None])
def test_an_empty_answer_omits_the_section(value):
    """Keep the existing behaviour where an empty field renders nothing."""
    assert "security concerns" not in render(value).lower()


def test_a_mapping_of_concerns_is_rendered():
    """Render a mapping the model may return instead of dropping the whole review."""
    out = render({"sql_injection": "user input reaches the query"})

    assert "user input reaches the query" in out
