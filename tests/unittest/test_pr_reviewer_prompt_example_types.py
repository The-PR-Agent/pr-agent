"""The reviewer prompt's examples must agree with the types its schema declares.

Named for the example TYPES, not `prompt_contract`: that name is taken by the
Jinja-variable contract tests beside this file, which answer a different
question about the same prompt.

Issue #3354. The prompts describe the model's output as `class X(BaseModel)`
blocks that exist nowhere as Python, so nothing checks that the schema, the
example and the parser agree — and two fields had drifted in opposite
directions:

    estimated_effort_to_review_[1-5]: int   schema
    estimated_effort_to_review_[1-5]: |     example — a block scalar, i.e. a str
      3
    score: str                              schema
    score: 89                               example — a bare int

Same shape as #3319/#3325 in `pr_add_docs.toml`, and the same test shape:
parse the example the model is shown and hold it to the declared type. This
covers ONE file; #3354's full ask is a real pydantic model per top-level class
with a fixture per tool, which is a larger piece of work.

Both example blocks are checked, not just the first: they drifted identically,
so a test that read one would go green while the other still taught the wrong
type.
"""

import re

import yaml

from pr_agent.config_loader import get_settings

# The declared type can be `int`, `List[Foo]` or `Union[List[Foo], str]`, and
# `Field(` may be called positionally, so match up to the `= Field(`.
_DECLARED = re.compile(
    r"^\s*(?P<field>[\w\[\]\-]+):\s*(?P<type>[^=]+?)\s*=\s*Field\(", re.M
)


def _prompt() -> str:
    """Both halves. The schema and the first example live in `system`; the
    SECOND example lives in `user`, and they drifted identically -- a test that
    read only one would go green while the other still taught the wrong type."""
    prompt = get_settings().pr_review_prompt
    return f"{prompt.system}\n{prompt.user}"


def _declared_types() -> dict[str, str]:
    """field name -> the type its `Field(...)` line declares."""
    return {m.group("field"): m.group("type") for m in _DECLARED.finditer(_prompt())}


def _example_blocks() -> list[str]:
    """Every ```yaml example in the prompt, Jinja lines stripped.

    The blocks are Jinja-conditional, so the raw text is not valid YAML; the
    `{%- ... %}` lines are control flow around the fields, never part of one.
    """
    blocks = re.findall(r"```yaml\n(.*?)```", _prompt(), re.S)
    return [
        "\n".join(line for line in b.splitlines() if not line.strip().startswith("{%"))
        for b in blocks
    ]


def _review_examples() -> list[dict]:
    out = []
    for block in _example_blocks():
        parsed = yaml.safe_load(block)
        if isinstance(parsed, dict) and "review" in parsed:
            out.append(parsed["review"])
    return out


def test_the_prompt_still_carries_the_example_blocks_this_pins():
    """The control: if the prompt stops shipping examples, every assertion
    below would pass by having nothing to check."""
    examples = _review_examples()
    assert len(examples) == 2, f"expected both review examples, got {len(examples)}"


def test_the_effort_example_is_the_int_its_schema_declares():
    assert _declared_types()["estimated_effort_to_review_[1-5]"] == "int"
    for example in _review_examples():
        value = example["estimated_effort_to_review_[1-5]"]
        assert isinstance(value, int), (
            f"the schema declares int; the example teaches {type(value).__name__} "
            f"({value!r}) — a block scalar is a str"
        )


def test_the_score_example_is_the_int_its_schema_declares():
    assert _declared_types()["score"] == "int"
    for example in _review_examples():
        value = example["score"]
        assert isinstance(value, int), (
            f"the schema declares int; the example teaches {type(value).__name__} "
            f"({value!r})"
        )


def _taught_fields(value) -> set[str]:
    """Every key an example teaches, at any depth."""
    if isinstance(value, dict):
        return set(value) | {f for v in value.values() for f in _taught_fields(v)}
    if isinstance(value, list):
        return {f for v in value for f in _taught_fields(v)}
    return set()


def test_every_field_the_examples_teach_is_one_the_schema_declares():
    """The drift that started #3354 was a field the examples kept after the
    schema dropped it (`overall_compliance_level`, #3318). It sat under
    `ticket_compliance_check`, so walk nested mappings and lists too."""
    declared = set(_declared_types())
    for example in _review_examples():
        unknown = sorted(_taught_fields(example) - declared)
        assert not unknown, f"examples teach fields the schema does not declare: {unknown}"
