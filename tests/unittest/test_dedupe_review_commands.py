import pytest

from pr_agent.servers.utils import dedupe_review_commands


def test_dedupe_keeps_one_review_and_merges_args():
    commands = [
        "/describe --pr_description.final_update_message=false",
        "/review --pr_reviewer.num_max_findings=2",
        "/improve --pr_code_suggestions.committable_code_suggestions=true --pr_reviewer.num_max_findings=2",
        "/improve_code -i",
        "/ask what changed",
    ]

    assert dedupe_review_commands(commands) == [
        "/describe --pr_description.final_update_message=false",
        "/review --pr_reviewer.num_max_findings=2 --pr_code_suggestions.committable_code_suggestions=true -i",
        "/ask what changed",
    ]


@pytest.mark.parametrize(
    ("commands", "expected"),
    [
        (["/improve"], ["/review"]),
        (["improve_code --x=1"], ["review --x=1"]),
        (["/IMPROVE"], ["/review"]),
        (["  /improve   --x=1   --y=2  "], ["/review --x=1 --y=2"]),
        (["/improve", "/review"], ["/review"]),
        (["/review", "improve"], ["/review"]),
        (["/describe", "/ask q", "/custom"], ["/describe", "/ask q", "/custom"]),
        ([], []),
        (["/improve_extra"], ["/improve_extra"]),
        (["/review", 7, None, ""], ["/review", 7, None, ""]),
    ],
)
def test_dedupe_review_commands_shapes(commands, expected):
    assert dedupe_review_commands(commands) == expected


def test_dedupe_keeps_quoted_arguments_whole():
    instruction = '--pr_reviewer.extra_instructions="check A and B"'

    assert dedupe_review_commands([f"/review {instruction}", f"/improve {instruction} -i"]) == [
        f"/review {instruction} -i"
    ]


def test_dedupe_does_not_mutate_its_input():
    commands = ["/review", "/improve --x=1"]

    dedupe_review_commands(commands)

    assert commands == ["/review", "/improve --x=1"]


def test_dedupe_keeps_an_unterminated_quote_apart():
    broken = '/review --pr_reviewer.extra_instructions="unfinished'

    assert dedupe_review_commands([broken, "/improve"]) == [broken, "/review"]


def test_dedupe_treats_review_pr_as_review():
    assert dedupe_review_commands(["/review_pr", "/improve"]) == ["/review"]


def test_dedupe_accepts_a_double_quote_inside_single_quotes():
    review = "/review --pr_reviewer.extra_instructions='Check a literal \" character'"

    assert dedupe_review_commands([review, "/improve"]) == [review]


def test_dedupe_keeps_an_unterminated_single_quote_apart():
    broken = "/review --pr_reviewer.extra_instructions='unfinished"

    assert dedupe_review_commands([broken, "/improve"]) == [broken, "/review"]


@pytest.mark.parametrize("override", [None, False, [], ""])
def test_a_falsy_pr_commands_override_still_disables_the_automatic_commands(monkeypatch, override):
    from pr_agent.config_loader import get_settings
    from pr_agent.servers.utils import get_pr_commands

    monkeypatch.setitem(get_settings().github_app, "pr_commands", override)

    assert not get_pr_commands("github_app")
