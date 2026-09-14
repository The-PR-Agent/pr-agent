import re
from pathlib import Path

import pytest

from pr_agent.algo.output_models import (
    CodeSuggestion,
    CodeSuggestionFeedback,
    ContributionTimeCostEstimate,
    DocHeadingsHelper,
    DocHelper,
    FileDescription,
    FileIdxAndPath,
    KeyIssuesComponentLink,
    Labels,
    PRCodeSuggestions,
    PRCodeSuggestionsFeedback,
    PRDescription,
    PRDescriptionHeaders,
    PRFilesWalkthrough,
    PRRankRespones,
    PRReview,
    RelevantSection,
    Review,
    SubPR,
    TicketCompliance,
    TodoSection,
)


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
            "relevant_tests": "yes",
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
        (PRDescription, {"type": ["Bug fix"], "description": "Fix a runtime failure.", "title": "Fix runtime failure"}),
        (PRDescriptionHeaders, {"type": ["Tests"], "title": "Describe the test changes"}),
        (FileDescription, {"filename": "src/app.py", "changes_title": "Handle runtime failures", "label": "bug fix"}),
        (PRFilesWalkthrough, {"pr_files": [{
            "filename": "src/app.py", "changes_title": "Handle runtime failures", "label": "bug fix",
        }]}),
        (Labels, {"labels": ["Bug fix", "Tests"]}),
        (PRRankRespones, {"which_response_was_better": 1, "why": "It is clearer.", "score_response1": 9, "score_response2": 7}),
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


PROMPT_MODELS = {
    "pr_reviewer_prompts.toml": {"SubPR": SubPR, "KeyIssuesComponentLink": KeyIssuesComponentLink,
                                  "TodoSection": TodoSection, "TicketCompliance": TicketCompliance,
                                  "ContributionTimeCostEstimate": ContributionTimeCostEstimate, "Review": Review,
                                  "PRReview": PRReview},
    "pr_description_prompts.toml": {"FileDescription": FileDescription, "PRDescription": PRDescription},
    "pr_description_only_description_prompts.toml": {"PRDescriptionHeaders": PRDescriptionHeaders},
    "pr_description_only_files_prompts.toml": {"FileDescription": FileDescription, "PRFilesWalkthrough": PRFilesWalkthrough},
    "pr_custom_labels.toml": {"Labels": Labels},
    "pr_evaluate_prompt_response.toml": {"PRRankRespones": PRRankRespones},
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
