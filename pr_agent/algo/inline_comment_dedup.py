"""Cross-run deduplication of inline (line-anchored) comments.

Implements the feature requested in issue #2037: when the agent runs more
than once on the same PR/MR, it re-posts identical inline suggestions on
every run, cluttering the discussion (observed in particular on GitLab).
This module fingerprints each inline comment and embeds the fingerprint as
an HTML-comment marker in the posted body. On later runs the existing
comment bodies are scanned for those markers to rebuild the set of
already-posted fingerprints, and any suggestion whose fingerprint is already
present is skipped.

Two fingerprints are computed per comment and matched with OR semantics:

- Body fingerprint: SHA-256 over (relevant_file, anchor line, normalised
  body). The category/importance tag and the ``**Suggestion:**`` lead are
  stripped and whitespace is collapsed first. The default compares the first
  80 characters; providers may compare the complete body.
- Code fingerprint: SHA-256 over (relevant_file, anchor line, normalised
  contents of the first ```suggestion fenced block). Returns None when the
  body has no suggestion block, in which case matching falls back to the
  body fingerprint alone.

The OR-match catches both "same prose, different code" and "same code,
different prose" re-emissions of the same defect, which are the two ways an
LLM tends to restate a finding across runs.

The feature is opt-in via ``config.persistent_inline_comments`` (default
false) and is wired into the GitHub, GitLab, and Azure DevOps providers. The
marker-scan store needs no external infrastructure; a different backend
(database, cache) could populate the same load/seen/add interface.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterator, Optional

BODY_MARKER_RE = re.compile(r"<!-- pr-agent-dedup: ([a-f0-9]{12}) -->")
CODE_MARKER_RE = re.compile(r"<!-- pr-agent-dedup-code: ([a-f0-9]{12}) -->")
KEY_ISSUE_LOCATION_MARKER_RE = re.compile(r"<!-- pr-agent-key-issue-location: ([a-f0-9]{12}) -->")
_MARKER_RES = (BODY_MARKER_RE, CODE_MARKER_RE, KEY_ISSUE_LOCATION_MARKER_RE)

_LEAD_RE = re.compile(r"^\*\*Suggestion:\*\*\s*", re.IGNORECASE)
_TAG_RE = re.compile(r"\[[^\]]+?,\s*importance:\s*\d+\]", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_CODE_BLOCK_RE = re.compile(r"```suggestion[^\n]*\n(.*?)```", re.DOTALL)


def has_marker(body: str) -> bool:
    """True only if the body carries a well-formed dedup marker (12-hex),
    so incidental text mentioning the marker syntax is not mistaken for one."""
    return bool(BODY_MARKER_RE.search(body or "") or CODE_MARKER_RE.search(body or ""))


def is_agent_inline_comment(body: str) -> bool:
    body = (body or "").lstrip()
    return has_marker(body) or bool(_LEAD_RE.match(body))


def marker_fingerprints(body: str) -> set:
    """All dedup-marker fingerprints embedded in one comment body."""
    found = set()
    for marker_re in _MARKER_RES:
        for match in marker_re.finditer(body or ""):
            found.add(match.group(1))
    return found


def _strip_markers(body: str) -> str:
    """Remove embedded dedup markers so a pre-marked body fingerprints the
    same as its original (markers are appended after marking)."""
    body = BODY_MARKER_RE.sub("", body or "")
    body = CODE_MARKER_RE.sub("", body)
    return body


def _body_fingerprint(relevant_file: str, target_line_no, body: str, max_chars: Optional[int]) -> str:
    normalised = _LEAD_RE.sub("", _strip_markers(body))
    normalised = _TAG_RE.sub("", normalised)
    normalised = _WS_RE.sub(" ", normalised).strip().lower()
    if max_chars is not None:
        normalised = normalised[:max_chars]
    key = f"{relevant_file}|{target_line_no}|{normalised}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def body_fingerprint(relevant_file: str, target_line_no, body: str) -> str:
    return _body_fingerprint(relevant_file, target_line_no, body, 80)


def full_body_fingerprint(relevant_file: str, target_line_no, body: str) -> str:
    return _body_fingerprint(relevant_file, target_line_no, body, None)


def key_issue_fingerprint(relevant_file: str, body: str) -> str:
    key = f"{relevant_file}|{body}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def key_issue_location_fingerprint(fingerprint: str, start_line: int, end_line: int) -> str:
    key = f"{fingerprint}|{start_line}|{end_line}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def code_fingerprint(relevant_file: str, target_line_no, body: str) -> Optional[str]:
    match = _CODE_BLOCK_RE.search(_strip_markers(body))
    if not match:
        return None
    # Do not lower-case: code is case-sensitive, so case-only differences
    # must produce distinct fingerprints.
    code = match.group(1)
    code = _WS_RE.sub(" ", code).strip()
    if not code:
        return None
    key = f"{relevant_file}|{target_line_no}|code|{code}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def extract_suggestion_code(body: str) -> Optional[str]:
    match = _CODE_BLOCK_RE.search(_strip_markers(body))
    if not match:
        return None
    return match.group(1).strip("\n")


def build_markers(body_fp: str, code_fp: Optional[str]) -> str:
    markers = [f"<!-- pr-agent-dedup: {body_fp} -->"]
    if code_fp is not None:
        markers.append(f"<!-- pr-agent-dedup-code: {code_fp} -->")
    return "\n".join(markers)


def _append_markers(body: str, markers: str, max_chars: Optional[int]) -> str:
    suffix = f"\n\n{markers}"
    if max_chars and len(body) + len(suffix) > max_chars:
        body = body[: max(0, max_chars - len(suffix))]
    return f"{body}{suffix}"


def body_with_markers(body: str, body_fp: str, code_fp: "Optional[str]",
                      max_chars: "Optional[int]" = None) -> str:
    """Append the dedup marker(s) to a comment body. If max_chars is given and
    body + markers would exceed it, the body is clipped (never the markers) so
    the fingerprint marker always survives for the next run's scan."""
    return _append_markers(body, build_markers(body_fp, code_fp), max_chars)


def key_issue_body_with_markers(body: str, body_fp: str, location_fp: str,
                                max_chars: Optional[int] = None) -> str:
    markers = (f"{build_markers(body_fp, None)}\n"
               f"<!-- pr-agent-key-issue-location: {location_fp} -->")
    return _append_markers(body, markers, max_chars)


def inline_comment_line(comment: dict):
    """Best-effort anchor line for a GitHub inline-comment dict."""
    for key in ("line", "position", "start_line"):
        if comment.get(key) is not None:
            return comment[key]
    return None


def iter_existing_inline_comment_bodies(git_provider) -> Iterator[str]:
    """Yield the body of every existing comment on the current PR/MR.

    Dispatch is by provider class name so this module needs no provider
    import. Unsupported providers raise NotImplementedError, which the store
    treats as "cannot dedup here" and degrades to within-run dedup only.
    """
    provider_name = type(git_provider).__name__
    if provider_name == "GithubProvider":
        for comment in git_provider.pr.get_comments():
            yield getattr(comment, "body", "") or ""
    elif provider_name == "GitLabProvider":
        for discussion in git_provider.mr.discussions.list(get_all=True):
            attrs = getattr(discussion, "attributes", None) or {}
            for note in attrs.get("notes", []) or []:
                if isinstance(note, dict):
                    yield note.get("body", "") or ""
        # The committable-suggestion fallback posts via mr.notes.create, which
        # may not surface as a discussion; scan plain notes too so their markers
        # are seen on later runs.
        for note in git_provider.mr.notes.list(get_all=True):
            yield getattr(note, "body", "") or ""
        # gitlab.publish_code_suggestions_as_review queues suggestions as draft notes
        # (invisible in the discussions/notes listings above until published). Scan
        # them too, so a marker from a draft that's still pending - e.g. because a
        # prior run's bulk-publish failed - is still seen, instead of being re-posted
        # as a duplicate once it (or a fresh copy) is eventually published.
        for draft in git_provider.mr.draft_notes.list(get_all=True):
            yield getattr(draft, "note", "") or ""
    elif provider_name == "AzureDevopsProvider":
        yield from git_provider.get_persistent_comment_bodies()
    else:
        raise NotImplementedError(
            f"inline-comment dedup not implemented for {provider_name}"
        )


def can_verify_inline_comment_publication(git_provider) -> bool:
    return (callable(getattr(git_provider, "get_persistent_comment_bodies", None)) and
            callable(getattr(git_provider, "get_recent_inline_comment_bodies", None)))


class InlineCommentStore:
    """Set of already-posted inline-comment fingerprints for one PR/MR.

    The existing comment bodies are scanned lazily on first lookup and the
    seen-set is held in memory for the rest of the run. A failure to list
    existing comments degrades to within-run dedup only and never raises
    into the publish path.
    """

    def __init__(self, git_provider):
        self._git_provider = git_provider
        self._keys: set = set()
        self._released: set = set()
        self._loaded = False
        self._load_failed = False

    def load(self) -> set:
        if self._loaded:
            return self._keys
        try:
            for body in iter_existing_inline_comment_bodies(self._git_provider):
                self.add_body(body)
            fingerprints = getattr(
                self._git_provider, "get_existing_inline_comment_fingerprints", None
            )
            if callable(fingerprints):
                for fingerprint in fingerprints():
                    self.add(fingerprint)
        except Exception as e:
            self._load_failed = True
            from pr_agent.log import get_logger
            get_logger().info(
                f"Persistent inline comments: could not load existing comments, "
                f"within-run dedup only. error={e}"
            )
        self._keys -= self._released
        self._loaded = True
        return self._keys

    @property
    def load_failed(self) -> bool:
        return self._load_failed

    def seen(self, fingerprint: Optional[str]) -> bool:
        if fingerprint is None:
            return False
        return fingerprint in self.load()

    def add(self, fingerprint: Optional[str]) -> None:
        if fingerprint is not None:
            self._keys.add(fingerprint)

    def add_body(self, body: str) -> None:
        self._keys |= marker_fingerprints(body)

    def release(self, fingerprints) -> None:
        """Forget fingerprints whose threads the outdated-thread sweep resolved, so they
        stop suppressing their own replacements. Applied to what is already loaded and
        again after any later load, since the sweep and the load can run in either order.
        Human-resolved threads never reach here and stay suppressive."""
        self._released |= set(fingerprints)
        self._keys -= self._released


def get_inline_comment_store(git_provider) -> InlineCommentStore:
    """Return the per-provider store, creating and caching it on first use."""
    store = getattr(git_provider, "_inline_comment_store", None)
    if store is None:
        store = InlineCommentStore(git_provider)
        git_provider._inline_comment_store = store
    return store
