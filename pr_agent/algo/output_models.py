"""Pydantic models for structured outputs described by prompt templates."""

from enum import Enum
from typing import List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field


class SubPR(BaseModel):
    relevant_files: List[str]
    title: str


class KeyIssuesComponentLink(BaseModel):
    relevant_file: str
    issue_header: str
    issue_content: str
    start_line: int
    end_line: int


class TodoSection(BaseModel):
    relevant_file: str
    line_number: int
    content: str


class TicketCompliance(BaseModel):
    ticket_url: str
    ticket_requirements: str
    fully_compliant_requirements: str
    not_compliant_requirements: str
    requires_further_human_verification: str


class ContributionTimeCostEstimate(BaseModel):
    best_case: str
    average_case: str
    worst_case: str


class Review(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    ticket_compliance_check: Optional[List[TicketCompliance]] = None
    estimated_effort_to_review: Optional[int] = Field(
        default=None, alias="estimated_effort_to_review_[1-5]"
    )
    risk_level: Optional[str] = None
    merge_recommendation: Optional[str] = None
    review_priority_files: Optional[List[str]] = None
    contribution_time_cost_estimate: Optional[ContributionTimeCostEstimate] = None
    score: Optional[int] = None
    relevant_tests: Optional[str] = None
    insights_from_user_answers: Optional[str] = None
    key_issues_to_review: List[KeyIssuesComponentLink]
    security_concerns: Optional[str] = None
    todo_sections: Optional[Union[List[TodoSection], str]] = None
    can_be_split: Optional[List[SubPR]] = None


class PRReview(BaseModel):
    review: Review


class CodeSuggestion(BaseModel):
    relevant_file: str
    language: str
    existing_code: str
    suggestion_content: str
    improved_code: str
    one_sentence_summary: str
    label: Optional[str] = None


class PRCodeSuggestions(BaseModel):
    code_suggestions: List[CodeSuggestion]


class CodeSuggestionFeedback(BaseModel):
    suggestion_summary: str
    relevant_file: str
    relevant_lines_start: int
    relevant_lines_end: int
    suggestion_score: int
    why: str


class PRCodeSuggestionsFeedback(BaseModel):
    code_suggestions: List[CodeSuggestionFeedback]


class PRType(str, Enum):
    bug_fix = "Bug fix"
    tests = "Tests"
    enhancement = "Enhancement"
    documentation = "Documentation"
    other = "Other"


class FileDescription(BaseModel):
    filename: str
    changes_summary: Optional[str] = None
    changes_title: str
    label: str


class PRDescription(BaseModel):
    type: List[PRType]
    description: Optional[str] = None
    title: str
    changes_diagram: Optional[str] = None
    pr_files: Optional[List[FileDescription]] = None


class PRDescriptionHeaders(BaseModel):
    type: List[PRType]
    description: Optional[str] = None
    title: str
    changes_diagram: Optional[str] = None


class PRFilesWalkthrough(BaseModel):
    pr_files: List[FileDescription]


class Label(str, Enum):
    bug_fix = "Bug fix"
    tests = "Tests"
    enhancement = "Enhancement"
    documentation = "Documentation"
    other = "Other"


class Labels(BaseModel):
    labels: List[Label]


class PRRankRespones(BaseModel):
    which_response_was_better: int
    why: str
    score_response1: int
    score_response2: int


class RelevantSection(BaseModel):
    file_name: str
    relevant_section_header_string: str


class DocHelper(BaseModel):
    user_question: str
    response: str
    relevant_sections: List[RelevantSection]
    question_is_relevant: Optional[int] = None


class FileIdxAndPath(BaseModel):
    idx: int
    file_name: str


class DocHeadingsHelper(BaseModel):
    user_question: str
    relevant_files_ranking: List[FileIdxAndPath]


# Prompt templates use these lowercase names in their class declarations.
relevant_section = RelevantSection
file_idx_and_path = FileIdxAndPath
