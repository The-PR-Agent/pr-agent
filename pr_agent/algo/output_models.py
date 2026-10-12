"""Pydantic models for structured outputs described by prompt templates."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationError, field_validator


class SubPR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relevant_files: list[str]
    title: str


class KeyIssuesComponentLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relevant_file: str
    issue_header: str
    issue_content: str
    start_line: int
    end_line: int


class TodoSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relevant_file: str
    line_number: int
    content: str


class TicketCompliance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_url: str
    ticket_requirements: str
    fully_compliant_requirements: str
    not_compliant_requirements: str
    requires_further_human_verification: str


class ContributionTimeCostEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    best_case: str
    average_case: str
    worst_case: str


class FailureMode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what: str
    where: str
    trigger: str
    detected_by: str
    covered_in_this_pr: StrictBool


def parse_failure_modes(value: object) -> list[dict]:
    """Keep at most three valid cases, including when review validation only warns."""
    modes = []
    for entry in value if isinstance(value, list) else []:
        try:
            modes.append(FailureMode.model_validate(entry).model_dump())
        except ValidationError:
            continue
        if len(modes) == 3:
            break
    return modes


class Review(BaseModel):
    # extra_instructions and [artifacts] may request fields beyond the prompt schema
    model_config = ConfigDict(extra="allow")

    ticket_compliance_check: list[TicketCompliance] | None = None
    estimated_effort_to_review: StrictInt | None = Field(
        default=None, alias="estimated_effort_to_review_[1-5]", ge=1, le=5
    )
    risk_level: Literal["low", "medium", "high"] | None = None
    merge_recommendation: Literal["no_concerns_found", "needs_review", "changes_required"] | None = None
    failure_modes: list[FailureMode] | None = Field(default=None, max_length=3)
    review_priority_files: list[str] | None = None
    contribution_time_cost_estimate: ContributionTimeCostEstimate | None = None
    score: StrictInt | None = Field(default=None, ge=0, le=100)
    relevant_tests: Literal["Yes", "No"] | None = None
    insights_from_user_answers: str | None = None
    key_issues_to_review: list[KeyIssuesComponentLink]
    security_concerns: str | None = None
    todo_sections: list[TodoSection] | str | None = None
    can_be_split: list[SubPR] | None = Field(default=None, max_length=3)

    @field_validator("risk_level", "merge_recommendation", "relevant_tests", mode="before")
    @classmethod
    def _strip_literal_values(cls, value):
        return value.strip() if isinstance(value, str) else value


class PRReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review: Review


class CodeSuggestion(BaseModel):
    relevant_file: str
    language: str
    existing_code: str
    suggestion_content: str
    improved_code: str
    one_sentence_summary: str
    label: str


class PRCodeSuggestions(BaseModel):
    code_suggestions: list[CodeSuggestion]


class CodeSuggestionFeedback(BaseModel):
    suggestion_summary: str
    relevant_file: str
    relevant_lines_start: int
    relevant_lines_end: int
    suggestion_score: int = Field(ge=0, le=10)
    why: str


class PRCodeSuggestionsFeedback(BaseModel):
    code_suggestions: list[CodeSuggestionFeedback]


class PRType(StrEnum):
    BUG_FIX = "Bug fix"
    TESTS = "Tests"
    ENHANCEMENT = "Enhancement"
    DOCUMENTATION = "Documentation"
    OTHER = "Other"


class FileDescription(BaseModel):
    filename: str
    changes_summary: str | None = None
    changes_title: str
    label: str


class PRDescription(BaseModel):
    type: list[PRType] = Field(min_length=1)
    description: str | None = None
    title: str
    changes_diagram: str | None = None
    pr_files: list[FileDescription] | None = Field(default=None, max_length=20)


class PRDescriptionAssembled(PRDescription):
    """Validate the final description after file chunks and uncovered files are joined."""

    pr_files: list[FileDescription] | None = None


class PRDescriptionHeaders(BaseModel):
    type: list[PRType] = Field(min_length=1)
    description: str | None = None
    title: str
    changes_diagram: str | None = None


class PRFilesWalkthrough(BaseModel):
    pr_files: list[FileDescription]


class Label(StrEnum):
    BUG_FIX = "Bug fix"
    TESTS = "Tests"
    ENHANCEMENT = "Enhancement"
    DOCUMENTATION = "Documentation"
    OTHER = "Other"


class Labels(BaseModel):
    labels: list[str]

    @field_validator("labels", mode="before")
    @classmethod
    def _normalize_labels(cls, value):
        if isinstance(value, str):
            entries = value.split(",")
        elif isinstance(value, list):
            if not value:
                return []
            entries = value
        else:
            raise ValueError("labels must be a list or comma-separated string")

        labels = []
        for entry in entries:
            if isinstance(entry, dict):
                entry = next(
                    (
                        entry[key]
                        for key in ("name", "label", "title", "value")
                        if isinstance(entry.get(key), str) and entry[key].strip()
                    ),
                    None,
                )
            if isinstance(entry, bool) or not isinstance(entry, (str, int, float)):
                continue
            label = str(entry).strip()
            if label:
                labels.append(label)

        if not labels:
            raise ValueError("labels must contain at least one usable value")
        return labels


class CodeDocumentationItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    relevant_file: str = Field(alias="relevant file")
    relevant_line: StrictInt = Field(alias="relevant line", ge=1)
    doc_placement: Literal["before", "after"] = Field(alias="doc placement")
    documentation: str


class CodeDocumentation(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    code_documentation: list[CodeDocumentationItem] = Field(alias="Code Documentation")

    @field_validator("code_documentation", mode="after")
    @classmethod
    def _reject_duplicate_items(cls, items):
        serialized = [tuple(sorted(item.model_dump().items())) for item in items]
        if len(serialized) != len(set(serialized)):
            raise ValueError("Code Documentation items must be unique")
        return items


class PRRankResponses(BaseModel):
    which_response_was_better: Literal[0, 1, 2]
    why: str
    score_response1: int = Field(ge=1, le=10)
    score_response2: int = Field(ge=1, le=10)


class RelevantSection(BaseModel):
    file_name: str
    relevant_section_header_string: str


class DocHelper(BaseModel):
    user_question: str
    response: str
    relevant_sections: list[RelevantSection]
    question_is_relevant: int | None = Field(default=None, ge=0, le=1)


class FileIdxAndPath(BaseModel):
    idx: int = Field(ge=0)
    file_name: str


class DocHeadingsHelper(BaseModel):
    user_question: str
    relevant_files_ranking: list[FileIdxAndPath]
