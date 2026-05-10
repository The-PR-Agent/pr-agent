"""Unit tests for the agent skills loader."""
import os
import textwrap
from pathlib import Path

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
        # Directory without SKILL.md should be ignored
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
        return Skill(name=name, description=f"Use when {name}", body=body, path=f"/{name}/SKILL.md")

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
        big_body = "x" * 1000
        skills = [self._mk(f"s{i}", body=big_body) for i in range(5)]
        # Budget of 250 tokens ~ 1000 chars, fits roughly one skill.
        out = format_skills_context(skills, max_tokens=250)
        assert "Skill: s0" in out
        # At least one of the later skills must be dropped.
        assert "Skill: s4" not in out

    def test_truncates_when_first_skill_exceeds_budget(self):
        huge = self._mk("huge", body="y" * 5000)
        out = format_skills_context([huge], max_tokens=50)  # 200 char budget
        assert "[truncated]" in out
        assert len(out) <= 250  # budget + truncation marker

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
