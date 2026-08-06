"""Unit tests for the agent skills loader."""
import os
import textwrap
from pathlib import Path

from jinja2 import Environment, StrictUndefined

from pr_agent.algo.skills_loader import (Skill, _parse_skill_file,
                                         discover_skills,
                                         format_skills_context,
                                         get_skills_context)


def _write_skill(directory: Path, name: str, body: str = "Body content."):
    skill_dir = directory / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(textwrap.dedent(f"""\
        ---
        name: {name}
        description: Use when reviewing {name} code.
        ---

        {body}
        """))
    return skill_file


class TestParseSkillFile:
    def test_parses_valid_frontmatter_and_body(self, tmp_path):
        skill_file = _write_skill(tmp_path, "terraform-standards",
                                  body="# Terraform Review\n- check tags")
        skill = _parse_skill_file(str(skill_file))
        assert skill is not None
        assert skill.name == "terraform-standards"
        assert skill.description == "Use when reviewing terraform-standards code."
        assert "Terraform Review" in skill.body
        assert "- check tags" in skill.body

    def test_missing_opening_delimiter_returns_none(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text("no frontmatter here\nname: x\n")
        assert _parse_skill_file(str(f)) is None

    def test_missing_closing_delimiter_returns_none(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: x\ndescription: y\nstill in frontmatter\n")
        assert _parse_skill_file(str(f)) is None

    def test_invalid_yaml_returns_none(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: [unclosed\n---\nbody\n")
        assert _parse_skill_file(str(f)) is None

    def test_missing_required_fields_returns_none(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: only-name\n---\nbody\n")
        assert _parse_skill_file(str(f)) is None

        f2 = tmp_path / "SKILL2.md"
        f2.write_text("---\ndescription: only desc\n---\nbody\n")
        assert _parse_skill_file(str(f2)) is None

    def test_body_with_inner_dashes_preserved(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text(textwrap.dedent("""\
            ---
            name: with-dashes
            description: Use when X.
            ---

            # Heading
            ---
            section after rule
            """))
        skill = _parse_skill_file(str(f))
        assert skill is not None
        assert "section after rule" in skill.body
        assert "---" in skill.body


class TestDiscoverSkills:
    def test_finds_nested_skill_md_files(self, tmp_path):
        _write_skill(tmp_path / "a", "alpha")
        _write_skill(tmp_path / "b" / "nested", "bravo")
        (tmp_path / "c").mkdir()
        (tmp_path / "c" / "README.md").write_text("not a skill")

        skills = discover_skills([str(tmp_path)])
        names = {s.name for s in skills}
        assert names == {"alpha", "bravo"}

    def test_skips_missing_paths_without_raising(self, tmp_path):
        skills = discover_skills([str(tmp_path / "does-not-exist")])
        assert skills == []

    def test_accepts_direct_path_to_skill_file(self, tmp_path):
        skill_file = _write_skill(tmp_path, "direct")
        skills = discover_skills([str(skill_file)])
        assert len(skills) == 1
        assert skills[0].name == "direct"

    def test_deduplicates_overlapping_paths(self, tmp_path):
        _write_skill(tmp_path / "x", "xray")
        skills = discover_skills([str(tmp_path), str(tmp_path / "x")])
        assert len(skills) == 1

    def test_skips_malformed_files_but_returns_others(self, tmp_path):
        _write_skill(tmp_path / "good", "good")
        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        (bad_dir / "SKILL.md").write_text("no frontmatter\n")
        skills = discover_skills([str(tmp_path)])
        names = [s.name for s in skills]
        assert names == ["good"]

    def test_ignores_empty_and_non_string_path_entries(self, tmp_path):
        _write_skill(tmp_path, "only")
        skills = discover_skills([str(tmp_path), "", None])  # type: ignore[list-item]
        assert len(skills) == 1


class TestFormatSkillsContext:
    def _mk(self, name: str, body: str = "guidance body") -> Skill:
        return Skill(name=name, description=f"Use when {name}", body=body)

    def test_returns_empty_when_no_skills(self):
        assert format_skills_context([], 4000) == ""

    def test_returns_empty_when_budget_zero(self):
        assert format_skills_context([self._mk("a")], 0) == ""

    def test_includes_name_description_and_body(self):
        out = format_skills_context([self._mk("alpha", body="step one\nstep two")], 4000)
        assert "Skill: alpha" in out
        assert "When to use: Use when alpha" in out
        assert "step one" in out
        assert "step two" in out

    def test_drops_skills_beyond_budget(self):
        skills = [self._mk(f"s{i}", body="x " * 500) for i in range(5)]
        out = format_skills_context(skills, max_tokens=300)
        assert "Skill: s0" in out
        assert "Skill: s4" not in out

    def test_truncates_when_first_skill_exceeds_budget(self):
        huge = self._mk("huge", body="y " * 5000)
        out = format_skills_context([huge], max_tokens=50)
        assert "[truncated]" in out

    def test_separator_between_multiple_skills(self):
        out = format_skills_context(
            [self._mk("a", body="A"), self._mk("b", body="B")], max_tokens=4000
        )
        assert out.count("---") >= 1
        assert out.index("Skill: a") < out.index("Skill: b")


class TestGetSkillsContext:
    def test_disabled_returns_empty(self, tmp_path, monkeypatch):
        from pr_agent.config_loader import get_settings
        get_settings().set("skills", {"enabled": False, "paths": [str(tmp_path)],
                                       "max_skills_tokens": 4000})
        assert get_skills_context() == ""

    def test_enabled_with_no_paths_returns_empty(self, monkeypatch):
        from pr_agent.config_loader import get_settings
        get_settings().set("skills", {"enabled": True, "paths": [],
                                       "max_skills_tokens": 4000})
        assert get_skills_context() == ""

    def test_enabled_with_skills_returns_formatted(self, tmp_path):
        _write_skill(tmp_path, "demo", body="check the thing")
        from pr_agent.config_loader import get_settings
        get_settings().set("skills", {"enabled": True, "paths": [str(tmp_path)],
                                       "max_skills_tokens": 4000})
        out = get_skills_context()
        assert "Skill: demo" in out
        assert "check the thing" in out

    def test_invalid_max_tokens_falls_back_to_default(self, tmp_path):
        _write_skill(tmp_path, "demo", body="check the thing")
        from pr_agent.config_loader import get_settings
        get_settings().set("skills", {"enabled": True, "paths": [str(tmp_path)],
                                       "max_skills_tokens": "not-a-number"})
        # Should not raise; should still produce skills_context using the default budget.
        out = get_skills_context()
        assert "Skill: demo" in out


class TestJinjaSafety:
    """Skills bodies often contain {{ }} or {% %} (Helm/Ansible/Terraform).

    Confirm that Jinja2 substitution is single-pass: the rendered template
    contains the literal characters from the substituted variable, not a
    re-evaluation of them.
    """

    def test_jinja_syntax_in_skill_body_renders_as_literal(self, tmp_path):
        body = "Use {{ unknown_var }} and {% if foo %}bar{% endif %} here."
        _write_skill(tmp_path, "helm", body=body)
        skills = discover_skills([str(tmp_path)])
        out = format_skills_context(skills, max_tokens=4000)

        # Mirror the prompt-template injection site: a guarded {{ skills_context }}.
        # autoescape is enabled here so the test doesn't rely on Jinja's insecure
        # default; the property under test (a substituted value is never re-parsed
        # as a template) holds regardless of the autoescape setting.
        template = "before\n{%- if skills_context %}{{ skills_context }}{% endif %}\nafter"
        env = Environment(undefined=StrictUndefined, autoescape=True)
        rendered = env.from_string(template).render(skills_context=out)

        assert "{{ unknown_var }}" in rendered
        assert "{% if foo %}" in rendered


class TestPathExpansion:
    def test_env_var_in_path_is_expanded(self, tmp_path, monkeypatch):
        _write_skill(tmp_path, "envtest")
        monkeypatch.setenv("SKILLS_TEST_DIR", str(tmp_path))
        skills = discover_skills(["$SKILLS_TEST_DIR"])
        assert [s.name for s in skills] == ["envtest"]

    def test_tilde_in_path_is_expanded(self, tmp_path, monkeypatch):
        _write_skill(tmp_path, "homestest")
        monkeypatch.setenv("HOME", str(tmp_path))
        skills = discover_skills(["~"])
        assert [s.name for s in skills] == ["homestest"]


class TestResourceGathering:
    def test_sibling_md_file_is_inlined_as_resource(self, tmp_path):
        _write_skill(tmp_path, "withrefs", body="main body")
        skill_dir = tmp_path / "withrefs"
        (skill_dir / "examples.md").write_text("# Examples\n- one\n- two\n")

        skills = discover_skills([str(tmp_path)])
        assert len(skills) == 1
        names = [r.relative_path for r in skills[0].resources]
        assert names == ["examples.md"]
        assert "- one" in skills[0].resources[0].content

    def test_references_subdirectory_is_inlined(self, tmp_path):
        _write_skill(tmp_path, "withdir")
        refs = tmp_path / "withdir" / "references"
        refs.mkdir()
        (refs / "guide.md").write_text("guide content")
        (refs / "deep" / "nested").mkdir(parents=True)
        (refs / "deep" / "nested" / "more.md").write_text("deeper content")

        skills = discover_skills([str(tmp_path)])
        rels = sorted(r.relative_path for r in skills[0].resources)
        # Use os.sep-agnostic comparison
        rels_normalised = [r.replace(os.sep, "/") for r in rels]
        assert rels_normalised == ["references/deep/nested/more.md", "references/guide.md"]

    def test_scripts_and_assets_directories_are_excluded(self, tmp_path):
        _write_skill(tmp_path, "secure")
        skill_dir = tmp_path / "secure"
        (skill_dir / "scripts").mkdir()
        (skill_dir / "scripts" / "run.py").write_text("print('hi')")
        (skill_dir / "scripts" / "notes.md").write_text("script notes (should be excluded)")
        (skill_dir / "assets").mkdir()
        (skill_dir / "assets" / "data.md").write_text("asset data (should be excluded)")
        (skill_dir / "assets" / "img.svg").write_text("<svg/>")

        skills = discover_skills([str(tmp_path)])
        rels = [r.relative_path for r in skills[0].resources]
        assert rels == []

    def test_nested_skill_directory_is_treated_independently(self, tmp_path):
        _write_skill(tmp_path, "outer", body="outer body")
        # Nested skill inside the outer skill's directory.
        inner_dir = tmp_path / "outer" / "inner"
        inner_dir.mkdir()
        (inner_dir / "SKILL.md").write_text(textwrap.dedent("""\
            ---
            name: inner
            description: Use when inner.
            ---

            inner body
            """))
        (inner_dir / "extra.md").write_text("extra inner content")

        skills = discover_skills([str(tmp_path)])
        by_name = {s.name: s for s in skills}
        assert set(by_name) == {"inner", "outer"}
        # outer must not absorb the inner skill's files
        outer_rels = [r.relative_path for r in by_name["outer"].resources]
        assert outer_rels == []
        # inner picks up only its own sibling
        inner_rels = [r.relative_path for r in by_name["inner"].resources]
        assert inner_rels == ["extra.md"]

    def _apply_repo_skills_toml(self, monkeypatch, repo_toml: bytes):
        from pr_agent.config_loader import get_settings
        from pr_agent.git_providers import utils as gp_utils

        get_settings().unset("skills")
        get_settings().set("skills", {"enabled": False, "paths": [],
                                       "max_skills_tokens": 8000})
        get_settings().config.use_repo_settings_file = True

        class FakeGitProvider:
            def __init__(self, *a, **kw):
                pass

            def get_repo_settings(self):
                return repo_toml

        monkeypatch.setattr(gp_utils, "get_git_provider_with_context",
                            lambda _url: FakeGitProvider())
        gp_utils.apply_repo_settings("https://example.com/owner/repo/pull/1")
        return get_settings()

    def test_repo_settings_cannot_override_skills_paths(self, monkeypatch):
        """A malicious repo's .pr_agent.toml must not be able to set skills.paths —
        that points at the host filesystem and would allow host-file exfiltration
        to the LLM. The rejected key must not sneak in alongside allowed ones.
        """
        repo_toml = b'[skills]\nenabled = true\npaths = ["/etc/pwned"]\n'
        settings = self._apply_repo_skills_toml(monkeypatch, repo_toml)

        assert "/etc/pwned" not in list(settings.skills.paths), \
            "Repo settings must not be able to inject skills.paths"

    def test_repo_settings_can_override_safe_skills_keys(self, monkeypatch):
        """Safe per-repo preferences (enabled, max_skills_tokens) may be set from a
        repo's .pr_agent.toml; only the host-only skills.paths is refused.
        """
        repo_toml = b'[skills]\nenabled = true\nmax_skills_tokens = 1234\n'
        settings = self._apply_repo_skills_toml(monkeypatch, repo_toml)

        assert settings.skills.enabled is True, \
            "Repo settings should be able to toggle skills.enabled"
        assert int(settings.skills.max_skills_tokens) == 1234, \
            "Repo settings should be able to set skills.max_skills_tokens"

    def test_format_skills_context_includes_resource_content(self, tmp_path):
        _write_skill(tmp_path, "doc")
        (tmp_path / "doc" / "checklist.md").write_text("- item one\n- item two")
        skills = discover_skills([str(tmp_path)])
        out = format_skills_context(skills, max_tokens=4000)
        assert "#### checklist.md" in out
        assert "- item one" in out

    def test_non_utf8_skill_md_is_skipped_without_crashing(self, tmp_path):
        bad = tmp_path / "broken"
        bad.mkdir()
        (bad / "SKILL.md").write_bytes(b"---\nname: x\ndescription: y\n---\n\n\xff\xfe invalid utf-8")
        _write_skill(tmp_path, "good")
        skills = discover_skills([str(tmp_path)])
        assert [s.name for s in skills] == ["good"]

    def test_non_utf8_resource_file_is_skipped_without_crashing(self, tmp_path):
        _write_skill(tmp_path, "mixed")
        (tmp_path / "mixed" / "good.md").write_text("readable content")
        (tmp_path / "mixed" / "bad.md").write_bytes(b"\xff\xfe binary garbage")
        skills = discover_skills([str(tmp_path)])
        rels = [r.relative_path for r in skills[0].resources]
        assert "good.md" in rels
        assert "bad.md" not in rels

    def test_oversized_resource_file_is_skipped(self, tmp_path, caplog):
        _write_skill(tmp_path, "huge-res")
        huge = tmp_path / "huge-res" / "huge.md"
        huge.write_text("a" * (300 * 1024))  # 300 KB, above the 256 KB cap
        (tmp_path / "huge-res" / "fine.md").write_text("small content")

        skills = discover_skills([str(tmp_path)])
        rels = [r.relative_path for r in skills[0].resources]
        assert "fine.md" in rels
        assert "huge.md" not in rels

    def test_huge_resource_is_dropped_when_skill_already_consumed_budget(self, tmp_path):
        # Two skills; the second has a huge resource. Budget fits skill 1 plus
        # SKILL.md of skill 2 only — so skill 2 is dropped entirely (not partially).
        _write_skill(tmp_path, "first", body="first body")
        _write_skill(tmp_path, "second", body="second body")
        (tmp_path / "second" / "huge.md").write_text("z" * 50_000)

        skills = discover_skills([str(tmp_path)])
        out = format_skills_context(skills, max_tokens=200)  # 800-char budget
        assert "Skill: first" in out
        assert "Skill: second" not in out
