"""Pydantic models for structured outputs described by prompt templates."""

from enum import Enum
from typing import List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


class SubPR(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relevant_files: List[str]
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


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_compliance_check: Optional[List[TicketCompliance]] = None
    estimated_effort_to_review: Optional[StrictInt] = Field(
        default=None, alias="estimated_effort_to_review_[1-5]", ge=1, le=5
    )
    risk_level: Optional[Literal["low", "medium", "high"]] = None
    merge_recommendation: Optional[Literal["safe_to_merge", "merge_with_caution", "changes_required"]] = None
    review_priority_files: Optional[List[str]] = None
    contribution_time_cost_estimate: Optional[ContributionTimeCostEstimate] = None
    score: Optional[StrictInt] = Field(default=None, ge=0, le=100)
    relevant_tests: Optional[Literal["Yes", "No"]] = None
    insights_from_user_answers: Optional[str] = None
    key_issues_to_review: List[KeyIssuesComponentLink]
    security_concerns: Optional[str] = None
    todo_sections: Optional[Union[List[TodoSection], str]] = None
    can_be_split: Optional[List[SubPR]] = Field(default=None, max_length=3)

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
    code_suggestions: List[CodeSuggestion]


class CodeSuggestionFeedback(BaseModel):
    suggestion_summary: str
    relevant_file: str
    relevant_lines_start: int
    relevant_lines_end: int
    suggestion_score: int = Field(ge=0, le=10)
    why: str


class PRCodeSuggestionsFeedback(BaseModel):
    code_suggestions: List[CodeSuggestionFeedback]


class PRType(str, Enum):
    BUG_FIX = "Bug fix"
    TESTS = "Tests"
    ENHANCEMENT = "Enhancement"
    DOCUMENTATION = "Documentation"
    OTHER = "Other"


class FileDescription(BaseModel):
    filename: str
    changes_summary: Optional[str] = None
    changes_title: str
    label: str


class PRDescription(BaseModel):
    type: List[PRType] = Field(min_length=1)
    description: Optional[str] = None
    title: str
    changes_diagram: Optional[str] = None
    pr_files: Optional[List[FileDescription]] = Field(default=None, max_length=20)


class PRDescriptionHeaders(BaseModel):
    type: List[PRType] = Field(min_length=1)
    description: Optional[str] = None
    title: str
    changes_diagram: Optional[str] = None


class PRFilesWalkthrough(BaseModel):
    pr_files: List[FileDescription]


class Label(str, Enum):
    BUG_FIX = "Bug fix"
    TESTS = "Tests"
    ENHANCEMENT = "Enhancement"
    DOCUMENTATION = "Documentation"
    OTHER = "Other"


class Labels(BaseModel):
    labels: List[str]

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

    code_documentation: List[CodeDocumentationItem] = Field(alias="Code Documentation")

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
    relevant_sections: List[RelevantSection]
    question_is_relevant: Optional[int] = Field(default=None, ge=0, le=1)


class FileIdxAndPath(BaseModel):
    idx: int = Field(ge=0)
    file_name: str


class DocHeadingsHelper(BaseModel):
    user_question: str
    relevant_files_ranking: List[FileIdxAndPath]
