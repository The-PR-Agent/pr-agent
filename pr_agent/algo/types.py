from dataclasses import dataclass
from enum import Enum
from typing import Optional


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
    head_file_is_complete: bool = True
    # Set when the provider could not read one side of the file, so no trustworthy patch can
    # be built for it. The file is kept with an empty patch and this flag, and diff generation
    # tells the model the file could not be reviewed instead of dropping it silently.
    content_fetch_failed: bool = False
