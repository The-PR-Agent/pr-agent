import yaml

from pr_agent.config_loader import get_settings


def test_add_docs_example_uses_singular_relevant_line():
    prompt = get_settings().pr_add_docs_prompt.system
    example_section = prompt.partition("Example output:")[2]
    example = example_section.partition("```yaml")[2].partition("```")[0]

    documentation = yaml.safe_load(example)["Code Documentation"][0]

    assert documentation["relevant line"] == 12
    assert "relevant lines" not in documentation
