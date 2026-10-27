import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from pr_agent.algo import blast_radius as br
from pr_agent.algo.artifacts import resolve_artifact_path
from pr_agent.algo.types import FilePatchInfo
from pr_agent.algo.utils import convert_to_markdown_v2
from pr_agent.config_loader import get_settings
from pr_agent.config_security import CLI_HOST_ONLY_KEYS_BY_SECTION
from pr_agent.tools.pr_reviewer import PRReviewer
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_KEYS = (
    "pr_reviewer.enable_blast_radius",
    "pr_reviewer.blast_radius_input_file",
    "pr_reviewer.blast_radius_path_heuristic",
    "pr_reviewer.blast_radius_sensitive_terms",
    "pr_reviewer.blast_radius_churn_ignore_pattern",
)

STRUCTURE = {"reach": 69, "cross_domain": True, "sensitive_domains": []}
SMALL_CHURN = {"files": 1, "added": 1, "deleted": 0}
LARGE_CHURN = {"files": 40, "added": 500, "deleted": 0}


@pytest.fixture
def blast_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("GITHUB_WORKSPACE", str(tmp_path))
    snapshot = snapshot_settings(_KEYS)
    settings = get_settings()
    settings.set("pr_reviewer.enable_blast_radius", True)
    settings.set("pr_reviewer.blast_radius_input_file", "")
    settings.set("pr_reviewer.blast_radius_path_heuristic", False)
    settings.set("pr_reviewer.blast_radius_sensitive_terms", [])
    settings.set("pr_reviewer.blast_radius_churn_ignore_pattern", "")
    yield settings
    restore_settings(snapshot)


def _file(filename, patch):
    return SimpleNamespace(filename=filename, patch=patch)


def _multi_file_diff():
    return [_file("libs/auth/login.ts", "+a\n+b\n-c"), _file("services/player/x.ts", "+d")]


def _write_input(tmp_path, payload):
    path = tmp_path / "blast-radius.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path.name


def test_reach_band():
    assert br.reach_band(None) is None
    assert br.reach_band(2) == "narrow"
    assert br.reach_band(10) == "moderate"
    assert br.reach_band(50) == "wide"


def test_churn_band():
    assert br.churn_band(None) is None
    assert br.churn_band({"files": 1, "added": 1, "deleted": 0}) == "small"
    assert br.churn_band({"files": 3, "added": 60, "deleted": 0}) == "moderate"
    assert br.churn_band({"files": 7, "added": 1, "deleted": 0}) == "moderate"
    assert br.churn_band({"files": 40, "added": 10, "deleted": 0}) == "large"
    assert br.churn_band({"files": 2, "added": 300, "deleted": 200}) == "large"


@pytest.mark.parametrize(("reach", "expected"), [
    (0, "narrow"), (3, "narrow"), (4, "moderate"), (25, "moderate"), (26, "wide"),
])
def test_reach_band_boundaries(reach, expected):
    assert br.reach_band(reach) == expected


@pytest.mark.parametrize(("files", "added", "deleted", "expected"), [
    (1, 49, 0, "small"),
    (1, 25, 24, "small"),
    (1, 50, 0, "moderate"),
    (1, 25, 25, "moderate"),
    (5, 1, 0, "small"),
    (6, 1, 0, "moderate"),
    (1, 400, 0, "moderate"),
    (1, 200, 200, "moderate"),
    (1, 401, 0, "large"),
    (1, 201, 200, "large"),
    (30, 1, 0, "moderate"),
    (31, 1, 0, "large"),
])
def test_churn_band_boundaries(files, added, deleted, expected):
    assert br.churn_band({"files": files, "added": added, "deleted": deleted}) == expected


def test_compose_low():
    risk = br.compose_risk(
        {"reach": 2, "cross_domain": False, "sensitive_domains": []},
        {"files": 1, "added": 2, "deleted": 0},
    )
    assert risk["band"] == "Low"


def test_compose_moderate_on_wide_reach():
    risk = br.compose_risk(
        {"reach": 50, "cross_domain": False, "sensitive_domains": []},
        {"files": 1, "added": 1, "deleted": 0},
    )
    assert risk["band"] == "Moderate"


def test_compose_high_on_wide_and_large():
    risk = br.compose_risk(
        {"reach": 50, "cross_domain": True, "sensitive_domains": []},
        {"files": 40, "added": 500, "deleted": 50},
    )
    assert risk["band"] == "High"


def test_compose_sensitive_escalates_regardless_of_size():
    risk = br.compose_risk(
        {"reach": 2, "cross_domain": False, "sensitive_domains": ["auth"]},
        {"files": 1, "added": 1, "deleted": 0},
    )
    assert risk["band"] == "High"
    assert any("auth" in reason for reason in risk["reasons"])


def test_compose_sensitive_plus_wide_is_critical():
    risk = br.compose_risk(
        {"reach": 50, "cross_domain": True, "sensitive_domains": ["payments"]},
        {"files": 40, "added": 900, "deleted": 0},
    )
    assert risk["band"] == "Critical"


def test_compose_reach_only_when_no_churn():
    risk = br.compose_risk({"reach": 50, "cross_domain": False, "sensitive_domains": []}, None)
    assert risk["band"] == "Moderate"
    assert any("reach" in reason for reason in risk["reasons"])
    assert not any("churn" in reason for reason in risk["reasons"])


def test_compose_cross_domain_alone_is_moderate():
    risk = br.compose_risk(
        {"reach": 2, "cross_domain": True, "sensitive_domains": []},
        {"files": 1, "added": 2, "deleted": 0},
    )
    assert risk["band"] == "Moderate"
    assert risk["level"] == 1
    assert risk["reasons"] == ["narrow reach (2 projects)", "small churn (1 file, +2/-0)", "cross-domain change"]


@pytest.mark.parametrize(("reach", "churn", "expected"), [
    (2, {"files": 1, "added": 1, "deleted": 0}, "High"),
    (4, {"files": 6, "added": 60, "deleted": 0}, "High"),
    (50, {"files": 1, "added": 1, "deleted": 0}, "Critical"),
    (2, {"files": 31, "added": 1, "deleted": 0}, "Critical"),
    (2, {"files": 1, "added": 401, "deleted": 0}, "Critical"),
    (26, None, "Critical"),
    (2, None, "High"),
])
def test_compose_sensitive_escalation_by_reach_or_churn(reach, churn, expected):
    risk = br.compose_risk({"reach": reach, "cross_domain": False, "sensitive_domains": ["auth"]}, churn)
    assert risk["band"] == expected
    assert risk["level"] == br.BANDS.index(expected)


@pytest.mark.parametrize(("structure", "churn", "expected"), [
    ({"reach": 50, "sensitive_domains": ["auth"]}, SMALL_CHURN, "sensitive domain (escalates regardless of size)"),
    ({"reach": 50}, LARGE_CHURN, "wide reach and large churn"),
    ({"reach": 50}, SMALL_CHURN, "wide reach"),
    ({"reach": 2}, LARGE_CHURN, "large churn"),
    ({"reach": 2, "cross_domain": True}, SMALL_CHURN, "cross-domain change"),
    ({"reach": 2}, SMALL_CHURN, "contained reach and churn"),
    ({"reach": 2}, None, "contained reach and churn"),
    (None, None, "contained reach and churn"),
])
def test_driver_names_the_strongest_axis(structure, churn, expected):
    assert br.driver(structure, churn) == expected


def test_count_patch_lines_ignores_headers():
    patch = "--- a/x\n+++ b/x\n@@\n+added one\n+added two\n-removed one\n unchanged"
    assert br.count_patch_lines(patch) == (2, 1)


def test_compute_churn_excludes_generated_and_sums():
    files = [
        _file("libs/a/src/index.ts", "+one\n+two\n-three"),
        _file("package-lock.json", "+lots\n+more"),
        _file("libs/b/foo.snap", "+snap"),
    ]
    assert br.compute_churn(files) == {"files": 1, "added": 2, "deleted": 1}


def test_compute_churn_none_without_diff():
    assert br.compute_churn(None) is None


def test_compute_churn_reads_file_patch_info():
    files = [FilePatchInfo(base_file="", head_file="", patch="@@\n+a\n-b\n-c", filename="src/app.py")]
    assert br.compute_churn(files) == {"files": 1, "added": 1, "deleted": 2}


IGNORED_MECHANICAL_FILES = [
    "package-lock.json",
    "apps/web/package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "bun.lockb",
    "deno.lock",
    "libs/backend/api/gen/client.ts",
    "libs/backend/api/generated/schema.py",
    "libs/player/api.zod.ts",
    "libs/player/client.generated.tsx",
    "libs/player/handlers.mock.ts",
    "assets/app.min.js",
    "assets/app.min.css",
    "dist/main.js",
    "bundle.js.map",
    "styles.css.map",
    "libs/b/foo.snap",
    "src/__snapshots__/widget.spec.js",
    "src/__image_snapshots__/button.png",
    "tests/snapshots/result.ambr",
    "spec/cassettes/login.yml",
    "test/vcr_cassettes/api_call.yml",
    "poetry.lock",
    "uv.lock",
    "Pipfile.lock",
    "Gemfile.lock",
    "composer.lock",
    "Cargo.lock",
    "go.sum",
    "mix.lock",
    "pubspec.lock",
    "Podfile.lock",
    "Package.resolved",
    "gradle.lockfile",
    "backend/packages.lock.json",
    "flake.lock",
    ".terraform.lock.hcl",
    "infra/layers/100ci/.terraform.lock.hcl",
    "proto/service_pb2.py",
    "proto/service_pb2_grpc.py",
    "internal/api/service.pb.go",
    "web/gen_pb.js",
    "proto/service_pb2.pyi",
    "proto/service_pb2_grpc.pyi",
    "vendor/github.com/pkg/errors/errors.go",
    "deep/nested/vendor/pkg/errors.go",
    "third-party/node_modules/lodash/index.js",
    "src/third-party/node_modules/lodash/index.js",
]

AUTHORED_FILES = [
    "package.json",
    "pyproject.toml",
    "go.mod",
    "Gemfile",
    "Cargo.toml",
    "requirements.txt",
    "mix.exs",
    "pubspec.yaml",
    "main.tf",
    "src/lock.ts",
    "src/locker/index.ts",
    "docs/generated-content-guide.md",
    "sitemap.xml",
    "README.md",
]


def test_ignored_churn_matches_mechanical_files():
    assert [name for name in IGNORED_MECHANICAL_FILES if not br.IGNORED_CHURN.search(name)] == []


def test_ignored_churn_keeps_authored_files():
    assert [name for name in AUTHORED_FILES if br.IGNORED_CHURN.search(name)] == []


def test_compute_churn_applies_extra_ignore_pattern():
    files = [_file("docs/huge-generated-page.md", "+a\n+b"), _file("src/real-change.ts", "+one")]
    extra = br.compile_churn_ignore(r"(^|/)docs/")
    assert br.compute_churn(files, extra) == {"files": 1, "added": 1, "deleted": 0}


def test_invalid_extra_ignore_pattern_is_dropped():
    assert br.compile_churn_ignore("([unclosed") is None
    assert br.compile_churn_ignore("") is None
    assert br.compile_churn_ignore(None) is None


def test_path_signal_none_without_paths():
    assert br.path_signal(None) is None
    assert br.path_signal([]) is None


def test_path_signal_counts_areas_and_cross_area():
    signal = br.path_signal(["src/player/a.ts", "src/player/b.ts", "docs/x.md"])
    assert signal["areas"] == 2
    assert signal["cross_area"] is True
    assert signal["sensitive_hits"] == []


def test_path_signal_single_area_not_cross():
    signal = br.path_signal(["src/player/a.ts", "src/player/b.ts"])
    assert signal["areas"] == 1
    assert signal["cross_area"] is False


def test_path_signal_detects_sensitive_paths():
    signal = br.path_signal(["libs/auth/login.ts", "services/payments/charge.ts"])
    assert "auth" in signal["sensitive_hits"]
    assert "payment" in signal["sensitive_hits"]


def test_path_signal_ignores_sensitive_lookalikes():
    assert br.path_signal(["docs/authored-guide.md", "src/tokenizer.ts"])["sensitive_hits"] == []


def test_path_signal_extends_sensitive_terms():
    signal = br.path_signal(["libs/payout/run.ts", "services/kyc/check.ts"], ("payout", "kyc"))
    assert "payout" in signal["sensitive_hits"]
    assert "kyc" in signal["sensitive_hits"]


def test_path_band_small_change_is_low():
    band = br.path_band(
        {"areas": 1, "cross_area": False, "sensitive_hits": []},
        {"files": 1, "added": 3, "deleted": 0},
    )
    assert band == "Low"


def test_path_band_large_churn_is_moderate():
    band = br.path_band(
        {"areas": 1, "cross_area": False, "sensitive_hits": []},
        {"files": 40, "added": 500, "deleted": 0},
    )
    assert band == "Moderate"


def test_path_band_caps_sensitive_and_spread_at_moderate():
    band = br.path_band(
        {"areas": 40, "cross_area": True, "sensitive_hits": ["payment"]},
        {"files": 80, "added": 2000, "deleted": 500},
    )
    assert band == "Moderate"


@pytest.mark.parametrize(("areas", "cross_area", "churn", "expected"), [
    (10, False, SMALL_CHURN, "Low"),
    (11, False, SMALL_CHURN, "Moderate"),
    (1, True, SMALL_CHURN, "Low"),
    (1, False, {"files": 1, "added": 50, "deleted": 0}, "Low"),
    (2, True, {"files": 1, "added": 50, "deleted": 0}, "Moderate"),
    (2, True, LARGE_CHURN, "Moderate"),
    (2, False, LARGE_CHURN, "Moderate"),
])
def test_path_band_triggers(areas, cross_area, churn, expected):
    assert br.path_band({"areas": areas, "cross_area": cross_area, "sensitive_hits": []}, churn) == expected


@pytest.mark.parametrize(("signal", "churn", "reason"), [
    ({"areas": 1, "cross_area": False, "sensitive_hits": ["auth"]}, LARGE_CHURN, "sensitive path match"),
    ({"areas": 1, "cross_area": False, "sensitive_hits": []}, LARGE_CHURN, "large churn"),
    ({"areas": 11, "cross_area": True, "sensitive_hits": []}, SMALL_CHURN, "cross-area change"),
    ({"areas": 11, "cross_area": False, "sensitive_hits": []}, SMALL_CHURN, "many areas touched"),
    ({"areas": 10, "cross_area": False, "sensitive_hits": []}, SMALL_CHURN, "small, contained change"),
    ({"areas": 1, "cross_area": False, "sensitive_hits": []}, SMALL_CHURN, "small, contained change"),
])
def test_path_driver_names_the_strongest_signal(signal, churn, reason):
    assert br.path_driver(signal, churn) == f"{reason} (heuristic estimate, no dependency graph)"


def test_assess_path_heuristic_counts_many_areas_in_one_tree():
    files = [_file(f"src/area{i}/mod.ts", "+a") for i in range(11)]
    row = br.assess_change(files, path_heuristic=True)
    assert row["band"] == "Moderate"
    assert row["driver"] == "many areas touched (heuristic estimate, no dependency graph)"
    assert {"label": "Reach (est.)", "value": "~11 areas, path-based"} in row["factors"]


def test_assess_path_heuristic_ten_areas_in_one_tree_stays_low():
    files = [_file(f"src/area{i}/mod.ts", "+a") for i in range(10)]
    row = br.assess_change(files, path_heuristic=True)
    assert row["band"] == "Low"
    assert row["driver"] == "small, contained change (heuristic estimate, no dependency graph)"


def test_assess_path_heuristic_rates_nameless_file_by_churn_alone():
    row = br.assess_change([_file("", "+a")], path_heuristic=True)
    assert row == {
        "band": "Low",
        "level": 0,
        "heuristic": True,
        "factors": [{"label": "Churn", "value": "1 file, +1/-0"}],
        "driver": "small, contained change (heuristic estimate, no dependency graph)",
    }


def test_assess_with_structure():
    row = br.assess_change([_file("a.ts", "+a\n+b\n-c")], structure=STRUCTURE)
    assert row["band"] == "Moderate"
    assert row["heuristic"] is False
    assert {"label": "Reach", "value": "69 projects, cross-domain"} in row["factors"]
    assert {"label": "Churn", "value": "1 file, +2/-1"} in row["factors"]
    assert row["driver"] == "wide reach"


def test_assess_without_structure_or_diff_is_none():
    assert br.assess_change(None) is None
    assert br.assess_change(None, path_heuristic=True) is None


def test_assess_path_heuristic_is_off_by_default():
    assert br.assess_change(_multi_file_diff()) is None


def test_assess_path_heuristic_when_enabled():
    row = br.assess_change(_multi_file_diff(), path_heuristic=True)
    assert row["heuristic"] is True
    assert row["band"] == "Moderate"
    assert {"label": "Churn", "value": "2 files, +3/-1"} in row["factors"]
    assert {"label": "Sensitivity (path match)", "value": "auth"} in row["factors"]
    assert row["driver"].startswith("sensitive path match")


def test_assess_path_heuristic_none_when_all_files_mechanical():
    files = [_file("package-lock.json", "+lots\n+more"), _file("dist/main.js", "+bundle")]
    assert br.assess_change(files, path_heuristic=True) is None


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
        {"domains": "draw"},
        {"domains": [1, 2]},
        {"sensitive_domains": [""]},
        {"sensitive_domains": ["x" * (br.MAX_NAME_CHARS + 1)]},
        {},
        {"unrelated": 1},
    ],
)
def test_parse_structure_rejects_malformed_input(raw):
    assert br.parse_structure(raw) is None


def test_parse_structure_accepts_zero_reach():
    assert br.parse_structure({"reach": 0}) == {"reach": 0}


def test_parse_structure_accepts_name_of_exactly_the_limit():
    name = "x" * br.MAX_NAME_CHARS
    assert br.parse_structure({"domains": [name]}) == {"domains": [name]}


def test_parse_structure_keeps_known_fields_and_drops_unknown():
    parsed = br.parse_structure({
        "version": 1,
        "reach": 4,
        "dependents": ["a", "b"],
        "domains": ["draw", "player"],
        "cross_domain": True,
        "sensitive_domains": ["auth"],
        "tool": "anything",
    })
    assert parsed == {
        "reach": 4,
        "dependents": ["a", "b"],
        "domains": ["draw", "player"],
        "cross_domain": True,
        "sensitive_domains": ["auth"],
    }


def test_parse_structure_derives_reach_from_dependents():
    assert br.parse_structure({"dependents": ["a", "b", "c"]})["reach"] == 3


def test_load_structure_reads_file_under_workspace(blast_settings, tmp_path):
    name = _write_input(tmp_path, {"reach": 5, "domains": ["draw"]})
    assert br.load_structure(name, resolve_artifact_path) == {"reach": 5, "domains": ["draw"]}


@pytest.mark.parametrize("payload", ["{not json", json.dumps({"reach": "five"}), "\udcff"])
def test_load_structure_fails_open_on_bad_file(blast_settings, tmp_path, payload):
    path = tmp_path / "blast-radius.json"
    path.write_bytes(payload.encode("utf-8", "surrogateescape"))
    assert br.load_structure(path.name, resolve_artifact_path) is None


def test_load_structure_ignores_non_string_path(blast_settings, tmp_path):
    name = _write_input(tmp_path, {"reach": 5})
    assert br.load_structure(tmp_path / name, resolve_artifact_path) is None
    assert br.load_structure(None, resolve_artifact_path) is None


def test_load_structure_rejects_missing_and_outside_files(blast_settings, tmp_path):
    outside = tmp_path.parent / "outside-blast-radius.json"
    outside.write_text(json.dumps({"reach": 5}), encoding="utf-8")
    try:
        assert br.load_structure("missing.json", resolve_artifact_path) is None
        assert br.load_structure(str(outside), resolve_artifact_path) is None
        assert br.load_structure("", resolve_artifact_path) is None
    finally:
        outside.unlink()


def test_load_structure_rejects_oversized_file(blast_settings, tmp_path, monkeypatch):
    monkeypatch.setattr(br, "MAX_INPUT_BYTES", 10)
    name = _write_input(tmp_path, {"reach": 5, "domains": ["a-long-domain-name"]})
    assert br.load_structure(name, resolve_artifact_path) is None


@pytest.mark.parametrize(("size_offset", "expected"), [(0, {"reach": 5}), (-1, None)])
def test_load_structure_size_limit_is_inclusive(blast_settings, tmp_path, monkeypatch, size_offset, expected):
    name = _write_input(tmp_path, {"reach": 5})
    monkeypatch.setattr(br, "MAX_INPUT_BYTES", (tmp_path / name).stat().st_size + size_offset)
    assert br.load_structure(name, resolve_artifact_path) == expected


def test_build_is_off_by_default(blast_settings):
    blast_settings.set("pr_reviewer.enable_blast_radius", False)
    blast_settings.set("pr_reviewer.blast_radius_path_heuristic", True)
    assert br.build_blast_radius(_multi_file_diff(), resolve_artifact_path) is None


def test_build_reads_input_file(blast_settings, tmp_path):
    blast_settings.set("pr_reviewer.blast_radius_input_file", _write_input(tmp_path, STRUCTURE))
    row = br.build_blast_radius([_file("a.ts", "+a")], resolve_artifact_path)
    assert row["band"] == "Moderate"
    assert row["heuristic"] is False


def test_build_falls_back_to_path_heuristic_on_malformed_input(blast_settings, tmp_path):
    blast_settings.set("pr_reviewer.blast_radius_input_file", _write_input(tmp_path, "{broken"))
    assert br.build_blast_radius(_multi_file_diff(), resolve_artifact_path) is None
    blast_settings.set("pr_reviewer.blast_radius_path_heuristic", True)
    assert br.build_blast_radius(_multi_file_diff(), resolve_artifact_path)["heuristic"] is True


def test_build_applies_configured_terms_and_pattern(blast_settings):
    blast_settings.set("pr_reviewer.blast_radius_path_heuristic", "true")
    blast_settings.set("pr_reviewer.blast_radius_sensitive_terms", ["payout"])
    blast_settings.set("pr_reviewer.blast_radius_churn_ignore_pattern", r"(^|/)docs/")
    files = [_file("libs/payout/run.ts", "+a"), _file("docs/page.md", "+b\n+c")]
    row = br.build_blast_radius(files, resolve_artifact_path)
    assert {"label": "Sensitivity (path match)", "value": "payout"} in row["factors"]
    assert {"label": "Churn", "value": "1 file, +1/-0"} in row["factors"]


def test_build_accepts_comma_separated_terms(blast_settings):
    blast_settings.set("pr_reviewer.blast_radius_path_heuristic", True)
    blast_settings.set("pr_reviewer.blast_radius_sensitive_terms", "payout, kyc")
    row = br.build_blast_radius([_file("libs/kyc/run.ts", "+a")], resolve_artifact_path)
    assert {"label": "Sensitivity (path match)", "value": "kyc"} in row["factors"]


def test_build_is_off_when_enable_setting_is_missing(monkeypatch):
    monkeypatch.setattr(br, "get_settings", lambda: SimpleNamespace(pr_reviewer={"blast_radius_path_heuristic": True}))
    assert br.build_blast_radius(_multi_file_diff(), resolve_artifact_path) is None


def test_build_path_heuristic_is_off_when_setting_is_missing(monkeypatch):
    monkeypatch.setattr(br, "get_settings", lambda: SimpleNamespace(pr_reviewer={"enable_blast_radius": True}))
    assert br.build_blast_radius(_multi_file_diff(), resolve_artifact_path) is None


def test_build_fails_open_on_any_exception(blast_settings, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(br, "assess_change", boom)
    assert br.build_blast_radius(_multi_file_diff(), resolve_artifact_path) is None


def test_render_gfm_row():
    row = br.assess_change([_file("a.ts", "+a\n+b\n-c")], structure=STRUCTURE)
    rendered = br.render_blast_radius(row, gfm_supported=True)
    assert rendered == (
        "<tr><td>💣&nbsp;<strong>Blast radius</strong>: <strong>Moderate</strong>"
        "<br>Reach: 69 projects, cross-domain<br>Churn: 1 file, +2/-1"
        "<br><em>Rating driven by wide reach</em></td></tr>\n"
    )


def test_render_markdown_row_labels_heuristic():
    row = br.assess_change(_multi_file_diff(), path_heuristic=True)
    rendered = br.render_blast_radius(row, gfm_supported=False)
    assert rendered.startswith("### 💣 Blast radius _(heuristic)_: **Moderate**\n\n")
    assert "- Churn: 2 files, +3/-1\n" in rendered


MARKDOWN_ROW = {
    "band": "High",
    "driver": "a_b*c",
    "factors": [{"label": "L[1]", "value": "`v`|x <b>"}],
}


def test_render_markdown_escapes_special_characters():
    assert br.render_blast_radius(MARKDOWN_ROW, gfm_supported=False) == (
        "### 💣 Blast radius: **High**\n\n"
        "- L\\[1\\]: \\`v\\`\\|x &lt;b&gt;\n"
        "- Rating driven by a\\_b\\*c\n\n"
    )


def test_render_gfm_keeps_special_characters_unescaped():
    assert br.render_blast_radius(MARKDOWN_ROW, gfm_supported=True) == (
        "<tr><td>💣&nbsp;<strong>Blast radius</strong>: <strong>High</strong>"
        "<br>L[1]: `v`|x &lt;b&gt;<br><em>Rating driven by a_b*c</em></td></tr>\n"
    )


def test_render_escapes_input_values():
    row = br.assess_change(None, structure={"reach": 1, "sensitive_domains": ["<img src=x>"]})
    rendered = br.render_blast_radius(row, gfm_supported=True)
    assert "<img" not in rendered
    assert "&lt;img src=x&gt;" in rendered


@pytest.mark.parametrize("factors", [
    ["Reach"],
    [{"label": "Reach"}],
    [{"value": "5"}],
    [{"label": "Reach", "value": 5}],
    [{"label": 1, "value": "5"}],
    [{"label": "Reach", "value": "5"}, {"label": "Churn", "value": None}],
])
def test_render_skips_row_with_malformed_factor(factors):
    row = {"band": "Low", "factors": factors, "driver": "x"}
    assert br.render_blast_radius(row, gfm_supported=True) == ""
    assert br.render_blast_radius(row, gfm_supported=False) == ""


def test_render_skips_row_with_non_string_driver():
    assert br.render_blast_radius({"band": "Low", "factors": [], "driver": 1}, gfm_supported=True) == ""


@pytest.mark.parametrize(("value", "expected"), [
    ("payout, kyc", ("payout", "kyc")),
    (" a , ,b,", ("a", "b")),
    (["a", " b ", "", "  ", 5, None], ("a", "b")),
    (("a", "b"), ("a", "b")),
    (5, ()),
    (None, ()),
])
def test_as_terms_keeps_only_non_blank_strings(value, expected):
    assert br._as_terms(value) == expected


@pytest.mark.parametrize("row", ["Critical", {}, {"band": "Severe", "factors": [], "driver": "x"},
                                 {"band": "Low", "factors": [{"label": 1}], "driver": "x"}])
def test_render_skips_malformed_row(row):
    assert br.render_blast_radius(row, gfm_supported=True) == ""
    assert br.render_blast_radius(row, gfm_supported=False) == ""


@pytest.mark.parametrize(("value", "expected"), [
    (True, True), (False, False), (1, True), (0, False), (2, False),
    ("1", True), ("true", True), ("ON", True), ("0", False), ("no", False), ("", False), (None, False),
])
def test_toggle_values(blast_settings, value, expected):
    blast_settings.set("pr_reviewer.enable_blast_radius", value)
    blast_settings.set("pr_reviewer.blast_radius_path_heuristic", True)
    assert (br.build_blast_radius(_multi_file_diff(), resolve_artifact_path) is not None) is expected


def test_convert_to_markdown_renders_row_first():
    row = br.assess_change([_file("a.ts", "+a")], structure=STRUCTURE)
    review = {"review": {"blast_radius": row, "estimated_effort_to_review_[1-5]": "2"}}
    markdown = convert_to_markdown_v2(review, gfm_supported=True)
    assert markdown.index("Blast radius") < markdown.index("Estimated effort to review")


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


def test_prepare_pr_review_adds_row_when_enabled(blast_settings, tmp_path):
    blast_settings.set("pr_reviewer.blast_radius_input_file", _write_input(tmp_path, STRUCTURE))
    markdown = _prepare(_reviewer(), "review:\n  relevant_tests: |\n    No\n", [_file("a.ts", "+a")])
    assert "💣&nbsp;<strong>Blast radius</strong>: <strong>Moderate</strong>" in markdown


def test_prepare_pr_review_has_no_row_when_disabled(blast_settings, tmp_path):
    blast_settings.set("pr_reviewer.enable_blast_radius", False)
    blast_settings.set("pr_reviewer.blast_radius_input_file", _write_input(tmp_path, STRUCTURE))
    markdown = _prepare(_reviewer(), "review:\n  relevant_tests: |\n    No\n", [_file("a.ts", "+a")])
    assert "Blast radius" not in markdown


def test_prepare_pr_review_drops_model_supplied_row(blast_settings):
    blast_settings.set("pr_reviewer.enable_blast_radius", False)
    review_yaml = "review:\n  relevant_tests: |\n    No\n  blast_radius: Critical\n"
    markdown = _prepare(_reviewer(), review_yaml, [_file("a.ts", "+a")])
    assert "Blast radius" not in markdown


def test_input_file_and_pattern_are_host_only_for_comment_arguments():
    assert {"blast_radius_input_file", "blast_radius_churn_ignore_pattern"} <= (
        CLI_HOST_ONLY_KEYS_BY_SECTION["pr_reviewer"]
    )


def test_count_patch_lines_counts_repeated_sign_content_inside_hunks():
    patch = "--- a/x.cpp\n+++ b/x.cpp\n@@ -1,1 +1,1 @@\n+++counter;\n---counter;\n context"
    assert br.count_patch_lines(patch) == (1, 1)


@pytest.mark.parametrize(
    "gfm_supported, expected",
    [
        (True, "Sensitivity: auth  ![x](https://example.com/x.png)"),
        (False, "- Sensitivity: auth  \\!\\[x\\]\\(https://example.com/x.png\\)"),
    ],
)
def test_render_keeps_multiline_values_on_one_line(gfm_supported, expected):
    row = br.assess_change(None, structure={"reach": 1, "sensitive_domains": ["auth\n\n![x](https://example.com/x.png)"]})
    rendered = br.render_blast_radius(row, gfm_supported=gfm_supported)
    sensitivity = next(line for line in rendered.split("<br>" if gfm_supported else "\n") if "Sensitivity" in line)
    assert sensitivity == expected
    assert "\n\n" not in rendered.split("Sensitivity", 1)[1].split("Rating driven", 1)[0]
