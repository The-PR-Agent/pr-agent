import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo import deterministic_risk as dr
from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
from pr_agent.algo.utils import convert_to_markdown_v2
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.config_loader import get_settings
from tests.unittest._settings_helpers import restore_settings, snapshot_settings

_KEYS = (
    "pr_reviewer.enable_risk_checks",
    "pr_reviewer.sensitive_paths",
    "pr_reviewer.extra_risky_shapes",
)


@pytest.fixture
def risk_settings():
    snapshot = snapshot_settings(_KEYS)
    settings = get_settings()
    settings.set("pr_reviewer.enable_risk_checks", True)
    settings.set("pr_reviewer.sensitive_paths", list(dr.DEFAULT_SENSITIVE_PATHS))
    settings.set("pr_reviewer.extra_risky_shapes", [])
    yield settings
    restore_settings(snapshot)


def _file(filename, patch="", edit_type=EDIT_TYPE.MODIFIED):
    return FilePatchInfo(
        base_file="",
        head_file="",
        patch=patch,
        filename=filename,
        edit_type=edit_type,
    )


# 1. normalize_file_path tests
def test_normalize_file_path():
    assert dr.normalize_file_path(None) == ""
    assert dr.normalize_file_path("") == ""
    assert dr.normalize_file_path("src\\models\\user.py") == "src/models/user.py"
    assert dr.normalize_file_path("/src/app.py") == "src/app.py"
    assert dr.normalize_file_path("  src/app.py  ") == "src/app.py"


# 2. matches_sensitive_path tests
@pytest.mark.parametrize(
    ("path", "expected_match"),
    [
        (".github/workflows/deploy.yml", True),
        (".github/workflows/sub/ci.yaml", True),
        (".gitlab-ci.yml", True),
        ("Jenkinsfile", True),
        ("Jenkinsfile.deploy", True),
        (".circleci/config.yml", True),
        ("azure-pipelines.yml", True),
        (".travis.yml", True),
        ("bitbucket-pipelines.yml", True),
        ("CODEOWNERS", True),
        (".github/CODEOWNERS", True),
        (".pr_agent.toml", True),
        ("package.json", True),
        ("frontend/package-lock.json", True),
        ("yarn.lock", True),
        ("pnpm-lock.yaml", True),
        ("requirements.txt", True),
        ("requirements-dev.txt", True),
        ("pyproject.toml", True),
        ("uv.lock", True),
        ("Cargo.toml", True),
        ("go.mod", True),
        ("pom.xml", True),
        ("build.gradle", True),
        ("Dockerfile", True),
        ("docker/Dockerfile.test", True),
        ("docker-compose.yml", True),
        ("infra/main.tf", True),
        ("terraform/variables.tfvars", True),
        ("terragrunt.hcl", True),
        ("src/db/migrations/0001_init.py", True),
        ("db/migrate/20260101_users.rb", True),
        ("alembic/versions/a1b2c3_add_col.py", True),
        ("src/app/main.py", False),
        ("tests/test_routes.py", False),
        ("README.md", False),
    ],
)
def test_matches_sensitive_path_defaults(path, expected_match):
    matched = dr.matches_sensitive_path(path)
    assert bool(matched) is expected_match


def test_matches_sensitive_path_custom():
    custom = ["config/secrets/**", "*.pem"]
    assert dr.matches_sensitive_path("config/secrets/prod.json", custom) == "config/secrets/**"
    assert dr.matches_sensitive_path("certs/server.pem", custom) == "*.pem"
    assert dr.matches_sensitive_path(".github/workflows/ci.yml", custom) is None
    assert dr.matches_sensitive_path("config/secrets/prod.json", ["prod.json", "config/**"]) == "prod.json"


def test_matches_sensitive_path_ignores_oversized_globs():
    oversized = "a" * (dr.MAX_GLOB_LENGTH + 1)
    assert dr.matches_sensitive_path("app.py", [oversized, "app.py"]) == "app.py"


# 3. is_prompt_file tests
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("pr_reviewer_prompts.toml", True),
        ("pr_agent/settings/pr_description_prompts.toml", True),
        ("prompts/custom.yaml", True),
        ("pr_reviewer.py", False),
        ("configuration.toml", False),
    ],
)
def test_is_prompt_file(path, expected):
    assert dr.is_prompt_file(path) is expected


# 4. compile_custom_shapes tests
def test_compile_custom_shapes():
    patterns = [
        r"\bREVOKE_SECRET\b",
        "a" * (dr.MAX_CUSTOM_PATTERN_LENGTH + 1),  # oversized
        r"(a+)+",  # unsafe ReDoS
        r"(a|a)*$",  # unsafe alternation under quantifier
        r"[invalid",  # invalid regex
    ]
    compiled = dr.compile_custom_shapes(patterns)
    assert len(compiled) == 1
    assert compiled[0].pattern == r"\bREVOKE_SECRET\b"


# 5. scan_file_diff tests
def test_scan_file_diff_deleted_file():
    f = _file("legacy.py", "", edit_type=EDIT_TYPE.DELETED)
    findings = dr.scan_file_diff(f)
    assert any(item["category"] == "deleted_file" for item in findings)


def test_scan_file_diff_prompt_file():
    f = _file("pr_reviewer_prompts.toml", "@@ -1 +1 @@\n-old\n+new")
    findings = dr.scan_file_diff(f)
    assert any(item["category"] == "prompt_file" for item in findings)


@pytest.mark.parametrize(
    ("line", "expected_cat"),
    [
        ("+ DROP TABLE users;", "destructive_sql"),
        ("+ DROP SCHEMA legacy CASCADE;", "destructive_sql"),
        ("+ DROP DATABASE analytics;", "destructive_sql"),
        ("+ ALTER TABLE accounts RENAME COLUMN email TO contact;", "destructive_sql"),
        ("+ ALTER users RENAME TO old_users;", "destructive_sql"),
        ("+ REVOKE SELECT ON accounts FROM public;", "destructive_sql"),
        ("+ REVOKE ALL PRIVILEGES ON accounts FROM user;", "destructive_sql"),
        ("+ REVOKE SELECT, INSERT, UPDATE ON accounts FROM user;", "destructive_sql"),
        ("+ TRUNCATE TABLE sessions;", "destructive_sql"),
        ("+ api_key = 'sk-1234567890'", "auth_and_secrets"),
        ("+ jwt_secret: 'topsecret'", "auth_and_secrets"),
        ("+ redis.flushdb()", "cache_logic"),
        ("+ @lru_cache(maxsize=128)", "cache_logic"),
    ],
)
def test_scan_file_diff_risky_shapes_added(line, expected_cat):
    patch = f"@@ -1,3 +1,3 @@\n context line\n{line}\n another context"
    f = _file("service.py", patch)
    findings = dr.scan_file_diff(f)
    assert any(item["category"] == expected_cat for item in findings), f"Failed for {line}"


@pytest.mark.parametrize(
    ("line", "expected_cat"),
    [
        ("-def calculate_discount(price, user):", "removed_function"),
        ("-async def fetch_user(user_id):", "removed_function"),
        ("-export function processPayment(token) {", "removed_function"),
        ("-func ValidateSignature(req *Request) bool {", "removed_function"),
        ("-pub fn verify_signature(data: &[u8]) -> bool {", "removed_function"),
        # Methods: the replay over 1,307 merged PRs found these were never flagged.
        ("-    def get_files(self):", "removed_function"),
        ("-        async def publish(self, body):", "removed_function"),
        ("-\tdef tabbed(self):", "removed_function"),
        ("-  async function load() {", "removed_function"),
        ("-    pub fn verify(&self) -> bool {", "removed_function"),
    ],
)
def test_scan_file_diff_removed_functions(line, expected_cat):
    patch = f"@@ -1,3 +1,3 @@\n context\n{line}\n+def new_impl():"
    f = _file("auth.py", patch)
    findings = dr.scan_file_diff(f)
    assert any(item["category"] == expected_cat for item in findings), f"Failed for {line}"


def test_scan_file_diff_removed_config_defaults():
    patch = "@@ -1,2 +1,2 @@\n-timeout = 30\n+timeout = 60"
    f = _file("settings.toml", patch)
    findings = dr.scan_file_diff(f)
    assert any(item["category"] == "removed_config_default" for item in findings)


def test_scan_file_diff_custom_pattern():
    custom = dr.compile_custom_shapes([r"DEPRECATED_FEATURE"])
    patch = "@@ -1 +1 @@\n+use DEPRECATED_FEATURE();"
    f = _file("app.py", patch)
    findings = dr.scan_file_diff(f, custom_patterns=custom)
    assert any(item["category"] == "custom_shape" for item in findings)


class _TimingOutPattern:
    pattern = "slow"

    def __init__(self):
        self.calls = 0

    def search(self, text, timeout=None):
        self.calls += 1
        raise TimeoutError("regex timed out")


def test_custom_pattern_that_times_out_is_skipped_for_the_rest_of_the_review(monkeypatch):
    logger = MagicMock()
    monkeypatch.setattr(dr, "get_logger", lambda: logger)
    slow = _TimingOutPattern()
    custom = [slow, *dr.compile_custom_shapes([r"DEPRECATED_FEATURE"])]
    patch = "@@ -1,2 +1,2 @@\n+first line\n+use DEPRECATED_FEATURE();"

    first = dr.scan_file_diff(_file("a.py", patch), custom_patterns=custom)
    second = dr.scan_file_diff(_file("b.py", patch), custom_patterns=custom)

    assert slow.calls == 1
    assert [item["file"] for item in first + second if item["category"] == "custom_shape"] == ["a.py", "b.py"]
    logger.warning.assert_called_once()


class _SlowPattern:
    """Never times out, but each search takes 1/32 s on the fake clock."""

    pattern = "slow"

    def __init__(self, clock):
        self.clock = clock
        self.timeouts = []

    def search(self, text, timeout=None):
        self.timeouts.append(timeout)
        self.clock[0] += 1 / 32
        return None


def test_pattern_slow_on_every_line_is_dropped_after_its_review_budget(monkeypatch, risk_settings):
    clock = [0.0]
    monkeypatch.setattr(dr, "time", SimpleNamespace(perf_counter=lambda: clock[0]), raising=False)
    monkeypatch.setattr(dr, "CUSTOM_PATTERN_REVIEW_BUDGET_SECONDS", 1.0, raising=False)
    slow = _SlowPattern(clock)
    monkeypatch.setattr(dr, "compile_custom_shapes", lambda patterns: [slow])
    patch = "@@ -1,20 +1,20 @@\n" + "\n".join(f"+line {i}" for i in range(20))

    dr.build_risk_checks([_file("a.py", patch), _file("b.py", patch)])

    assert len(slow.timeouts) == 32
    assert all(0 < timeout <= dr.CUSTOM_PATTERN_TIMEOUT_SECONDS for timeout in slow.timeouts)


def test_backtracking_custom_pattern_stays_within_its_time_budget():
    code = (
        "import time\n"
        "from pr_agent.algo import deterministic_risk as dr\n"
        "from pr_agent.algo.types import FilePatchInfo\n"
        "custom = dr.compile_custom_shapes([r'((a+))+$'])\n"
        "assert len(custom) == 1, 'pattern passes the static check'\n"
        "line = '+' + 'a' * (dr.MAX_LINE_CHARS - 2) + '!'\n"
        "f = FilePatchInfo(base_file='', head_file='', patch='\\n'.join([line] * 50), filename='a.py')\n"
        "start = time.perf_counter()\n"
        "dr.scan_file_diff(f, custom_patterns=custom)\n"
        "print(time.perf_counter() - start, len(custom))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    elapsed, remaining = result.stdout.split()
    assert float(elapsed) < 5
    assert remaining == "0"


# 6. build_risk_checks tests
def test_build_risk_checks_disabled(risk_settings):
    risk_settings.set("pr_reviewer.enable_risk_checks", False)
    assert dr.build_risk_checks([_file("app.py", "+1")]) is None


def test_build_risk_checks_string_bool_disabled(risk_settings):
    for false_val in ("false", "FALSE", "0", False):
        risk_settings.set("pr_reviewer.enable_risk_checks", false_val)
        assert dr.build_risk_checks([_file(".github/workflows/ci.yml", "+1")]) is None


def test_build_risk_checks_non_list_sensitive_paths_fallback(risk_settings):
    # Non-list value (e.g. integer or object) should fall back to DEFAULT_SENSITIVE_PATHS
    risk_settings.set("pr_reviewer.sensitive_paths", 12345)
    res = dr.build_risk_checks([_file(".github/workflows/ci.yml", "+1")])
    assert res is not None
    assert res["has_findings"] is True
    assert any(".github/workflows/ci.yml" in s["file"] for s in res["sensitive_paths"])


def test_build_risk_checks_empty():
    assert dr.build_risk_checks([]) is None
    assert dr.build_risk_checks(None) is None


def test_build_risk_checks_clean_pr(risk_settings):
    files = [
        _file("src/utils.py", "@@ -1,2 +1,2 @@\n context\n+a = 1"),
        _file("tests/test_utils.py", "@@ -1,2 +1,2 @@\n context\n+assert a == 1"),
    ]
    res = dr.build_risk_checks(files)
    assert res is None


def test_build_risk_checks_with_findings(risk_settings):
    files = [
        _file(".github/workflows/ci.yml", "@@ -1 +1 @@\n+  run: pytest"),
        _file("db.py", "@@ -1 +1 @@\n+DROP TABLE users;"),
        _file("old.py", "", edit_type=EDIT_TYPE.DELETED),
    ]
    res = dr.build_risk_checks(files)
    assert res is not None
    assert res["has_findings"] is True
    assert len(res["sensitive_paths"]) == 1
    assert res["sensitive_paths"][0]["file"] == ".github/workflows/ci.yml"
    assert len(res["risky_shapes"]) == 1
    assert res["risky_shapes"][0]["category"] == "destructive_sql"
    assert len(res["deleted_files"]) == 1
    assert res["deleted_files"][0] == "old.py"


def test_build_risk_checks_renamed_sensitive_file(risk_settings):
    # Moving a sensitive file to a non-sensitive path should still flag it
    f = _file("scripts/temp.yml", "@@ -1 +1 @@\n+echo 1")
    f.old_filename = ".github/workflows/ci.yml"
    res = dr.build_risk_checks([f])
    assert res is not None
    assert res["has_findings"] is True
    assert any(".github/workflows/ci.yml" in s["file"] for s in res["sensitive_paths"])


def test_build_risk_checks_unfiltered_lockfiles(risk_settings):
    # Lockfiles dropped by get_diff_files should still be flagged via unfiltered_files
    clean_diff = [_file("src/utils.py", "@@ -1,2 +1,2 @@\n context\n+a = 1")]
    unfiltered = ["package-lock.json", "yarn.lock"]
    res = dr.build_risk_checks(clean_diff, unfiltered_files=unfiltered)
    assert res is not None
    assert res["has_findings"] is True
    assert len(res["sensitive_paths"]) == 2
    matched_names = {s["file"] for s in res["sensitive_paths"]}
    assert "package-lock.json" in matched_names
    assert "yarn.lock" in matched_names


def test_extra_risky_shapes_is_repo_host_only():
    from pr_agent.config_security import is_repo_host_only_key
    assert is_repo_host_only_key("pr_reviewer", "extra_risky_shapes") is True
    assert is_repo_host_only_key("PR_REVIEWER", "EXTRA_RISKY_SHAPES") is True


# 7. render_risk_checks tests
def test_render_risk_checks_malformed():
    assert dr.render_risk_checks(None, True) == ""
    assert dr.render_risk_checks("bad", False) == ""


def test_render_risk_checks_clean():
    clean = {"has_findings": False, "sensitive_paths": [], "risky_shapes": [], "deleted_files": []}
    assert dr.render_risk_checks(clean, gfm_supported=True) == ""
    assert dr.render_risk_checks(clean, gfm_supported=False) == ""


def test_render_risk_checks_findings():
    findings = {
        "has_findings": True,
        "sensitive_paths": [{"file": ".github/workflows/ci.yml", "pattern": ".github/workflows/**"}],
        "risky_shapes": [{
            "category": "destructive_sql",
            "description": "Destructive DDL",
            "file": "db.sql",
            "line": "DROP TABLE",
        }],
        "deleted_files": ["legacy.py"],
    }
    gfm = dr.render_risk_checks(findings, gfm_supported=True)
    plain = dr.render_risk_checks(findings, gfm_supported=False)
    assert "3 finding(s) detected" in gfm
    assert "<tr><td>" in gfm
    assert ".github/workflows/ci.yml" in gfm
    assert "Destructive DDL" in gfm
    assert "legacy.py" in gfm

    assert "3 finding(s) detected" in plain
    assert "### ⚠️ Risk checks:" in plain
    assert ".github/workflows/ci.yml" in plain


def test_revoke_pattern_linear_timing():
    import time
    adversarial_line = "+ REVOKE " + ("word " * 500) + "trailing"
    start = time.perf_counter()
    f = _file("service.sql", f"@@ -1,2 +1,2 @@\n context\n{adversarial_line}")
    dr.scan_file_diff(f)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.2


def test_render_risk_checks_backtick_filename():
    findings = {
        "has_findings": True,
        "sensitive_paths": [{"file": "path/`file`.yml", "pattern": "*.yml"}],
        "risky_shapes": [{
            "category": "destructive_sql",
            "description": "Destructive DDL",
            "file": "db/`schema`.sql",
            "line": "DROP TABLE",
        }],
        "deleted_files": ["old/`deleted`.py"],
    }
    gfm = dr.render_risk_checks(findings, gfm_supported=True)
    plain = dr.render_risk_checks(findings, gfm_supported=False)
    assert "<code>path/`file`.yml</code>" in gfm
    assert "<code>db/`schema`.sql</code>" in gfm
    assert "<code>old/`deleted`.py</code>" in gfm
    assert "<code>&lt;img src=x&gt;</code>" in dr.render_risk_checks(
        {"has_findings": True, "deleted_files": ["<img src=x>"]}, gfm_supported=True
    )
    assert "``path/`file`.yml``" in plain
    assert "``db/`schema`.sql``" in plain
    assert "``old/`deleted`.py``" in plain


def test_render_risk_checks_gfm_newline_filename():
    out = dr.render_risk_checks({"has_findings": True, "deleted_files": ["a\n\n# h\n\nb.tf"]}, gfm_supported=True)
    assert "<code>a\\n\\n# h\\n\\nb.tf</code>" in out
    assert "\n\n" not in out


# 8. PRReviewer and convert_to_markdown_v2 integration
def test_convert_to_markdown_v2_renders_risk_checks():
    data = {
        "review": {
            "risk_checks": {
                "has_findings": True,
                "sensitive_paths": [{"file": "Dockerfile", "pattern": "*Dockerfile*"}],
                "risky_shapes": [],
                "deleted_files": [],
            }
        }
    }
    md = convert_to_markdown_v2(data, gfm_supported=True)
    assert "Risk checks:" in md
    assert "Dockerfile" in md


def test_reviewer_drops_model_supplied_risk_checks_row(risk_settings):
    from unittest.mock import patch

    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = MagicMock()
    reviewer.git_provider.get_diff_files.return_value = []
    reviewer.git_provider.is_supported.return_value = True
    reviewer.git_provider.publish_structured_review = None
    reviewer.pr_url = "https://example/pr/1"
    reviewer.token_handler = MagicMock()
    reviewer.remaining_files_list = []
    reviewer.partial_files_list = []
    reviewer.incremental = SimpleNamespace(is_incremental=False)
    reviewer.vars = {}

    model_output_with_hallucinated_row = (
        "review:\n"
        "  relevant_tests: No\n"
        "  risk_checks:\n"
        "    has_findings: true\n"
        "    hallucinated: true\n"
    )
    reviewer.prediction = model_output_with_hallucinated_row
    reviewer.prediction_data = None
    reviewer.review_chunk_count = 1
    reviewer.review_failed_chunk_count = 0

    with patch("pr_agent.commands.pr_reviewer.github_action_output"):
        markdown = reviewer._prepare_pr_review()

    # The clean git diff had no files, so build_risk_checks was None;
    # hallucinated risk_checks must be dropped
    assert "hallucinated" not in markdown
