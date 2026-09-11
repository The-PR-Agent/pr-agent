import asyncio
import hashlib
import hmac
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
