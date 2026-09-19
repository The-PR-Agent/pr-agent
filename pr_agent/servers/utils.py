import asyncio
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Sequence

from fastapi import HTTPException

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger

# custom_merge_loader bypasses Dynaconf token expansion, so TOML references such as
# @get would remain strings. Keep the shared profiles immutable and copy only defaults.
_STANDARD_PR_COMMANDS = (
    "/describe --pr_description.final_update_message=false",
    "/review",
    "/improve",
)
_PLAIN_PR_COMMANDS = ("/describe", "/review", "/improve")
_COMMITTABLE_PR_COMMANDS = (
    "/describe --pr_description.final_update_message=false",
    "/review",
    "/improve --pr_code_suggestions.commitable_code_suggestions=true",
)
_DEFAULT_PR_COMMANDS_BY_PROVIDER = {
    "github_app": _STANDARD_PR_COMMANDS,
    "gitlab": _STANDARD_PR_COMMANDS,
    "gitea": _PLAIN_PR_COMMANDS,
    "azure_devops_server": _PLAIN_PR_COMMANDS,
    "bitbucket_app": _COMMITTABLE_PR_COMMANDS,
    "bitbucket_server": _COMMITTABLE_PR_COMMANDS,
}
_MISSING = object()


def get_pr_commands(provider: str) -> Sequence[str]:
    """Return an explicit provider override or a fresh copy of its default profile."""
    configured = get_settings().get(f"{provider}.pr_commands", _MISSING)
    if configured is not _MISSING:
        return configured
    return list(_DEFAULT_PR_COMMANDS_BY_PROVIDER[provider])


def verify_signature(payload_body, secret_token, signature_header):
    """Verify that the payload was sent from GitHub by validating SHA256.

    Raise and return 403 if not authorized.

    Args:
        payload_body: original request body to verify (request.body())
        secret_token: GitHub app webhook token (WEBHOOK_SECRET)
        signature_header: header received from GitHub (x-hub-signature-256)
    """
    if not signature_header:
        raise HTTPException(status_code=403, detail="x-hub-signature-256 header is missing!")
    hash_object = hmac.new(secret_token.encode('utf-8'), msg=payload_body, digestmod=hashlib.sha256)
    expected_signature = "sha256=" + hash_object.hexdigest()
    if not hmac.compare_digest(expected_signature, signature_header):
        raise HTTPException(status_code=403, detail="Request signatures didn't match!")


def basic_auth_matches(credentials, username, password) -> bool:
    """Compare HTTP basic credentials against the configured pair in constant time.

    The comparison runs on UTF-8 bytes: given `str` arguments, secrets.compare_digest
    accepts ASCII only and raises TypeError otherwise, so a configured username or
    password with a non-ASCII character would turn every authenticated call into a 500.
    Both fields are compared before the result is combined, so a wrong username costs
    the same as a wrong password.
    """
    user_ok = secrets.compare_digest(credentials.username.encode("utf-8"), str(username).encode("utf-8"))
    pass_ok = secrets.compare_digest(credentials.password.encode("utf-8"), str(password).encode("utf-8"))
    return user_ok and pass_ok


class RateLimitExceeded(Exception):
    """Raised when the git provider API rate limit has been exceeded."""
    pass


class DefaultDictWithTimeout(defaultdict):
    """A defaultdict with a time-to-live (TTL)."""

    def __init__(
        self,
        default_factory: Callable[[], Any] = None,
        ttl: int = None,
        refresh_interval: int = 60,
        update_key_time_on_get: bool = True,
        *args,
        **kwargs,
    ):
        """
        Args:
            default_factory: The default factory to use for keys that are not in the dictionary.
            ttl: The time-to-live (TTL) in seconds.
            refresh_interval: How often to refresh the dict and delete items older than the TTL.
            update_key_time_on_get: Whether to update the access time of a key also on get (or only when set).
        """
        super().__init__(default_factory, *args, **kwargs)
        self.__key_times = dict()
        self.__ttl = ttl
        self.__refresh_interval = refresh_interval
        self.__update_key_time_on_get = update_key_time_on_get
        self.__last_refresh = self.__time() - self.__refresh_interval

    @staticmethod
    def __time():
        return time.monotonic()

    def __refresh(self):
        if self.__ttl is None:
            return
        request_time = self.__time()
        if request_time - self.__last_refresh < self.__refresh_interval:
            return
        to_delete = [key for key, key_time in self.__key_times.items() if request_time - key_time > self.__ttl]
        for key in to_delete:
            del self[key]
        self.__last_refresh = request_time

    def __getitem__(self, __key):
        if self.__update_key_time_on_get:
            self.__key_times[__key] = self.__time()
        self.__refresh()
        try:
            return super().__getitem__(__key)
        except KeyError:
            self.__key_times.pop(__key, None)
            raise

    def __setitem__(self, __key, __value):
        self.__key_times[__key] = self.__time()
        return super().__setitem__(__key, __value)

    def __delitem__(self, __key):
        self.__key_times.pop(__key, None)
        if super().__contains__(__key):
            super().__delitem__(__key)

    def setdefault(self, __key, __default=None):
        self.__refresh()
        if super().__contains__(__key):
            if self.__update_key_time_on_get:
                self.__key_times[__key] = self.__time()
            return super().__getitem__(__key)
        self[__key] = __default
        return __default


@dataclass
class _PushTriggerState:
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)
    active_tasks: int = 0
    running: bool = False


_push_trigger_states_by_ttl = {}
_active_push_trigger_states = {}


def _get_push_trigger_state(key: str, ttl: int | None) -> _PushTriggerState:
    # TTL changes and cache eviction must never split an active PR's queue.
    if key in _active_push_trigger_states:
        return _active_push_trigger_states[key]
    if ttl not in _push_trigger_states_by_ttl:
        _push_trigger_states_by_ttl[ttl] = DefaultDictWithTimeout(_PushTriggerState, ttl=ttl)
    states = _push_trigger_states_by_ttl[ttl]
    # setdefault expires idle entries before refreshing their access time.
    state = states.setdefault(key)
    if state is None:
        state = states[key] = _PushTriggerState()
    return state


@asynccontextmanager
async def push_trigger_slot(key: str, *, allow_backlog: bool, ttl: int | None) -> AsyncIterator[bool]:
    """Run one push per PR, optionally keeping one delegate for subsequent pushes.

    State is process-local. TTL bounds idle cache retention; active runs and
    waiters retain their state until all reserved slots have been released.
    """
    state = _get_push_trigger_state(key, ttl)
    max_active_tasks = 2 if allow_backlog else 1
    if state.active_tasks >= max_active_tasks:
        get_logger().info(
            f"Skipping push trigger for {key=} because another event already triggered the same processing"
        )
        yield False
        return

    get_logger().info(
        f"Continue processing push trigger for {key=} because there are {state.active_tasks} active tasks"
    )
    _active_push_trigger_states[key] = state
    state.active_tasks += 1
    acquired = False
    try:
        async with state.condition:
            await state.condition.wait_for(lambda: not state.running)
            state.running = True
            acquired = True
        yield True
    finally:
        async with state.condition:
            if acquired:
                state.running = False
            state.active_tasks -= 1
            state.condition.notify(1)
            if state.active_tasks == 0:
                del _active_push_trigger_states[key]


def _extract_pr_metadata(data: dict, provider: str | None = None):
    """Extract standard PR metadata from provider-specific webhook payloads.

    Returns a tuple of (title, sender, repo_full_name, labels, source_branch, target_branch),
    or None if the payload does not represent an MR/PR event (e.g., GitLab non-MR payload).
    """
    title = ""
    sender = ""
    repo_full_name = ""
    labels = []
    source_branch = ""
    target_branch = ""

    if not isinstance(data, dict):
        return title, sender, repo_full_name, labels, source_branch, target_branch

    # 1. GitLab payload
    if provider == "gitlab" or "object_attributes" in data:
        obj_attr = data.get("object_attributes")
        if not obj_attr or not isinstance(obj_attr, dict):
            return None
        title = obj_attr.get("title") or ""
        sender = data.get("user", {}).get("username") or ""
        repo_full_name = data.get("project", {}).get("path_with_namespace") or ""
        source_branch = obj_attr.get("source_branch") or ""
        target_branch = obj_attr.get("target_branch") or ""
        raw_labels = obj_attr.get("labels") or []
        labels = [
            label.get("title", "") if isinstance(label, dict) else str(label)
            for label in raw_labels
            if label
        ]
        return title, sender, repo_full_name, labels, source_branch, target_branch

    # 2. Bitbucket Cloud payload
    if provider == "bitbucket_app" or ("data" in data and "pullrequest" in data.get("data", {})):
        pr_data = data.get("data", {}).get("pullrequest", {})
        title = pr_data.get("title") or ""
        source_branch = pr_data.get("source", {}).get("branch", {}).get("name") or ""
        target_branch = pr_data.get("destination", {}).get("branch", {}).get("name") or ""
        repo_full_name = pr_data.get("destination", {}).get("repository", {}).get("full_name") or ""
        actor = data.get("data", {}).get("actor") or data.get("actor") or {}
        if isinstance(actor, dict):
            sender = actor.get("username") or actor.get("display_name") or actor.get("nickname") or ""
        return title, sender, repo_full_name, labels, source_branch, target_branch

    # 3. Bitbucket Server payload
    if provider == "bitbucket_server" or "pullRequest" in data:
        pr_data = data.get("pullRequest", {})
        title = pr_data.get("title") or ""
        from_ref = pr_data.get("fromRef", {})
        source_branch = from_ref.get("displayId", "") if from_ref else ""
        to_ref = pr_data.get("toRef", {})
        target_branch = to_ref.get("displayId", "") if to_ref else ""
        author = pr_data.get("author", {})
        user = author.get("user", {}) if author else {}
        sender = user.get("name", "") if user else ""
        repository = to_ref.get("repository", {}) if to_ref else {}
        project = repository.get("project", {}) if repository else {}
        project_key = project.get("key", "") if project else ""
        repo_slug = repository.get("slug", "") if repository else ""
        repo_full_name = f"{project_key}/{repo_slug}" if project_key and repo_slug else ""
        return title, sender, repo_full_name, labels, source_branch, target_branch

    # 4. GitHub and Gitea payload
    if provider in ("github", "gitea") or "pull_request" in data:
        pull_request = data.get("pull_request") or {}
        title = pull_request.get("title") or ""
        source_branch = pull_request.get("head", {}).get("ref") or ""
        target_branch = pull_request.get("base", {}).get("ref") or ""
        pr_labels = pull_request.get("labels") or []
        labels = [
            label.get("name", "") if isinstance(label, dict) else str(label)
            for label in pr_labels
            if label
        ]
        sender = data.get("sender", {}).get("login") or ""
        repo_full_name = data.get("repository", {}).get("full_name") or ""
        return title, sender, repo_full_name, labels, source_branch, target_branch

    return title, sender, repo_full_name, labels, source_branch, target_branch


def should_process_pr_logic(
    data: dict | None = None,
    *,
    provider: str | None = None,
    title: str | None = None,
    sender: str | None = None,
    repo_full_name: str | None = None,
    labels: Sequence[str] | None = None,
    source_branch: str | None = None,
    target_branch: str | None = None,
    raise_on_error: bool = False,
) -> bool:
    """Determine whether a pull/merge request should be processed based on configuration.

    Evaluates ignore rules configured in settings:
      - CONFIG.IGNORE_REPOSITORIES: regex match on repo_full_name
      - CONFIG.IGNORE_PR_AUTHORS: regex match on sender/author
      - CONFIG.IGNORE_PR_TITLE: regex match on title (supports str or list[str])
      - CONFIG.IGNORE_PR_LABELS: exact match against any PR/MR label
      - CONFIG.IGNORE_PR_SOURCE_BRANCHES: regex match on source_branch
      - CONFIG.IGNORE_PR_TARGET_BRANCHES: regex match on target_branch

    Can be called with a webhook payload dict (`data`), explicit keyword arguments, or both
    (explicit kwargs override extracted payload values).

    When `raise_on_error` is True, exceptions raised during filtering are re-raised so callers
    can distinguish error fallbacks from successful evaluations. Otherwise, returns True.

    Returns True if the PR should be processed, False if it should be ignored.
    """
    try:
        if data is not None:
            extracted = _extract_pr_metadata(data, provider=provider)
            if extracted is None:
                return False
            ext_title, ext_sender, ext_repo, ext_labels, ext_source, ext_target = extracted
            if title is None:
                title = ext_title
            if sender is None:
                sender = ext_sender
            if repo_full_name is None:
                repo_full_name = ext_repo
            if labels is None:
                labels = ext_labels
            if source_branch is None:
                source_branch = ext_source
            if target_branch is None:
                target_branch = ext_target

        title = title or ""
        sender = sender or ""
        repo_full_name = repo_full_name or ""
        labels = labels or []
        source_branch = source_branch or ""
        target_branch = target_branch or ""

        # Ignore PRs from specific repositories
        ignore_repos = get_settings().get("CONFIG.IGNORE_REPOSITORIES", [])
        if repo_full_name and ignore_repos:
            if any(re.search(regex, repo_full_name) for regex in ignore_repos):
                get_logger().info(
                    f"Ignoring PR from repository '{repo_full_name}' due to 'config.ignore_repositories' setting"
                )
                return False

        # Ignore PRs from specific users
        ignore_pr_users = get_settings().get("CONFIG.IGNORE_PR_AUTHORS", [])
        if sender and ignore_pr_users:
            if any(re.search(regex, sender) for regex in ignore_pr_users):
                get_logger().info(f"Ignoring PR from user '{sender}' due to 'config.ignore_pr_authors' setting")
                return False

        # Ignore PRs with specific titles
        if title:
            ignore_pr_title_re = get_settings().get("CONFIG.IGNORE_PR_TITLE", [])
            if not isinstance(ignore_pr_title_re, list):
                ignore_pr_title_re = [ignore_pr_title_re]
            if ignore_pr_title_re and any(re.search(regex, title) for regex in ignore_pr_title_re):
                get_logger().info(f"Ignoring PR with title '{title}' due to config.ignore_pr_title setting")
                return False

        # Ignore PRs with specific labels
        ignore_pr_labels = get_settings().get("CONFIG.IGNORE_PR_LABELS", [])
        if labels and ignore_pr_labels:
            if any(label in ignore_pr_labels for label in labels):
                labels_str = ", ".join(labels)
                get_logger().info(f"Ignoring PR with labels '{labels_str}' due to config.ignore_pr_labels settings")
                return False

        # Ignore PRs with specific source or target branches
        ignore_pr_source_branches = get_settings().get("CONFIG.IGNORE_PR_SOURCE_BRANCHES", [])
        ignore_pr_target_branches = get_settings().get("CONFIG.IGNORE_PR_TARGET_BRANCHES", [])
        if ignore_pr_source_branches or ignore_pr_target_branches:
            if source_branch and any(re.search(regex, source_branch) for regex in ignore_pr_source_branches):
                get_logger().info(
                    f"Ignoring PR with source branch '{source_branch}' due to config.ignore_pr_source_branches settings"
                )
                return False
            if target_branch and any(re.search(regex, target_branch) for regex in ignore_pr_target_branches):
                get_logger().info(
                    f"Ignoring PR with target branch '{target_branch}' due to config.ignore_pr_target_branches settings"
                )
                return False
    except Exception as e:
        get_logger().error(f"Failed 'should_process_pr_logic': {e}")
        if raise_on_error:
            raise
        return True
    return True


# Re-export should_process_pr_logic for direct server imports
shared_should_process_pr_logic = should_process_pr_logic
