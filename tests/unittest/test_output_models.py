import re
from pathlib import Path
from types import UnionType
from typing import get_args, get_origin

import pytest
from pydantic import StrictInt

from pr_agent.algo.output_models import (
    CodeDocumentation,
    CodeSuggestion,
    CodeSuggestionFeedback,
    ContributionTimeCostEstimate,
    DocHeadingsHelper,
    DocHelper,
    FileDescription,
    FileIdxAndPath,
    KeyIssuesComponentLink,
    Label,
    Labels,
    PRCodeSuggestions,
    PRCodeSuggestionsFeedback,
    PRDescription,
    PRDescriptionHeaders,
    PRFilesWalkthrough,
    PRRankResponses,
    PRReview,
    PRType,
    RelevantSection,
    Review,
    SubPR,
    TicketCompliance,
    TodoSection,
)
from pr_agent.algo.utils import load_yaml


def _review_fixture():
    issue = {
        "relevant_file": "src/app.py",
        "issue_header": "Possible Bug",
        "issue_content": "The error path can lose the original exception.",
        "start_line": 10,
        "end_line": 12,
    }
    return {
        "review": {
            "estimated_effort_to_review_[1-5]": 3,
            "risk_level": "medium",
            "merge_recommendation": "merge_with_caution",
            "review_priority_files": ["src/app.py"],
            "contribution_time_cost_estimate": {"best_case": "45m", "average_case": "2h", "worst_case": "5h"},
            "score": 89,
            "relevant_tests": "Yes",
            "insights_from_user_answers": "The deployment target is Linux.",
            "key_issues_to_review": [issue],
            "security_concerns": "No",
            "todo_sections": [{"relevant_file": "src/app.py", "line_number": 20, "content": "Remove fallback."}],
            "can_be_split": [{"relevant_files": ["src/app.py"], "title": "Improve error handling"}],
            "ticket_compliance_check": [{
                "ticket_url": "#123",
                "ticket_requirements": "Handle errors.",
                "fully_compliant_requirements": "Handle errors.",
                "not_compliant_requirements": "",
                "requires_further_human_verification": "",
            }],
        }
    }


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (SubPR, {"relevant_files": ["src/app.py"], "title": "Improve error handling"}),
        (KeyIssuesComponentLink, _review_fixture()["review"]["key_issues_to_review"][0]),
        (TodoSection, {"relevant_file": "src/app.py", "line_number": 20, "content": "Remove fallback."}),
        (TicketCompliance, _review_fixture()["review"]["ticket_compliance_check"][0]),
        (ContributionTimeCostEstimate, {"best_case": "45m", "average_case": "2h", "worst_case": "5h"}),
        (Review, _review_fixture()["review"]),
        (PRReview, _review_fixture()),
        (CodeSuggestion, {
            "relevant_file": "src/app.py", "language": "python", "existing_code": "return value",
            "suggestion_content": "Handle the missing value.", "improved_code": "return value or default",
            "one_sentence_summary": "Handle missing values", "label": "possible bug",
        }),
        (PRCodeSuggestions, {"code_suggestions": [{
            "relevant_file": "src/app.py", "language": "python", "existing_code": "return value",
            "suggestion_content": "Handle the missing value.", "improved_code": "return value or default",
            "one_sentence_summary": "Handle missing values", "label": "possible bug",
        }]}),
        (PRCodeSuggestionsFeedback, {"code_suggestions": [{
            "suggestion_summary": "Handle missing values", "relevant_file": "src/app.py",
            "relevant_lines_start": 10, "relevant_lines_end": 10, "suggestion_score": 8,
            "why": "The change prevents a runtime failure.",
        }]}),
        (PRDescription, {"type": ["Bug fix"], "description": "Fix a runtime failure.", "title": "Fix runtime failure",
                         "changes_diagram": "flowchart LR\nA --> B", "pr_files": [{
                             "filename": "src/app.py", "changes_summary": "- Handle failures",
                             "changes_title": "Handle runtime failures", "label": "bug fix",
                         }]}),
        (PRDescriptionHeaders, {"type": ["Tests"], "description": "Cover the new behavior.",
                                "title": "Describe the test changes", "changes_diagram": ""}),
        (FileDescription, {"filename": "src/app.py", "changes_summary": "- Handle failures",
                           "changes_title": "Handle runtime failures", "label": "bug fix"}),
        (PRFilesWalkthrough, {"pr_files": [{
            "filename": "src/app.py", "changes_summary": "- Handle failures",
            "changes_title": "Handle runtime failures", "label": "bug fix",
        }]}),
        (Labels, {"labels": ["Bug fix", "Tests"]}),
        (CodeDocumentation, {"Code Documentation": [{
            "relevant file": "src/app.py", "relevant line": 12, "doc placement": "after",
            "documentation": "Document the handler.",
        }]}),
        (PRRankResponses, {"which_response_was_better": 1, "why": "It is clearer.", "score_response1": 9, "score_response2": 7}),
        (DocHelper, {"user_question": "How?", "response": "Use the helper.", "relevant_sections": [{
            "file_name": "docs/guide.md", "relevant_section_header_string": "## Usage",
        }], "question_is_relevant": 1}),
        (DocHeadingsHelper, {"user_question": "How?", "relevant_files_ranking": [{"idx": 0, "file_name": "docs/guide.md"}]}),
    ],
)
def test_output_models_validate_complete_fixtures(model, payload):
    model.model_validate(payload)


def test_review_alias_accepts_prompt_field_name():
    assert Review.model_validate({"key_issues_to_review": [], "estimated_effort_to_review_[1-5]": 3}).estimated_effort_to_review == 3
    with pytest.raises(ValueError):
        Review.model_validate({"key_issues_to_review": [], "estimated_effort_to_review": 3})


def test_review_rejects_coercible_numeric_types_and_strips_prompt_literals():
    with pytest.raises(ValueError):
        Review.model_validate({"key_issues_to_review": [], "score": "89"})
    with pytest.raises(ValueError):
        Review.model_validate({"key_issues_to_review": [], "score": True})

    review = Review.model_validate({
        "key_issues_to_review": [],
        "risk_level": "low\n",
        "merge_recommendation": "safe_to_merge\n",
        "relevant_tests": "No\n",
    })
    assert review.risk_level == "low"
    assert review.merge_recommendation == "safe_to_merge"
    assert review.relevant_tests == "No"


def test_required_label_and_list_constraints_are_enforced():
    suggestion = {
        "relevant_file": "src/app.py", "language": "python", "existing_code": "return value",
        "suggestion_content": "Handle the missing value.", "improved_code": "return value or default",
        "one_sentence_summary": "Handle missing values",
    }
    with pytest.raises(ValueError):
        CodeSuggestion.model_validate(suggestion)
    with pytest.raises(ValueError):
        PRRankResponses.model_validate({"which_response_was_better": 3, "why": "No", "score_response1": 1, "score_response2": 1})
    with pytest.raises(ValueError):
        Review.model_validate({"key_issues_to_review": [], "can_be_split": [{"relevant_files": [], "title": "x"}] * 4})
    with pytest.raises(ValueError):
        PRDescription.model_validate({"type": ["Tests"], "title": "x", "pr_files": [{
            "filename": "x", "changes_title": "x", "label": "x",
        }] * 21})
    with pytest.raises(ValueError):
        Review.model_validate({"key_issues_to_review": [], "estimated_effort_to_review_[1-5]": 0})
    with pytest.raises(ValueError):
        Review.model_validate({"key_issues_to_review": [], "score": 101})
    with pytest.raises(ValueError):
        PRCodeSuggestionsFeedback.model_validate({"code_suggestions": [{
            "suggestion_summary": "x", "relevant_file": "x", "relevant_lines_start": 1,
            "relevant_lines_end": 1, "suggestion_score": 11, "why": "x",
        }]})
    with pytest.raises(ValueError):
        PRRankResponses.model_validate({"which_response_was_better": 1, "why": "x", "score_response1": 0, "score_response2": 11})
    with pytest.raises(ValueError):
        DocHelper.model_validate({"user_question": "x", "response": "x", "relevant_sections": [], "question_is_relevant": 2})
    with pytest.raises(ValueError):
        DocHeadingsHelper.model_validate({"user_question": "x", "relevant_files_ranking": [{"idx": -1, "file_name": "x"}]})
    with pytest.raises(ValueError):
        Review.model_validate({"key_issues_to_review": [], "risk_level": "critical"})
    with pytest.raises(ValueError):
        Review.model_validate({"key_issues_to_review": [], "merge_recommendation": "approve"})
    with pytest.raises(ValueError):
        Review.model_validate({"key_issues_to_review": [], "relevant_tests": "Maybe"})
    with pytest.raises(ValueError):
        PRDescription.model_validate({"type": [], "title": "x"})
    with pytest.raises(ValueError):
        PRDescriptionHeaders.model_validate({"type": [], "title": "x"})
    with pytest.raises(ValueError):
        Review.model_validate({"key_issues_to_review": [{
            "relevant_file": "x", "issue_header": "x", "issue_content": "x",
            "start_line": 1, "end_line": 1, "unexpected": "x",
        }]})


def _split_type_args(value):
    parts, depth, start = [], 0, 0
    for index, character in enumerate(value):
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
        elif character == "," and depth == 0:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _prompt_type_signature(annotation):
    annotation = annotation.strip().replace(" ", "")
    if annotation.startswith("Optional["):
        annotation = annotation[9:-1]
    if annotation.startswith("Union["):
        return ("union", tuple(sorted(
            (_prompt_type_signature(item) for item in _split_type_args(annotation[6:-1])), key=repr
        )))
    if annotation.startswith("List["):
        return ("list", _prompt_type_signature(annotation[5:-1]))
    if annotation.startswith("Literal["):
        values = tuple(value.strip().strip('"').strip("'") for value in annotation[8:-1].split(","))
        return ("literal", values)
    if annotation == "Label":
        return "str"
    if annotation == "relevant_section":
        return "RelevantSection"
    if annotation == "file_idx_and_path":
        return "FileIdxAndPath"
    return annotation


def _model_type_signature(annotation):
    origin = get_origin(annotation)
    if str(origin) == "typing.Annotated":
        return _model_type_signature(get_args(annotation)[0])
    if annotation is StrictInt:
        return "int"
    if str(origin) == "typing.Literal":
        return ("literal", tuple(str(value) for value in get_args(annotation)))
    if origin in (list,):
        return ("list", _model_type_signature(get_args(annotation)[0]))
    if origin in (UnionType,):
        args = get_args(annotation)
    elif str(origin) == "typing.Union":
        args = get_args(annotation)
    else:
        return getattr(annotation, "__name__", str(annotation).split(".")[-1])
    members = tuple(sorted(
        (_model_type_signature(item) for item in args if item is not type(None)), key=repr
    ))
    return members[0] if len(members) == 1 else ("union", members)


PROMPT_MODELS = {
    "pr_reviewer_prompts.toml": {"SubPR": SubPR, "KeyIssuesComponentLink": KeyIssuesComponentLink,
                                  "TodoSection": TodoSection, "TicketCompliance": TicketCompliance,
                                  "ContributionTimeCostEstimate": ContributionTimeCostEstimate, "Review": Review,
                                  "PRReview": PRReview},
    "pr_description_prompts.toml": {"FileDescription": FileDescription, "PRDescription": PRDescription},
    "pr_description_only_description_prompts.toml": {"PRDescriptionHeaders": PRDescriptionHeaders},
    "pr_description_only_files_prompts.toml": {"FileDescription": FileDescription, "PRFilesWalkthrough": PRFilesWalkthrough},
    "pr_custom_labels.toml": {"Labels": Labels},
    "pr_evaluate_prompt_response.toml": {"PRRankResponses": PRRankResponses},
    "pr_help_prompts.toml": {"relevant_section": RelevantSection, "DocHelper": DocHelper},
    "pr_help_docs_prompts.toml": {"relevant_section": RelevantSection, "DocHelper": DocHelper},
    "pr_help_docs_headings_prompts.toml": {"file_idx_and_path": FileIdxAndPath, "DocHeadingsHelper": DocHeadingsHelper},
    "code_suggestions/pr_code_suggestions_prompts.toml": {"CodeSuggestion": CodeSuggestion, "PRCodeSuggestions": PRCodeSuggestions},
    "code_suggestions/pr_code_suggestions_prompts_not_decoupled.toml": {"CodeSuggestion": CodeSuggestion, "PRCodeSuggestions": PRCodeSuggestions},
    "code_suggestions/pr_code_suggestions_reflect_prompts.toml": {"CodeSuggestionFeedback": CodeSuggestionFeedback, "PRCodeSuggestionsFeedback": PRCodeSuggestionsFeedback},
}


def test_prompt_fields_are_present_in_output_models():
    root = Path(__file__).parents[2] / "pr_agent" / "settings"
    for relative_path, classes in PROMPT_MODELS.items():
        text = (root / relative_path).read_text(encoding="utf-8")
        for class_name, model in classes.items():
            class_start = text.index(f"class {class_name}(BaseModel):")
            block_start = class_start + len(f"class {class_name}(BaseModel):")
            next_class = text.find("\nclass ", block_start)
            separator = text.find("=====", block_start)
            block_end = min(value for value in (next_class, separator) if value >= 0)
            block = text[block_start:block_end]
            declared = set(re.findall(r"^    ([A-Za-z_][A-Za-z0-9_\[\]-]*):", block, re.MULTILINE))
            model_fields = set(model.model_fields)
            aliases = {field.alias for field in model.model_fields.values() if field.alias}
            assert declared <= model_fields | aliases, f"{relative_path}: {class_name} has unmodelled fields"
            for field_name, annotation in re.findall(
                r"^    ([A-Za-z_][A-Za-z0-9_\[\]-]*):\s*([^=]+)", block, re.MULTILINE
            ):
                field = next((value for value in model.model_fields.values()
                              if value.alias == field_name or value.alias is None and value.validation_alias == field_name), None)
                if field is None:
                    field = model.model_fields.get(field_name)
                assert field is not None, f"{relative_path}: {class_name} field {field_name} is missing"
                assert _prompt_type_signature(annotation) == _model_type_signature(field.annotation), (
                    f"{relative_path}: {class_name}.{field_name} type drift"
                )


def test_prompt_enum_contracts_are_preserved():
    assert {member.value for member in PRType} == {"Bug fix", "Tests", "Enhancement", "Documentation", "Other"}
    assert {member.value for member in Label} == {"Bug fix", "Tests", "Enhancement", "Documentation", "Other"}


def test_add_docs_prompt_matches_output_model():
    prompt = (Path(__file__).parents[2] / "pr_agent" / "settings" / "pr_add_docs.toml").read_text(encoding="utf-8")
    assert "Code Documentation:" in prompt
    assert "relevant file:" in prompt
    assert "relevant line:" in prompt
    assert "doc placement:" in prompt
    assert "documentation:" in prompt
    CodeDocumentation.model_validate({"Code Documentation": [{
        "relevant file": "src/app.py", "relevant line": 12, "doc placement": "after",
        "documentation": "Document the handler.",
    }]})


def test_duplicate_doc_items_are_rejected():
    item = {
        "relevant file": "src/app.py", "relevant line": 12, "doc placement": "after",
        "documentation": "Document the handler.",
    }
    with pytest.raises(ValueError):
        CodeDocumentation.model_validate({"Code Documentation": [item, item]})


def test_estimate_effort_example_is_a_strict_integer():
    text = "estimated_effort_to_review_[1-5]: 3"
    assert type(load_yaml(text)["estimated_effort_to_review_[1-5]"]) is int
    Review.model_validate({
        "key_issues_to_review": [],
        "estimated_effort_to_review_[1-5]": load_yaml(text)["estimated_effort_to_review_[1-5]"],
    })


def test_ranking_example_is_a_valid_numeric_payload():
    text = 'which_response_was_better: 1\nwhy: "It is clearer."\nscore_response1: 9\nscore_response2: 7'
    parsed = load_yaml(text)
    assert isinstance(parsed["which_response_was_better"], int)
    PRRankResponses.model_validate(parsed)
