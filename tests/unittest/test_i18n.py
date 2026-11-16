import re
import string
import tomllib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from pr_agent.algo import i18n
from pr_agent.algo.comment_identity import PRReviewIdentity, render_hidden_marker
from pr_agent.algo.i18n import locale_chain, t
from pr_agent.algo.review_finding_state import append_review_state, parse_review_state, reconcile_review_findings
from pr_agent.algo.run_details import init_run_details, record_ai_call, record_model_used
from pr_agent.algo.run_output import show_run_details
from pr_agent.algo.utils import convert_to_markdown_v2
from pr_agent.commands.pr_reviewer import PRReviewer
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.bitbucket_provider import BitbucketProvider
from pr_agent.git_providers.codecommit_provider import CodeCommitProvider
from pr_agent.git_providers.git_provider import GitProvider
from tests.unittest._settings_helpers import restore_settings, snapshot_settings


@pytest.fixture
def language():
    snapshot = snapshot_settings(["config.response_language", "pr_reviewer.publish_output_no_suggestions"])

    def set_language(value):
        get_settings().set("config.response_language", value)

    yield set_language
    restore_settings(snapshot)


@pytest.fixture
def catalog_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(i18n, "CATALOG_DIR", tmp_path)
    i18n._shipped_catalogs.cache_clear()
    i18n._load_catalog.cache_clear()
    yield tmp_path
    i18n._shipped_catalogs.cache_clear()
    i18n._load_catalog.cache_clear()


def _placeholders(text):
    return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}


def _flatten(path):
    with open(path, "rb") as file:
        data = tomllib.load(file)
    flat = {}
    for table, entries in data.items():
        assert isinstance(entries, dict), f"{path.name}: [{table}] must be a table"
        for key, text in entries.items():
            assert isinstance(text, str), f"{path.name}: {table}.{key} must be a string"
            flat[f"{table}.{key}"] = text
    return flat


@pytest.mark.parametrize(
    "path", sorted(i18n.CATALOG_DIR.glob("*.toml")), ids=lambda path: path.name)
def test_shipped_catalog_matches_english_keys_and_placeholders(path):
    english = _flatten(i18n.CATALOG_DIR / "en.toml")
    catalog = _flatten(path)
    if path.stem in {"en", "es"}:
        assert set(catalog) <= set(english), f"keys missing from en.toml: {set(catalog) - set(english)}"
    else:
        assert set(catalog) == set(english), f"{path.name}: catalog keys differ from en.toml"
    for key, text in catalog.items():
        assert _placeholders(text) == _placeholders(english[key]), key


@pytest.mark.parametrize(("value", "expected"), [
    ("en-US", ["en"]),
    ("es-AR", ["es", "en"]),
    ("ES_ar", ["es", "en"]),
    (" es ", ["es", "en"]),
    ("pt-BR", ["pt-br", "en"]),
    ("PT_br", ["pt-br", "en"]),
    ("pt-PT-x-private", ["pt-pt", "en"]),
    ("zh_TW", ["zh-tw", "en"]),
    ("AR_sa", ["ar", "en"]),
    ("xx-YY", ["en"]),
    ("Spanish", ["en"]),
    ("../es", ["en"]),
    ("", ["en"]),
    (None, ["en"]),
    (42, ["en"]),
])
def test_locale_chain_drops_subtags_and_ends_in_english(value, expected):
    assert locale_chain(value) == expected


def test_lookup_falls_back_to_english_per_key(catalog_dir, language):
    (catalog_dir / "en.toml").write_text(
        '[demo]\ngreeting = "Hello {name}"\nfarewell = "Bye"\nbroken = "Count {count}"\n', encoding="utf-8")
    (catalog_dir / "es.toml").write_text(
        '[demo]\ngreeting = "Hola {name}"\nbroken = "Cuenta {cantidad}"\n', encoding="utf-8")
    language("es-AR")
    assert t("demo.greeting", name="Ana") == "Hola Ana"
    assert t("demo.farewell") == "Bye"
    assert t("demo.broken", count=3) == "Count 3"
    with pytest.raises(KeyError):
        t("demo.unknown")


def test_unreadable_catalog_falls_back_to_english(catalog_dir, language):
    (catalog_dir / "en.toml").write_text('[demo]\ngreeting = "Hello"\n', encoding="utf-8")
    (catalog_dir / "es.toml").write_text('[demo\ngreeting = "Hola"\n', encoding="utf-8")
    language("es")
    assert t("demo.greeting") == "Hello"


def _review(**fields):
    return {"review": {
        "estimated_effort_to_review_[1-5]": "2",
        "relevant_tests": "No",
        "security_concerns": "No",
        **fields,
    }}


@pytest.mark.parametrize("gfm_supported", [True, False])
@pytest.mark.parametrize(("locale", "labels"), [
    ("es-AR", ("Esfuerzo estimado de revisión", "Sin pruebas relevantes",
               "No se identificaron problemas de seguridad", "Áreas recomendadas para revisar", "Posible problema")),
    ("zh-TW", ("預估審查工作量", "未找到相關測試", "未發現資安疑慮", "建議優先審查的部分", "可能的問題")),
    ("ar", ("الجهد المقدر للمراجعة", "لا توجد اختبارات ذات صلة", "لم تُكتشف مخاوف أمنية",
            "مجالات التركيز الموصى بها للمراجعة", "مشكلة محتملة")),
])
def test_review_summary_renders_labels_in_the_response_language(language, gfm_supported, locale, labels):
    language(locale)
    issue = {"relevant_file": "a.py", "issue_header": "Possible bug", "issue_content": "Leak.",
             "start_line": 1, "end_line": 1}
    output = convert_to_markdown_v2(_review(key_issues_to_review=[issue]), gfm_supported=gfm_supported)

    for label in labels:
        assert label in output
    for label in ("Estimated effort", "No relevant tests", "No security concerns", "Recommended focus",
                  "Possible Issue", "key observations"):
        assert label not in output


def test_review_summary_translates_present_tests_and_security_concerns(language):
    language("es")
    output = convert_to_markdown_v2(_review(relevant_tests="Yes", security_concerns="SQL injection: raw query."))
    assert "El PR contiene pruebas" in output
    assert "Problemas de seguridad" in output
    assert "PR contains tests" not in output
    assert "Security concerns" not in output


def test_review_progress_message_is_translated(language):
    language("es-AR")
    reviewer = PRReviewer.__new__(PRReviewer)
    assert reviewer._review_progress_body("") == "Preparando la revisión..."
    assert reviewer._review_progress_body("x") == "Preparando la revisión... x"


def test_run_details_are_translated(language):
    language("es-AR")
    snapshot = snapshot_settings(["config.output_run_cost"])
    get_settings().set("config.output_run_cost", True)
    try:
        init_run_details()
        record_model_used("openai/backup", is_fallback=True)
        record_ai_call({"prompt_tokens": 1000, "completion_tokens": 20, "total_tokens": 1020})
        output = show_run_details(gfm_supported=True)
    finally:
        restore_settings(snapshot)

    assert "⚙️ Detalles de la ejecución del agente" in output
    assert "- Modelo: openai/backup (respaldo)" in output
    assert "- Tokens: 1,000 de entrada / 20 de salida / 1,020 en total" in output
    assert "- Llamadas a la IA: 1" in output
    assert "Tiempo empleado" in output
    assert "Costo estimado de la API" in output
    for label in ("Agent run details", "Model:", "fallback", " in /", "AI calls", "Time cost", "Estimated API cost"):
        assert label not in output


def test_no_suggestions_check_matches_the_translated_review(language):
    language("es-AR")
    get_settings().set("pr_reviewer.publish_output_no_suggestions", False)
    reviewer = PRReviewer.__new__(PRReviewer)
    without_issues = convert_to_markdown_v2(_review(key_issues_to_review=[]))
    with_issue = convert_to_markdown_v2(_review(key_issues_to_review=[
        {"relevant_file": "a.py", "issue_header": "Leak", "issue_content": "Leak."}]))
    assert reviewer._should_publish_review_no_suggestions(without_issues) is False
    assert reviewer._should_publish_review_no_suggestions(with_issue) is True


def test_finding_identity_text_stays_english(language):
    """Finding bodies feed the state and inline fingerprints, so a language change must not rename them."""
    language("es-AR")
    issue = {"relevant_file": "a.py", "issue_header": "Possible bug", "issue_content": "Leak.",
             "start_line": 1, "end_line": 1}
    finding = PRReviewer._review_finding_from_issue(issue)
    assert finding.rendered_body == "**Possible Issue**\n\nLeak."
    diff_files = {"a.py": SimpleNamespace(filename="a.py", head_file="x = 1\n")}
    comment = PRReviewer._build_key_issue_comment(
        PRReviewer.__new__(PRReviewer), finding, diff_files
    )
    assert comment["body"] == "**Possible Issue**\n\nLeak."


def test_resolved_findings_section_is_translated_and_state_marker_survives(language):
    language("es-AR")
    finding = {"path": "a.py", "body": "**Possible Issue**\n\nLeak.", "line_start": 3, "line_end": 3}
    active = reconcile_review_findings(None, [finding], allow_resolution=True, head_sha="h1").state
    state = reconcile_review_findings(active, [], allow_resolution=True, head_sha="h2").state
    body = append_review_state("## Guía\n\nbody", state)
    visible = body.split("<!-- pr-agent-review-state", 1)[0]
    assert "<summary>✅ Hallazgos resueltos</summary>" in visible
    assert "**Posible problema**\n\nLeak." in visible
    assert "Possible Issue" not in visible
    parsed = parse_review_state(body)
    assert parsed is not None and parsed.valid
    assert parsed.state["findings"][0]["body"] == "**Possible Issue**\n\nLeak."


def test_persistent_update_header_is_translated_within_the_reserved_budget(language):
    language("es-AR")
    provider = BitbucketProvider.__new__(BitbucketProvider)
    provider.max_comment_length = 1000
    comment = SimpleNamespace(body=f"## Guía\n\n{PRReviewIdentity.REGULAR.value}\n\nold")
    provider.get_issue_comments = MagicMock(return_value=[comment])
    provider.get_issue_comments_newest_first = MagicMock(return_value=[comment])
    provider.edit_comment = MagicMock(return_value=True)
    provider.publish_comment = MagicMock(return_value=SimpleNamespace(body="notice"))
    provider.get_latest_commit_url = MagicMock(return_value="https://example.com/commit/123")
    provider.get_comment_url = MagicMock(return_value="https://example.com/comment/1")

    GitProvider.publish_persistent_comment_full(
        provider, "## Guía\n\nnew", initial_header="## Guía",
        identity_marker=PRReviewIdentity.REGULAR.value, final_update_message=True,
    )

    body = provider.edit_comment.call_args.args[1]
    header = re.search(r"#### \(.*\)\n", body).group(0)
    assert header == "#### (Revisión actualizada hasta el commit https://example.com/commit/123)\n"
    notice = provider.publish_comment.call_args.args[0]
    assert notice == ("**[Revisión persistente](https://example.com/comment/1)** "
                      "actualizada al último commit https://example.com/commit/123")

    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = provider
    marker = render_hidden_marker(PRReviewIdentity.REGULAR.value, provider)
    assert reviewer._review_comment_max_chars() == 1000 - len(f"\n\n{header}") - len(marker) - 2


def test_codecommit_persistent_header_is_translated(language):
    language("es-AR")
    marker = PRReviewIdentity.REGULAR.value
    body = CodeCommitProvider._persistent_body_for_target(
        f"## Guía\n\n{marker}\n\nbody", True, "review", marker, {"source_commit": "abc123"})
    assert f"{marker}\n\n#### (Revisión actualizada hasta el commit abc123)\n" in body
