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
  first 80 characters of the body). The category/importance tag and the
  ``**Suggestion:**`` lead are stripped and whitespace is collapsed first.
- Code fingerprint: SHA-256 over (relevant_file, anchor line, normalised
  contents of the first ```suggestion fenced block). Returns None when the
  body has no suggestion block, in which case matching falls back to the
  body fingerprint alone.

The OR-match catches both "same prose, different code" and "same code,
different prose" re-emissions of the same defect, which are the two ways an
LLM tends to restate a finding across runs.

The feature is opt-in via ``config.persistent_inline_comments`` (default
false) and is wired into the GitHub and GitLab providers. The marker-scan
store needs no external infrastructure; a different backend (database,
cache) could populate the same load/seen/add interface.
"""

from __future__ import annotations

import hashlib
import re
from typing import Iterator, Optional

from pr_agent.log import get_logger

BODY_MARKER_RE = re.compile(r"<!-- pr-agent-dedup: ([a-f0-9]{12}) -->")
CODE_MARKER_RE = re.compile(r"<!-- pr-agent-dedup-code: ([a-f0-9]{12}) -->")

_LEAD_RE = re.compile(r"^\*\*Suggestion:\*\*\s*", re.IGNORECASE)
_TAG_RE = re.compile(r"\[(?:[a-z _]+),\s*importance:\s*\d+\]", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_CODE_BLOCK_RE = re.compile(r"```suggestion[^\n]*\n(.*?)```", re.DOTALL)


def body_fingerprint(relevant_file: str, target_line_no, body: str) -> str:
    normalised = _LEAD_RE.sub("", body or "")
    normalised = _TAG_RE.sub("", normalised)
    normalised = _WS_RE.sub(" ", normalised).strip()[:80].lower()
    key = f"{relevant_file}|{target_line_no}|{normalised}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def code_fingerprint(relevant_file: str, target_line_no, body: str) -> Optional[str]:
    m = _CODE_BLOCK_RE.search(body or "")
    if not m:
        return None
    code = _WS_RE.sub(" ", m.group(1)).strip().lower()
    if not code:
        return None
    key = f"{relevant_file}|{target_line_no}|code|{code}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def build_markers(body_fp: str, code_fp: Optional[str]) -> str:
    markers = [f"<!-- pr-agent-dedup: {body_fp} -->"]
    if code_fp is not None:
        markers.append(f"<!-- pr-agent-dedup-code: {code_fp} -->")
    return "\n".join(markers)


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
    else:
        raise NotImplementedError(
            f"inline-comment dedup not implemented for {provider_name}"
        )


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
        self._loaded = False

    def load(self) -> set:
        if self._loaded:
            return self._keys
        try:
            for body in iter_existing_inline_comment_bodies(self._git_provider):
                for marker_re in (BODY_MARKER_RE, CODE_MARKER_RE):
                    for match in marker_re.finditer(body or ""):
                        self._keys.add(match.group(1))
        except Exception as e:
            get_logger().info(
                f"Persistent inline comments: could not load existing comments, "
                f"within-run dedup only. error={e}"
            )
        self._loaded = True
        return self._keys

    def seen(self, fingerprint: Optional[str]) -> bool:
        if fingerprint is None:
            return False
        return fingerprint in self.load()

    def add(self, fingerprint: Optional[str]) -> None:
        if fingerprint is not None:
            self._keys.add(fingerprint)


def get_inline_comment_store(git_provider) -> InlineCommentStore:
    """Return the per-provider store, creating and caching it on first use."""
    store = getattr(git_provider, "_inline_comment_store", None)
    if store is None:
        store = InlineCommentStore(git_provider)
        git_provider._inline_comment_store = store
    return store
