import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.algo import structural_reach as sr
from pr_agent.algo.artifacts import resolve_artifact_path
from pr_agent.algo.utils import convert_to_markdown_v2
from pr_agent.config_loader import get_settings
from pr_agent.config_security import CLI_HOST_ONLY_KEYS_BY_SECTION
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_KEYS = ("pr_reviewer.structural_reach_input_file",)

STRUCTURE = {"reach": 40, "cross_domain": True, "sensitive_domains": []}


@pytest.fixture
def reach_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    snapshot = snapshot_settings(_KEYS)
    settings = get_settings()
    settings.set("pr_reviewer.structural_reach_input_file", "")
    yield settings
    restore_settings(snapshot)


def _file(filename, patch):
    return SimpleNamespace(filename=filename, patch=patch)


def _write_input(tmp_path, payload):
    path = tmp_path / "structural-reach.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path.name


def test_reach_band():
    assert sr.reach_band(None) is None
    assert sr.reach_band(2) == "narrow"
    assert sr.reach_band(10) == "moderate"
    assert sr.reach_band(50) == "wide"


@pytest.mark.parametrize(("reach", "expected"), [
    (0, "narrow"), (3, "narrow"), (4, "moderate"), (25, "moderate"), (26, "wide"),
])
def test_reach_band_boundaries(reach, expected):
    assert sr.reach_band(reach) == expected


@pytest.mark.parametrize(("structure", "expected"), [
    ({"reach": 2}, "Low"),
    ({"reach": 25}, "Low"),
    ({"reach": 26}, "Moderate"),
    ({"reach": 2, "cross_domain": True}, "Moderate"),
    ({"reach": 50, "cross_domain": True}, "Moderate"),
    ({"reach": 2, "sensitive_domains": ["auth"]}, "High"),
    ({"reach": 25, "sensitive_domains": ["auth"]}, "High"),
    ({"sensitive_domains": ["auth"]}, "High"),
    ({"reach": 26, "sensitive_domains": ["auth"]}, "Critical"),
    ({"domains": ["search"]}, "Low"),
])
def test_compose_risk(structure, expected):
    assert sr.BANDS[sr.compose_risk(structure)] == expected


@pytest.mark.parametrize(("structure", "expected"), [
    ({"reach": 50, "sensitive_domains": ["auth"]}, "sensitive domain (escalates regardless of size)"),
    ({"reach": 50, "cross_domain": True}, "wide reach"),
    ({"reach": 2, "cross_domain": True}, "cross-domain change"),
    ({"reach": 2}, "contained reach"),
    ({}, "contained reach"),
])
def test_driver_names_the_strongest_factor(structure, expected):
    assert sr.driver(structure) == expected


def test_assess_with_structure():
    assert sr.assess_change(STRUCTURE) == {
        "band": "Moderate",
        "level": 1,
        "factors": [{"label": "Reach", "value": "40 projects, cross-domain"}],
        "driver": "wide reach",
    }


def test_assess_without_structure_is_none():
    assert sr.assess_change(None) is None
    assert sr.assess_change({}) is None


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "reach",
        {"version": 2, "reach": 3},
        {"version": True, "reach": 3},
        {"reach": "3"},
        {"reach": -1},
        {"reach": True},
        {"reach": 1.5},
        {"cross_domain": "yes"},
        {"domains": "search"},
        {"domains": [1, 2]},
        {"sensitive_domains": [""]},
        {"sensitive_domains": ["x" * (sr.MAX_NAME_CHARS + 1)]},
        {},
        {"unrelated": 1},
    ],
)
def test_parse_structure_rejects_malformed_input(raw):
    assert sr.parse_structure(raw) is None


def test_parse_structure_accepts_zero_reach():
    assert sr.parse_structure({"reach": 0}) == {"reach": 0}


def test_parse_structure_accepts_name_of_exactly_the_limit():
    name = "x" * sr.MAX_NAME_CHARS
    assert sr.parse_structure({"domains": [name]}) == {"domains": [name]}


def test_parse_structure_keeps_known_fields_and_drops_unknown():
    parsed = sr.parse_structure({
        "version": 1,
        "reach": 4,
        "dependents": ["a", "b"],
        "domains": ["search", "checkout"],
        "cross_domain": True,
        "sensitive_domains": ["auth"],
        "tool": "anything",
    })
    assert parsed == {
        "reach": 4,
        "dependents": ["a", "b"],
        "domains": ["search", "checkout"],
        "cross_domain": True,
        "sensitive_domains": ["auth"],
    }


def test_parse_structure_derives_reach_from_dependents():
    assert sr.parse_structure({"dependents": ["a", "b", "c"]})["reach"] == 3


def test_load_structure_reads_file_under_workspace(reach_settings, tmp_path):
    name = _write_input(tmp_path, {"reach": 5, "domains": ["search"]})
    assert sr.load_structure(name, resolve_artifact_path) == {"reach": 5, "domains": ["search"]}


@pytest.mark.parametrize("payload", ["{not json", json.dumps({"reach": "five"}), "\udcff"])
def test_load_structure_fails_open_on_bad_file(reach_settings, tmp_path, payload):
    path = tmp_path / "structural-reach.json"
    path.write_bytes(payload.encode("utf-8", "surrogateescape"))
    assert sr.load_structure(path.name, resolve_artifact_path) is None


def test_load_structure_ignores_non_string_path(reach_settings, tmp_path):
    name = _write_input(tmp_path, {"reach": 5})
    assert sr.load_structure(tmp_path / name, resolve_artifact_path) is None
    assert sr.load_structure(None, resolve_artifact_path) is None


def test_load_structure_rejects_missing_and_outside_files(reach_settings, tmp_path):
    outside = tmp_path.parent / "outside-structural-reach.json"
    outside.write_text(json.dumps({"reach": 5}), encoding="utf-8")
    try:
        assert sr.load_structure("missing.json", resolve_artifact_path) is None
        assert sr.load_structure(str(outside), resolve_artifact_path) is None
        assert sr.load_structure("", resolve_artifact_path) is None
    finally:
        outside.unlink()


def test_load_structure_rejects_oversized_file(reach_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "MAX_INPUT_BYTES", 10)
    name = _write_input(tmp_path, {"reach": 5, "domains": ["a-long-domain-name"]})
    assert sr.load_structure(name, resolve_artifact_path) is None


@pytest.mark.parametrize(("size_offset", "expected"), [(0, {"reach": 5}), (-1, None)])
def test_load_structure_size_limit_is_inclusive(reach_settings, tmp_path, monkeypatch, size_offset, expected):
    name = _write_input(tmp_path, {"reach": 5})
    monkeypatch.setattr(sr, "MAX_INPUT_BYTES", (tmp_path / name).stat().st_size + size_offset)
    assert sr.load_structure(name, resolve_artifact_path) == expected


def test_build_reads_input_file(reach_settings, tmp_path):
    reach_settings.set("pr_reviewer.structural_reach_input_file", _write_input(tmp_path, STRUCTURE))
    row = sr.build_structural_reach(resolve_artifact_path)
    assert row["band"] == "Moderate"


def test_build_has_no_row_without_input_file(reach_settings, tmp_path):
    assert sr.build_structural_reach(resolve_artifact_path) is None
    reach_settings.set("pr_reviewer.structural_reach_input_file", "missing.json")
    assert sr.build_structural_reach(resolve_artifact_path) is None
    reach_settings.set("pr_reviewer.structural_reach_input_file", _write_input(tmp_path, "{broken"))
    assert sr.build_structural_reach(resolve_artifact_path) is None


def test_build_has_no_row_when_setting_is_missing(monkeypatch):
    monkeypatch.setattr(sr, "get_settings", lambda: SimpleNamespace(pr_reviewer={}))
    assert sr.build_structural_reach(resolve_artifact_path) is None


def test_build_fails_open_on_any_exception(reach_settings, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(sr, "assess_change", boom)
    assert sr.build_structural_reach(resolve_artifact_path) is None


def test_render_gfm_row():
    row = sr.assess_change(STRUCTURE)
    rendered = sr.render_structural_reach(row, gfm_supported=True)
    assert rendered == (
        "<tr><td>🔗&nbsp;<strong>Structural reach</strong>: <strong>Moderate</strong>"
        "<br>Reach: 40 projects, cross-domain"
        "<br><em>Rating driven by wide reach</em></td></tr>\n"
    )


MARKDOWN_ROW = {
    "band": "High",
    "driver": "a_b*c",
    "factors": [{"label": "L[1]", "value": "`v`|x <b>"}],
}


def test_render_markdown_escapes_special_characters():
    assert sr.render_structural_reach(MARKDOWN_ROW, gfm_supported=False) == (
        "### 🔗 Structural reach: **High**\n\n"
        "- L\\[1\\]: \\`v\\`\\|x &lt;b&gt;\n"
        "- Rating driven by a\\_b\\*c\n\n"
    )


def test_render_gfm_keeps_special_characters_unescaped():
    assert sr.render_structural_reach(MARKDOWN_ROW, gfm_supported=True) == (
        "<tr><td>🔗&nbsp;<strong>Structural reach</strong>: <strong>High</strong>"
        "<br>L[1]: `v`|x &lt;b&gt;<br><em>Rating driven by a_b*c</em></td></tr>\n"
    )


def test_render_escapes_input_values():
    row = sr.assess_change({"reach": 1, "sensitive_domains": ["<img src=x>"]})
    rendered = sr.render_structural_reach(row, gfm_supported=True)
    assert "<img" not in rendered
    assert "&lt;img src=x&gt;" in rendered


@pytest.mark.parametrize("factors", [
    ["Reach"],
    [{"label": "Reach"}],
    [{"value": "5"}],
    [{"label": "Reach", "value": 5}],
    [{"label": 1, "value": "5"}],
    [{"label": "Reach", "value": "5"}, {"label": "Sensitivity", "value": None}],
])
def test_render_skips_row_with_malformed_factor(factors):
    row = {"band": "Low", "factors": factors, "driver": "x"}
    assert sr.render_structural_reach(row, gfm_supported=True) == ""
    assert sr.render_structural_reach(row, gfm_supported=False) == ""


def test_render_skips_row_with_non_string_driver():
    assert sr.render_structural_reach({"band": "Low", "factors": [], "driver": 1}, gfm_supported=True) == ""


@pytest.mark.parametrize("row", ["Critical", {}, {"band": "Severe", "factors": [], "driver": "x"},
                                 {"band": "Low", "factors": [{"label": 1}], "driver": "x"}])
def test_render_skips_malformed_row(row):
    assert sr.render_structural_reach(row, gfm_supported=True) == ""
    assert sr.render_structural_reach(row, gfm_supported=False) == ""


def test_convert_to_markdown_renders_row_first():
    row = sr.assess_change(STRUCTURE)
    review = {"review": {"structural_reach": row, "estimated_effort_to_review_[1-5]": "2"}}
    markdown = convert_to_markdown_v2(review, gfm_supported=True)
    assert markdown.index("Structural reach") < markdown.index("Estimated effort to review")


def _prepare(reviewer, review_yaml, diff_files):
    reviewer.prediction = review_yaml
    reviewer.git_provider.get_diff_files.return_value = diff_files
    reviewer.git_provider.is_supported.return_value = True
    with patch("pr_agent.tools.pr_reviewer.github_action_output"):
        markdown = reviewer._prepare_pr_review()
    return markdown


def _reviewer():
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer.git_provider.publish_structured_review = None
    reviewer.pr_url = "https://example/pr/1"
    reviewer.token_handler = MagicMock()
    reviewer.remaining_files_list = []
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.vars = {}
    return reviewer


def test_prepare_pr_review_adds_row_when_input_file_exists(reach_settings, tmp_path):
    reach_settings.set("pr_reviewer.structural_reach_input_file", _write_input(tmp_path, STRUCTURE))
    markdown = _prepare(_reviewer(), "review:\n  relevant_tests: |\n    No\n", [_file("a.ts", "+a")])
    assert "🔗&nbsp;<strong>Structural reach</strong>: <strong>Moderate</strong>" in markdown


def test_prepare_pr_review_has_no_row_without_input_file(reach_settings):
    markdown = _prepare(_reviewer(), "review:\n  relevant_tests: |\n    No\n", [_file("a.ts", "+a")])
    assert "Structural reach" not in markdown


def test_prepare_pr_review_drops_model_supplied_row(reach_settings):
    review_yaml = "review:\n  relevant_tests: |\n    No\n  structural_reach: Critical\n"
    markdown = _prepare(_reviewer(), review_yaml, [_file("a.ts", "+a")])
    assert "Structural reach" not in markdown


@pytest.mark.parametrize("key", [
    "structural_reach", "Structural_Reach", "STRUCTURAL_REACH",
    "structural reach", "Structural-Reach", "structural__reach", "structural.reach", "'structural_reach '",
])
def test_prepare_pr_review_drops_a_well_formed_model_row_in_any_case(reach_settings, key):
    review_yaml = (
        "review:\n  relevant_tests: |\n    No\n"
        f"  {key}:\n    band: Critical\n    level: 3\n"
        "    factors:\n    - label: Reach\n      value: 999 projects\n    driver: model\n"
    )
    markdown = _prepare(_reviewer(), review_yaml, [_file("a.ts", "+a")])
    assert "Structural reach" not in markdown
    assert "999 projects" not in markdown


def test_input_file_is_host_only_for_comment_arguments():
    assert "structural_reach_input_file" in CLI_HOST_ONLY_KEYS_BY_SECTION["pr_reviewer"]


@pytest.mark.parametrize(
    "gfm_supported, expected",
    [
        (True, "Sensitivity: auth  ![x](https://example.com/x.png)"),
        (False, "- Sensitivity: auth  \\!\\[x\\]\\(https://example.com/x.png\\)"),
    ],
)
def test_render_keeps_multiline_values_on_one_line(gfm_supported, expected):
    row = sr.assess_change({"reach": 1, "sensitive_domains": ["auth\n\n![x](https://example.com/x.png)"]})
    rendered = sr.render_structural_reach(row, gfm_supported=gfm_supported)
    sensitivity = next(line for line in rendered.split("<br>" if gfm_supported else "\n") if "Sensitivity" in line)
    assert sensitivity == expected
    assert "\n\n" not in rendered.split("Sensitivity", 1)[1].split("Rating driven", 1)[0]


@pytest.mark.parametrize("unit", ["modules", "files"])
def test_module_and_file_reach_have_no_band(unit):
    row = sr.assess_change({"reach": 300, "reach_unit": unit, "dependents": ["a", "b"]})
    assert row == {"factors": [
        {"label": "Reach", "value": f"300 {unit}"},
        {"label": "Dependents", "value": "a, b (+298 more)"},
    ]}


def test_module_reach_keeps_band_when_sensitive_or_cross_domain():
    sensitive = sr.assess_change({"reach": 300, "reach_unit": "modules", "sensitive_domains": ["auth"]})
    assert (sensitive["band"], sensitive["driver"]) == ("High", "sensitive domain (escalates regardless of size)")
    cross = sr.assess_change({"reach": 300, "reach_unit": "modules", "cross_domain": True})
    assert (cross["band"], cross["driver"]) == ("Moderate", "cross-domain change")


def test_project_is_the_default_reach_unit():
    assert sr.assess_change({"reach": 26})["band"] == "Moderate"
    assert sr.assess_change({"reach": 26, "reach_unit": "projects"})["band"] == "Moderate"
    assert sr.assess_change({"reach": 1, "reach_unit": "files"})["factors"] == [{"label": "Reach", "value": "1 file"}]


def test_row_without_factors_is_none():
    assert sr.assess_change({"reach_unit": "files"}) is None
    assert sr.assess_change({"reach": 0, "reach_unit": "modules"}) is None
    assert sr.assess_change({"domains": ["search"]}) is None


@pytest.mark.parametrize(("structure", "expected"), [
    ({"dependents": ["a", "b", "c"]}, "a, b, c"),
    ({"dependents": list("abcdef")}, "a, b, c, d, e (+1 more)"),
    ({"reach": 161, "dependents": list("abcdefg")}, "a, b, c, d, e (+156 more)"),
    ({"reach": 2, "dependents": list("abc")}, "a, b, c"),
])
def test_dependents_lists_the_first_five(structure, expected):
    dependents = next(f for f in sr.factors(structure) if f["label"] == "Dependents")
    assert dependents["value"] == expected


@pytest.mark.parametrize(("sensitive", "expected"), [
    (["auth", "payments"], "auth, payments"),
    (list("abcdefgh"), "a, b, c, d, e (+3 more)"),
])
def test_sensitivity_lists_the_first_five(sensitive, expected):
    sensitivity = next(f for f in sr.factors({"sensitive_domains": sensitive}) if f["label"] == "Sensitivity")
    assert sensitivity["value"] == expected


@pytest.mark.parametrize("unit", ["project", "packages", "", 3, None])
def test_parse_structure_rejects_unknown_reach_unit(unit):
    assert sr.parse_structure({"reach": 3, "reach_unit": unit}) is None


def test_parse_structure_keeps_reach_unit():
    assert sr.parse_structure({"reach": 3, "reach_unit": "modules"}) == {"reach": 3, "reach_unit": "modules"}


@pytest.mark.parametrize(("gfm_supported", "expected"), [
    (True, "<tr><td>🔗&nbsp;<strong>Structural reach</strong><br>Reach: 12 modules</td></tr>\n"),
    (False, "### 🔗 Structural reach\n\n- Reach: 12 modules\n\n"),
])
def test_render_unbanded_row(gfm_supported, expected):
    row = sr.assess_change({"reach": 12, "reach_unit": "modules"})
    assert sr.render_structural_reach(row, gfm_supported=gfm_supported) == expected


@pytest.mark.parametrize("row", [
    {"factors": [], "driver": "x"},
    {"band": None, "factors": [], "driver": "x"},
    {"band": "Low", "factors": []},
])
def test_render_skips_row_with_band_and_driver_mismatch(row):
    assert sr.render_structural_reach(row, gfm_supported=True) == ""
