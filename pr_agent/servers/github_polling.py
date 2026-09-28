import asyncio
import copy
import math
import multiprocessing
import re
import traceback
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit

import aiohttp
from starlette_context import request_cycle_context

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.ai_handlers.litellm_helpers import (
    DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    drain_litellm_callbacks,
    litellm_callbacks_registered,
)
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers import get_git_provider
from pr_agent.log import LoggingFormat, get_logger, setup_logger

setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
NOTIFICATION_URL = "https://api.github.com/notifications"
DEFAULT_POLLING_REQUEST_TIMEOUT = 10
MAX_POLLING_REQUEST_TIMEOUT = 60
POLLING_CAPACITY_CHECK_INTERVAL = 0.25
POLLING_COMMENT_SCAN_LIMIT = 4


class _PollingWorkerStartError(RuntimeError):
    """Stop dispatch when child startup leaves process state uncertain."""


class _InvalidPaginationMetadata(ValueError):
    """Reject untrusted pagination metadata without retaining its contents."""


def _get_polling_request_timeout() -> float:
    """Bound the timeout for notification fallback requests."""
    value = global_settings.get("github.polling_request_timeout", DEFAULT_POLLING_REQUEST_TIMEOUT)
    try:
        timeout = float(value) if not isinstance(value, bool) else 0.0
    except (TypeError, ValueError, OverflowError):
        timeout = 0.0
    if not math.isfinite(timeout) or timeout <= 0:
        get_logger().warning(
            f"Invalid github.polling_request_timeout; using {DEFAULT_POLLING_REQUEST_TIMEOUT} seconds"
        )
        return float(DEFAULT_POLLING_REQUEST_TIMEOUT)
    if timeout > MAX_POLLING_REQUEST_TIMEOUT:
        get_logger().warning(f"Capping github.polling_request_timeout at {MAX_POLLING_REQUEST_TIMEOUT} seconds")
    return min(timeout, float(MAX_POLLING_REQUEST_TIMEOUT))


def _split_link_header(value: str, separator: str) -> list[str]:
    """Split a Link header outside URI references and quoted strings."""
    parts = []
    start = 0
    in_uri = False
    in_quote = False
    escaped = False
    for index, character in enumerate(value):
        if escaped:
            escaped = False
        elif in_quote and character == "\\":
            escaped = True
        elif character == '"':
            in_quote = not in_quote
        elif not in_quote and character == "<":
            if in_uri:
                raise _InvalidPaginationMetadata("Invalid pagination metadata")
            in_uri = True
        elif not in_quote and character == ">":
            if not in_uri:
                raise _InvalidPaginationMetadata("Invalid pagination metadata")
            in_uri = False
        elif character == separator and not in_uri and not in_quote:
            part = value[start:index].strip()
            if not part:
                raise _InvalidPaginationMetadata("Invalid pagination metadata")
            parts.append(part)
            start = index + 1
    if escaped or in_uri or in_quote:
        raise _InvalidPaginationMetadata("Invalid pagination metadata")
    part = value[start:].strip()
    if not part:
        raise _InvalidPaginationMetadata("Invalid pagination metadata")
    parts.append(part)
    return parts


def _parse_link_headers(values: list[str]) -> dict[str, str]:
    """Parse Link relationships strictly without exposing supplied targets."""
    relationships = {}
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise _InvalidPaginationMetadata("Invalid pagination metadata")
        for entry in _split_link_header(value, ","):
            if not entry.startswith("<"):
                raise _InvalidPaginationMetadata("Invalid pagination metadata")
            closing = entry.find(">")
            if closing <= 1:
                raise _InvalidPaginationMetadata("Invalid pagination metadata")
            target = entry[1:closing]
            remainder = entry[closing + 1:].strip()
            if not remainder.startswith(";"):
                raise _InvalidPaginationMetadata("Invalid pagination metadata")
            parameters = _split_link_header(remainder[1:], ";")
            rel_values = []
            for parameter in parameters:
                if "=" not in parameter:
                    raise _InvalidPaginationMetadata("Invalid pagination metadata")
                name, raw_value = (part.strip() for part in parameter.split("=", 1))
                if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                    raise _InvalidPaginationMetadata("Invalid pagination metadata")
                if raw_value.startswith('"'):
                    if len(raw_value) < 2 or not raw_value.endswith('"'):
                        raise _InvalidPaginationMetadata("Invalid pagination metadata")
                    inner = raw_value[1:-1]
                    parameter_characters = []
                    index = 0
                    while index < len(inner):
                        character = inner[index]
                        if character == '"' or ord(character) < 0x20 or ord(character) == 0x7f:
                            raise _InvalidPaginationMetadata("Invalid pagination metadata")
                        if character == "\\":
                            index += 1
                            if index == len(inner):
                                raise _InvalidPaginationMetadata("Invalid pagination metadata")
                            character = inner[index]
                        parameter_characters.append(character)
                        index += 1
                    parameter_value = "".join(parameter_characters)
                elif re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", raw_value):
                    parameter_value = raw_value
                else:
                    raise _InvalidPaginationMetadata("Invalid pagination metadata")
                if name.lower() == "rel":
                    rel_values.extend(parameter_value.split())
            if not rel_values:
                raise _InvalidPaginationMetadata("Invalid pagination metadata")
            for relationship in rel_values:
                relationship = relationship.lower()
                if relationship in relationships:
                    raise _InvalidPaginationMetadata("Invalid pagination metadata")
                relationships[relationship] = target
    return relationships


def _response_link_headers(response) -> list[str]:
    headers = response.headers
    getall = getattr(headers, "getall", None)
    if getall is not None:
        return list(getall("Link", []))
    value = headers.get("Link")
    return [] if value is None else [value]


def _effective_port(parts) -> int | None:
    try:
        if parts.port is not None:
            return parts.port
    except ValueError:
        raise _InvalidPaginationMetadata("Invalid pagination metadata") from None
    return {"http": 80, "https": 443}.get(parts.scheme.lower())


def _comment_resource(url: str) -> tuple:
    try:
        parts = urlsplit(url)
        has_userinfo = parts.username is not None or parts.password is not None
        origin = (parts.scheme.lower(), parts.hostname.lower() if parts.hostname else None, _effective_port(parts))
    except (ValueError, UnicodeError):
        raise ValueError("Invalid comment history URL") from None
    if not parts.scheme or not parts.hostname or has_userinfo or parts.fragment:
        raise ValueError("Invalid comment history URL")
    issue_match = re.fullmatch(r"/repos/[^/]+/[^/]+/issues/([1-9][0-9]*)/comments", parts.path)
    if issue_match is None:
        issue_match = re.fullmatch(r"/repositories/[1-9][0-9]*/issues/([1-9][0-9]*)/comments", parts.path)
    if issue_match is None:
        raise ValueError("Invalid comment history URL")
    return parts, origin, issue_match.group(1)


def _pagination_page(target: str, initial_parts, initial_origin: tuple, issue_number: str) -> int:
    try:
        parts = urlsplit(target)
        has_userinfo = parts.username is not None or parts.password is not None
        origin = (parts.scheme.lower(), parts.hostname.lower() if parts.hostname else None, _effective_port(parts))
    except (ValueError, UnicodeError):
        raise _InvalidPaginationMetadata("Invalid pagination metadata") from None
    if (not parts.scheme or not parts.hostname or has_userinfo or parts.fragment or origin != initial_origin):
        raise _InvalidPaginationMetadata("Invalid pagination metadata")
    numeric_alias = re.fullmatch(
        rf"/repositories/[1-9][0-9]*/issues/{re.escape(issue_number)}/comments", parts.path
    )
    if parts.path != initial_parts.path and numeric_alias is None:
        raise _InvalidPaginationMetadata("Invalid pagination metadata")
    try:
        page_values = [value for name, value in parse_qsl(parts.query, keep_blank_values=True) if name == "page"]
    except ValueError:
        raise _InvalidPaginationMetadata("Invalid pagination metadata") from None
    if len(page_values) != 1 or re.fullmatch(r"[1-9][0-9]*", page_values[0]) is None:
        raise _InvalidPaginationMetadata("Invalid pagination metadata")
    try:
        return int(page_values[0])
    except ValueError:
        raise _InvalidPaginationMetadata("Invalid pagination metadata") from None


def _pagination_pages(relationships: dict[str, str], initial_parts, initial_origin: tuple,
                      issue_number: str) -> dict[str, int]:
    return {
        relationship: _pagination_page(relationships[relationship], initial_parts, initial_origin, issue_number)
        for relationship in ("next", "last", "prev")
        if relationship in relationships
    }


def _remaining_polling_timeout(deadline: float) -> float:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        raise asyncio.TimeoutError("Comment history request timed out")
    return remaining


async def _fetch_comment_page(session, url, headers, deadline: float, *, page: int | None,
                              allow_redirects: bool) -> tuple[list, list[str]]:
    params = {"per_page": POLLING_COMMENT_SCAN_LIMIT}
    if page is not None:
        params["page"] = page
    async with session.get(
        url,
        headers=headers,
        params=params,
        timeout=aiohttp.ClientTimeout(total=_remaining_polling_timeout(deadline)),
        allow_redirects=allow_redirects,
        max_redirects=30,
    ) as response:
        if not 200 <= response.status < 300:
            raise aiohttp.ClientResponseError(
                response.request_info,
                response.history,
                status=response.status,
                message="Unexpected comment history response status",
                headers=response.headers,
            )
        response.raise_for_status()
        comments = await response.json(content_type=None)
        link_headers = _response_link_headers(response)
        _remaining_polling_timeout(deadline)
    if not isinstance(comments, list):
        raise ValueError("Expected a list of pull request comments")
    return comments, link_headers


async def _fetch_comment_history(session, url, headers) -> list:
    """Fetch the newest bounded fallback tail without trusting Link targets."""
    initial_parts, initial_origin, issue_number = _comment_resource(url)
    deadline = asyncio.get_running_loop().time() + _get_polling_request_timeout()
    comments, link_headers = await _fetch_comment_page(
        session, url, headers, deadline, page=None, allow_redirects=True
    )
    if not link_headers:
        return comments[-POLLING_COMMENT_SCAN_LIMIT:]

    relationships = _parse_link_headers(link_headers)
    pages = _pagination_pages(relationships, initial_parts, initial_origin, issue_number)
    _remaining_polling_timeout(deadline)
    if "next" not in pages:
        if "prev" in pages or ("last" in pages and pages["last"] != 1):
            raise _InvalidPaginationMetadata("Inconsistent pagination metadata")
        return comments[-POLLING_COMMENT_SCAN_LIMIT:]
    if "last" not in pages or pages["next"] != 2 or pages["last"] < pages["next"] or "prev" in pages:
        raise _InvalidPaginationMetadata("Inconsistent pagination metadata")

    last_page = pages["last"]
    last_comments, last_link_headers = await _fetch_comment_page(
        session, url, headers, deadline, page=last_page, allow_redirects=False
    )
    last_relationships = _parse_link_headers(last_link_headers) if last_link_headers else {}
    last_pages = _pagination_pages(last_relationships, initial_parts, initial_origin, issue_number)
    _remaining_polling_timeout(deadline)
    if "next" in last_pages or ("last" in last_pages and last_pages["last"] != last_page):
        raise _InvalidPaginationMetadata("Inconsistent pagination metadata")
    if len(last_comments) >= POLLING_COMMENT_SCAN_LIMIT:
        return last_comments[-POLLING_COMMENT_SCAN_LIMIT:]

    expected_previous = last_page - 1
    if expected_previous < 1 or last_pages.get("prev") != expected_previous:
        raise _InvalidPaginationMetadata("Inconsistent pagination metadata")
    previous_comments, previous_link_headers = await _fetch_comment_page(
        session, url, headers, deadline, page=expected_previous, allow_redirects=False
    )
    previous_relationships = _parse_link_headers(previous_link_headers) if previous_link_headers else {}
    previous_pages = _pagination_pages(previous_relationships, initial_parts, initial_origin, issue_number)
    _remaining_polling_timeout(deadline)
    if (("next" in previous_pages and previous_pages["next"] != last_page)
            or ("last" in previous_pages and previous_pages["last"] != last_page)):
        raise _InvalidPaginationMetadata("Inconsistent pagination metadata")
    return (previous_comments + last_comments)[-POLLING_COMMENT_SCAN_LIMIT:]


async def mark_notification_as_read(headers, notification, session):
    async with session.patch(
            f"https://api.github.com/notifications/threads/{notification['id']}",
            headers=headers) as mark_read_response:
        if mark_read_response.status != 205:
            get_logger().error(
                f"Failed to mark notification as read. Status code: {mark_read_response.status}")


def now() -> str:
    """
    Get the current UTC time in ISO 8601 format.

    Returns:
        str: The current UTC time in ISO 8601 format.
    """
    now_utc = datetime.now(timezone.utc).isoformat()
    now_utc = now_utc.replace("+00:00", "Z")
    return now_utc

async def async_handle_request(pr_url, rest_of_comment, comment_id, git_provider):
    agent = PRAgent()
    success = await agent.handle_request(
        pr_url,
        rest_of_comment,
        notify=lambda: git_provider.add_eyes_reaction(comment_id)
    )
    return success

async def _handle_request_and_drain(pr_url, rest_of_comment, comment_id, git_provider):
    """
    Run the request, then flush litellm's deferred callbacks before the loop closes.

    This runs in a short-lived child process, so asyncio.run() below tears the loop
    down and the process exits immediately afterwards - without the drain the last
    completion's callback is dropped.
    """
    try:
        return await async_handle_request(pr_url, rest_of_comment, comment_id, git_provider)
    finally:
        if litellm_callbacks_registered():
            await drain_litellm_callbacks(
                get_settings().litellm.get("callback_timeout_seconds", DEFAULT_CALLBACK_TIMEOUT_SECONDS)
            )


def run_handle_request(pr_url, rest_of_comment, comment_id, git_provider):
    return asyncio.run(_handle_request_and_drain(pr_url, rest_of_comment, comment_id, git_provider))


def _polling_request_settings():
    """Clone global settings with the polling-mode overrides applied.

    Applying the overrides here instead of in ``polling_loop`` makes them reach
    the task regardless of the multiprocessing start method: fork children
    inherit the parent's globals, spawn/forkserver children do not - and
    forkserver is the Linux default from Python 3.14.
    """
    settings = copy.deepcopy(global_settings)
    settings.set("CONFIG.PUBLISH_OUTPUT_PROGRESS", False)
    settings.set("pr_description.publish_description_as_comment", True)
    return settings


@contextmanager
def _polling_settings_scope():
    """request_cycle_context with a guaranteed reset: its bare yield (unfixed
    upstream as of starlette-context 0.5.1) skips the ContextVar reset when an
    exception crosses the with-body, so enter and exit are driven explicitly
    and the reset runs on any exit, BaseException included.
    """
    cm = request_cycle_context({"settings": _polling_request_settings()})
    cm.__enter__()
    try:
        yield
    finally:
        cm.__exit__(None, None, None)


def process_comment_sync(pr_url, rest_of_comment, comment_id):
    try:
        with _polling_settings_scope():
            # Run the async handle_request in a separate function
            git_provider = get_git_provider()(pr_url=pr_url)
            run_handle_request(pr_url, rest_of_comment, comment_id, git_provider)
    except Exception as e:
        get_logger().error(f"Error processing comment: {e}", artifact={"traceback": traceback.format_exc()})


async def process_comment(pr_url, rest_of_comment, comment_id):
    try:
        with _polling_settings_scope():
            git_provider = get_git_provider()(pr_url=pr_url)
            git_provider.set_pr(pr_url)
            agent = PRAgent()
            await agent.handle_request(
                pr_url,
                rest_of_comment,
                notify=lambda: git_provider.add_eyes_reaction(comment_id)
            )
        get_logger().info(f"Finished processing comment for PR: {pr_url}")
    except Exception as e:
        get_logger().error(f"Error processing comment: {e}", artifact={"traceback": traceback.format_exc()})

async def is_valid_notification(notification, headers, handled_ids, session, user_id):
    try:
        if 'reason' in notification and notification['reason'] == 'mention':
            if 'subject' in notification and notification['subject']['type'] == 'PullRequest':
                pr_url = notification['subject']['url']
                latest_comment = notification['subject']['latest_comment_url']
                if not latest_comment or not isinstance(latest_comment, str):
                    get_logger().debug("no latest_comment")
                    return False, handled_ids
                async with session.get(latest_comment, headers=headers) as comment_response:
                    check_prev_comments = False
                    user_tag = "@" + user_id
                    if comment_response.status == 200:
                        comment = await comment_response.json()
                        if 'id' in comment:
                            if comment['id'] in handled_ids:
                                get_logger().debug("comment['id'] in handled_ids")
                                return False, handled_ids
                            else:
                                handled_ids.add(comment['id'])
                        if 'user' in comment and 'login' in comment['user']:
                            if comment['user']['login'] == user_id:
                                get_logger().debug("comment['user']['login'] == user_id")
                                check_prev_comments = True
                        comment_body = comment.get('body', '')
                        if not comment_body:
                            get_logger().debug("no comment_body")
                            check_prev_comments = True
                        else:
                            if user_tag not in comment_body:
                                get_logger().debug("user_tag not in comment_body")
                                check_prev_comments = True
                            else:
                                get_logger().info(f"Polling, pr_url: {pr_url}",
                                                  artifact={"comment": comment_body})

                        if not check_prev_comments:
                            return True, handled_ids, comment, comment_body, pr_url, user_tag
                        else: # we could not find the user tag in the latest comment. Check previous comments
                            # get all comments in the PR
                            requests_url = f"{pr_url}/comments".replace("pulls", "issues")
                            try:
                                comments = (await _fetch_comment_history(session, requests_url, headers))[::-1]
                            except _InvalidPaginationMetadata:
                                get_logger().warning(
                                    f"Ignoring invalid comment pagination metadata for PR: {pr_url}"
                                )
                                return False, handled_ids
                            for comment in comments[:POLLING_COMMENT_SCAN_LIMIT]:
                                if 'user' in comment and 'login' in comment['user']:
                                    if comment['user']['login'] == user_id:
                                        continue
                                comment_body = comment.get('body', '')
                                if not comment_body:
                                    continue
                                if user_tag in comment_body:
                                    get_logger().info("found user tag in previous comments")
                                    get_logger().info(f"Polling, pr_url: {pr_url}",
                                                      artifact={"comment": comment_body})
                                    return True, handled_ids, comment, comment_body, pr_url, user_tag

                            get_logger().warning(f"Failed to fetch comments for PR: {pr_url}",
                                                    artifact={"comments": comments})
                            return False, handled_ids

        return False, handled_ids
    except Exception as e:
        get_logger().exception("Error processing polling notification",
                               artifact={"notification": notification, "error": e})
        return False, handled_ids


def _reap_finished_processes(active_processes):
    """Release completed workers without waiting for live ones."""
    for process in active_processes[:]:
        if not process.is_alive():
            process.join(timeout=0)
            process.close()
            active_processes.remove(process)


async def _start_queued_processes(task_queue, max_allowed_parallel_tasks, active_processes):
    """Keep the batch limit, waiting for capacity shared across polling iterations."""
    if max_allowed_parallel_tasks <= 0:
        raise ValueError("The polling process limit must be positive")
    overflow = len(task_queue) - max_allowed_parallel_tasks
    if overflow > 0:
        get_logger().error(f"Dropping {overflow} tasks from polling session")
        for _ in range(overflow):
            task_queue.pop()

    waiting_logged = False
    try:
        while task_queue:
            _reap_finished_processes(active_processes)
            if len(active_processes) >= max_allowed_parallel_tasks:
                if not waiting_logged:
                    get_logger().info(
                        f"Polling dispatch waiting for capacity: {len(active_processes)} workers active, "
                        f"{len(task_queue)} tasks queued"
                    )
                    waiting_logged = True
                await asyncio.sleep(POLLING_CAPACITY_CHECK_INTERVAL)
                continue
            func, args = task_queue[0]
            process = multiprocessing.Process(target=func, args=args)
            try:
                process.start()
            except BaseException as exc:
                # Treat a PID-bearing child as potentially dispatched; never retry its task.
                if process.pid is not None:
                    active_processes.append(process)
                    task_queue.popleft()
                else:
                    process.close()
                if isinstance(exc, Exception):
                    raise _PollingWorkerStartError("Polling worker startup failed; stopping dispatch") from exc
                raise
            active_processes.append(process)
            task_queue.popleft()
    finally:
        if task_queue:
            get_logger().error(f"Polling dispatch stopped with {len(task_queue)} tasks not dispatched")


async def polling_loop():
    """
    Polls for notifications and handles them accordingly.
    """
    handled_ids = set()
    since = [now()]
    last_modified = [None]
    git_provider = get_git_provider()()
    user_id = git_provider.get_user_id()

    try:
        deployment_type = get_settings().github.deployment_type
        token = get_settings().github.user_token
    except AttributeError:
        deployment_type = 'none'
        token = None

    if deployment_type != 'user':
        raise ValueError("Deployment mode must be set to 'user' to get notifications")
    if not token:
        raise ValueError("User token must be set to get notifications")

    active_processes = []
    async with aiohttp.ClientSession() as session:
        while True:
            task_queue = deque()
            dispatch_started = False
            try:
                await asyncio.sleep(5)
                headers = {
                    "Accept": "application/vnd.github.v3+json",
                    "Authorization": f"Bearer {token}"
                }
                params = {
                    "participating": "true"
                }
                if since[0]:
                    params["since"] = since[0]
                if last_modified[0]:
                    headers["If-Modified-Since"] = last_modified[0]

                async with session.get(NOTIFICATION_URL, headers=headers, params=params) as response:
                    if response.status == 200:
                        if 'Last-Modified' in response.headers:
                            last_modified[0] = response.headers['Last-Modified']
                            since[0] = None
                        notifications = await response.json()
                        if not notifications:
                            continue
                        get_logger().info(f"Received {len(notifications)} notifications")
                        for notification in notifications:
                            if not notification:
                                continue
                            # mark notification as read
                            await mark_notification_as_read(headers, notification, session)

                            handled_ids.add(notification['id'])
                            output = await is_valid_notification(notification, headers, handled_ids, session, user_id)
                            if output[0]:
                                _, handled_ids, comment, comment_body, pr_url, user_tag = output
                                rest_of_comment = comment_body.split(user_tag)[1].strip()
                                comment_id = comment['id']

                                # Add to the task queue
                                get_logger().info(
                                    f"Adding comment processing to task queue for PR, {pr_url},"
                                    f" comment_body: {comment_body}")
                                task_queue.append((process_comment_sync, (pr_url, rest_of_comment, comment_id)))
                                get_logger().info(f"Queued comment processing for PR: {pr_url}")
                            else:
                                get_logger().debug("Skipping comment processing for PR")

                        max_allowed_parallel_tasks = 10
                        if task_queue:
                            dispatch_started = True
                            await _start_queued_processes(task_queue, max_allowed_parallel_tasks, active_processes)

                    elif response.status != 304:
                        print(f"Failed to fetch notifications. Status code: {response.status}")

            except _PollingWorkerStartError:
                raise
            except Exception as e:
                get_logger().error(f"Polling exception during processing of a notification: {e}",
                                   artifact={"traceback": traceback.format_exc()})
            finally:
                if task_queue and not dispatch_started:
                    get_logger().error(f"Polling dispatch stopped with {len(task_queue)} tasks not dispatched")
                _reap_finished_processes(active_processes)


if __name__ == '__main__':
    asyncio.run(polling_loop())
