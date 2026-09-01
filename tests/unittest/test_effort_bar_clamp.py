"""Keep the effort bar on its 1-5 scale whatever the model returns."""
import pytest

from pr_agent.algo.utils import convert_to_markdown_v2


def _bars(value):
    out = convert_to_markdown_v2({"review": {"estimated_effort_to_review_[1-5]": value}},
                                 gfm_supported=True)
    return out.count("\U0001f535"), out.count("\u26aa")


@pytest.mark.parametrize("value, expected", [("1", (1, 4)), ("3", (3, 2)), ("5", (5, 0))])
def test_render_in_range_scores_unchanged(value, expected):
    """Keep the existing rendering for scores inside the scale."""
    assert _bars(value) == expected


def test_clamp_a_score_above_the_scale():
    """Cap a score above 5 so the bar is no longer than the scale."""
    blue, white = _bars("8")

    assert blue == 5
    assert white == 0


def test_clamp_a_score_below_the_scale():
    """Raise a score below 1 so the bar has no negative-length segment."""
    blue, white = _bars("0")

    assert blue == 1
    assert white == 4


def test_the_bar_is_always_five_segments():
    """Keep the total segment count constant so the bar reads as a 1-5 scale."""
    for value in ("0", "1", "3", "5", "8", "99"):
        blue, white = _bars(value)
        assert blue + white == 5, value


@pytest.mark.parametrize("score, expected", [("8", 5), ("0", 1), ("3", 3)])
def test_the_review_label_matches_the_rendered_bar(score, expected):
    """Clamp the label the same way as the bar, so the two never disagree."""
    from pr_agent.config_loader import get_settings
    from pr_agent.tools.pr_reviewer import PRReviewer

    reviewer = PRReviewer.__new__(PRReviewer)
    published = []
    reviewer.git_provider = type("P", (), {
        "publish_labels": lambda _self, labels: published.append(labels) or True,
        "get_pr_labels": lambda _self, update=False: [],
        "is_supported": lambda _self, feature: feature == "get_labels",
    })()

    settings = get_settings(use_context=False)
    settings.set("pr_reviewer.enable_review_labels_effort", True)
    settings.set("pr_reviewer.enable_review_labels_security", False)
    settings.set("pr_reviewer.require_estimate_effort_to_review", True)
    settings.set("pr_reviewer.require_security_review", False)
    settings.set("config.publish_output", True)
    reviewer.set_review_labels({"review": {"estimated_effort_to_review_[1-5]": score}})

    assert published and published[0] == [f"Review effort {expected}/5"]
