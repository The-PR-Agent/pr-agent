from dataclasses import dataclass
from enum import Enum
from typing import Optional, TypedDict

from pydantic import BaseModel


class EDIT_TYPE(Enum):
    ADDED = 1
    DELETED = 2
    MODIFIED = 3
    RENAMED = 4
    UNKNOWN = 5


@dataclass
class FilePatchInfo:
    base_file: str
    head_file: str
    patch: str
    filename: str
    tokens: int = -1
    edit_type: EDIT_TYPE = EDIT_TYPE.UNKNOWN
    old_filename: str = None
    num_plus_lines: int = -1
    num_minus_lines: int = -1
    language: Optional[str] = None
    ai_file_summary: str = None
class Range(BaseModel):
    line_start: int  # should be 0-indexed
    line_end: int
    column_start: int = -1
    column_end: int = -1


class ModelType(str, Enum):
    REGULAR = "regular"
    WEAK = "weak"
    REASONING = "reasoning"


class TodoItem(TypedDict):
    relevant_file: str
    line_number: int
    content: str


class PRReviewHeader(str, Enum):
    REGULAR = "## PR Reviewer Guide"
    INCREMENTAL = "## Incremental PR Reviewer Guide"


class ReasoningEffort(str, Enum):
    XHIGH = "xhigh"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    MINIMAL = "minimal"
    NONE = "none"


class PRDescriptionHeader(str, Enum):
    DIAGRAM_WALKTHROUGH = "Diagram Walkthrough"
    FILE_WALKTHROUGH = "File Walkthrough"
