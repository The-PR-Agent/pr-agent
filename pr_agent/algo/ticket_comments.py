import re
from datetime import UTC, datetime
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from pr_agent.config_loader import get_settings


def ticket_comment_limits() -> tuple[int, int]:
    def bounded_int(key, default, ceiling):
        value = get_settings().get(key, default)
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            return 0
        try:
            return min(max(int(value), 0), ceiling)
        except ValueError:
            return 0

    return (
        bounded_int("config.ticket_comments_max", 0, 100),
        bounded_int("config.ticket_comments_max_characters", 2000, 10000),
    )


def _image_placeholder(alt, source=""):
    name = alt or unquote(urlsplit(source).path).rsplit("/", 1)[-1] or "image"
    name = " ".join(name.replace("[", "").replace("]", "").split())
    return f"[image: {name[:200]}]"


def _closing_delimiter(text, start, opening, closing):
    depth = 1
    position = start + 1
    while position < len(text):
        if text[position] == "\\":
            position += 2
            continue
        if text[position] == opening:
            depth += 1
        elif text[position] == closing:
            depth -= 1
            if depth == 0:
                return position
        position += 1
    return len(text)


def _comment_text(text: str) -> str:
    # Remove image destinations before stripping HTML or truncating the text.
    soup = BeautifulSoup(text, "html.parser")
    for image in soup.find_all("img"):
        image.replace_with(_image_placeholder(image.get("alt", ""), image.get("src", "")))
    for hidden in soup.find_all(["script", "style"]):
        hidden.decompose()
    text = soup.get_text()
    parts = []
    references = set()
    position = 0
    while (start := text.find("![", position)) != -1:
        end = _closing_delimiter(text, start + 1, "[", "]")
        if end == len(text):
            break
        alt = text[start + 2:end]
        after = end + 1
        if text[after:after + 1] == "(":
            after = min(_closing_delimiter(text, after, "(", ")") + 1, len(text))
        elif text[after:after + 1] == "[":
            reference_end = text.find("]", after + 1)
            if reference_end != -1:
                references.add((text[after + 1:reference_end] or alt).casefold())
                after = reference_end + 1
        else:
            references.add(alt.casefold())
        if start > position and text[start - 1] == "[" and text[after:after + 2] == "](":
            start -= 1
            after = min(_closing_delimiter(text, after + 1, "(", ")") + 1, len(text))
        parts.extend((text[position:start], _image_placeholder(alt)))
        position = after
    parts.append(text[position:])
    text = "".join(parts)
    lines = []
    skip_destination = False
    for line in text.splitlines():
        if skip_destination:
            skip_destination = False
            continue
        definition = re.match(r" {0,3}\[([^\]\r\n]+)\]:", line)
        if definition and definition.group(1).casefold() in references:
            skip_destination = not line[definition.end():].strip()
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def normalize_ticket_comments(comments: list[dict], maximum: int, characters: int) -> list[dict]:
    normalized = []
    if maximum <= 0 or characters <= 0:
        return normalized
    for comment in comments:
        if comment.get("is_deleted") or comment.get("is_system"):
            continue
        body = comment.get("body")
        if not isinstance(body, str) or not body.strip():
            continue
        body = _comment_text(body)[:characters]
        if not body:
            continue
        created_at = comment.get("created_at") or ""
        if hasattr(created_at, "isoformat"):
            created_at = created_at.isoformat()
        try:
            date = datetime.fromisoformat(str(created_at))
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            created_at = date.astimezone(UTC).isoformat()
        except ValueError:
            created_at = ""
        normalized.append({"body": body, "created_at": created_at})
        if len(normalized) >= maximum:
            break
    return normalized
