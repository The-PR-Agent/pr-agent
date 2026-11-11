"""Wrap repository-controlled filenames in a Markdown code span.

A leaf module on purpose: pure text handling with no PR-Agent imports, so every layer
(algo state, commands, risk rendering) can use it without forming an import cycle —
CodeQL flagged exactly that when the helper lived next to its consumers.
"""

import re


def _markdown_code_span(text: str) -> str:
    """Keep repository-controlled filenames inside one Markdown code span.

    Newlines and carriage returns are escaped so a crafted path cannot leave the span,
    and the delimiter grows past any backtick run the text itself contains.
    """
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    delimiter = "`" * (max((len(run.group()) for run in re.finditer(r"`+", text)), default=0) + 1)
    if text.startswith(("`", " ")) or text.endswith(("`", " ")):
        text = f" {text} "
    return f"{delimiter}{text}{delimiter}"
