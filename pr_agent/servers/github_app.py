import copy
import os
import time
import uuid
from typing import Any, Dict, Optional, Tuple

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette_context import context
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent, prepare_command
from pr_agent.algo.run_details import command_failed, init_run_details
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers import get_git_provider, get_git_provider_with_context
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.identity_providers import get_identity_provider
from pr_agent.identity_providers.identity_provider import Eligibility
from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.servers.utils import (
    DefaultDictWithTimeout,
    get_pr_commands,
    push_trigger_slot,
    shared_should_process_pr_logic,
    verify_signature,
)
from pr_agent.telemetry.prometheus import attach_metrics_endpoint, prometheus_metrics_enabled

setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
base_path = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
build_number_path = os.path.join(base_path, "build_number.txt")
if os.path.exists(build_number_path):
    with open(build_number_path) as f:
        build_number = f.read().strip()
else:
    build_number = "unknown"
router = APIRouter()
_WEBHOOK_DELIVERY_TTL = get_settings().github_app.push_trigger_pending_tasks_ttl
_completed_webhook_deliveries = DefaultDictWithTimeout(
    float,
    ttl=_WEBHOOK_DELIVERY_TTL,
    update_key_time_on_get=False,
)


@router.post("/api/v1/github_webhooks")
async def handle_github_webhooks(background_tasks: BackgroundTasks, request: Request, response: Response):
    """
    Receives and processes incoming GitHub webhook requests.
    Verifies the request signature, parses the request body, and passes it to the handle_request function for further
    processing.
    """
    get_logger().debug("Received a GitHub webhook")

    body = await get_body(request)

    installation_id = body.get("installation", {}).get("id")
    context["installation_id"] = installation_id
    context["settings"] = copy.deepcopy(global_settings)
    context["git_provider"] = {}
    background_tasks.add_task(
        handle_request,
        body,
        event=request.headers.get("X-GitHub-Event", None),
        delivery_id=request.headers.get("X-GitHub-Delivery", None),
    )
    return {}


@router.post("/api/v1/marketplace_webhooks")
async def handle_marketplace_webhooks(request: Request, response: Response):
    # Marketplace webhooks do not carry the same x-hub-signature-256 header as
    # PR webhooks, so they bypass the signature check in get_body. The handler
    # is intentionally a no-op aside from logging: it never publishes comments,
    # mutates PR state, or triggers AI calls, so a forged request can only
    # poison logs.
    try:
        # Parse the JSON body so a malformed payload returns HTTP 400, but the
        # handler is a no-op aside from logging - assign to _ to satisfy Ruff
        # F841 (unused-local) without losing the validation side effect.
        _ = await request.json()
    except Exception as e:
        get_logger().error("Error parsing marketplace request body", artifact={"error": e})
        raise HTTPException(status_code=400, detail="Error parsing request body") from e
    get_logger().info("Marketplace request body received")


async def get_body(request):
    try:
        body_bytes = await request.body()
    except Exception as e:
        get_logger().error("Error reading request body", artifact={"error": e})
        raise HTTPException(status_code=400, detail="Error reading request body") from e
    try:
        body = await request.json()
    except Exception as e:
        get_logger().error("Error parsing request body", artifact={"error": e})
        raise HTTPException(status_code=400, detail="Error parsing request body") from e
    webhook_secret = getattr(get_settings().github, 'webhook_secret', None)
    if not webhook_secret:
        # Refuse unauthenticated webhooks. Silently accepting requests when
        # the secret is not configured used to allow any internet caller to
        # trigger expensive AI commands against arbitrary PRs.
        get_logger().error("Rejecting GitHub webhook: GITHUB.WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=403, detail="Webhook secret not configured")
    signature_header = request.headers.get('x-hub-signature-256', None)
    verify_signature(body_bytes, webhook_secret, signature_header)
    return body


def _reformat_quote_ask_command(comment_body: str) -> Optional[str]:
    """Move a /ask command buried in a quoted Golf/mobile reply to the front so it
    is dispatched, preserving the whole question text. Returns None when the
    comment is not an image-quote reply carrying a /ask."""
    if '/ask' not in comment_body or not comment_body.strip().startswith('> ![image]'):
        return None
    before, _, after = comment_body.partition('/ask')
    return '/ask' + after + ' \n' + before.strip().lstrip('>')


async def handle_comments_on_pr(body: Dict[str, Any],
                                event: str,
                                sender: str,
                                sender_id: str,
                                action: str,
                                log_context: Dict[str, Any],
                                agent: PRAgent):
    if "comment" not in body:
        return {}
    comment_body = body.get("comment", {}).get("body")
    if comment_body and isinstance(comment_body, str) and not comment_body.lstrip().startswith("/"):
        reformatted = _reformat_quote_ask_command(comment_body) if '/ask' in comment_body else None
        if reformatted is not None:
            comment_body = reformatted
            get_logger().info(f"Reformatting comment_body so command is at the beginning: {comment_body}")
        else:
            get_logger().info("Ignoring comment not starting with /")
            return {}
    disable_eyes = False
    if "issue" in body and "pull_request" in body["issue"] and "url" in body["issue"]["pull_request"]:
        api_url = body["issue"]["pull_request"]["url"]
    elif "comment" in body and "pull_request_url" in body["comment"]:
        api_url = body["comment"]["pull_request_url"]
        try:
            if ('/ask' in comment_body and
                    'subject_type' in body["comment"] and body["comment"]["subject_type"] == "line"):
                # comment on a code line in the "files changed" tab
                comment_body = handle_line_comments(body, comment_body)
                disable_eyes = True
        except Exception as e:
            get_logger().error("Failed to get log context", artifact={'error': e})
    else:
        return {}
    log_context["api_url"] = api_url
    comment_id = body.get("comment", {}).get("id")
    provider = get_git_provider_with_context(pr_url=api_url)
    if isinstance(sender, str) and sender and hasattr(provider, "set_command_actor"):
        provider.set_command_actor(sender)
    with get_logger().contextualize(**log_context):
        if get_identity_provider().verify_eligibility("github", sender_id, api_url) is not Eligibility.NOT_ELIGIBLE:
            get_logger().info(f"Processing comment on PR {api_url=}, comment_body={comment_body}")
            succeeded = await agent.handle_request(
                api_url, comment_body,
                notify=lambda: provider.add_eyes_reaction(comment_id, disable_eyes=disable_eyes),
                # Preserve compatibility mode while exposing failures to outcome reactions.
                propagate_tool_errors=True)
            # Optional, and disabled by default: tell the author how the command ended without
            # adding another comment to the thread.
            provider.react_to_outcome(comment_id, bool(succeeded))
            return succeeded
        else:
            get_logger().info(f"User {sender=} is not eligible to process comment on PR {api_url=}")

async def handle_new_pr_opened(body: Dict[str, Any],
                               event: str,
                               sender: str,
                               sender_id: str,
                               action: str,
                               log_context: Dict[str, Any],
                               agent: PRAgent):

    pull_request, api_url = _check_pull_request_event(action, body, log_context)
    if not (pull_request and api_url):
        get_logger().info(f"Invalid PR event: {action=} {api_url=}")
        return {}
    if action in get_settings().github_app.handle_pr_actions:  # ['opened', 'reopened', 'ready_for_review']
        # logic to ignore PRs with specific titles (e.g. "[Auto] ...")
        apply_repo_settings(api_url)
        if get_identity_provider().verify_eligibility("github", sender_id, api_url) is not Eligibility.NOT_ELIGIBLE:
            return await _perform_auto_commands_github("pr_commands", agent, body, api_url, log_context)
        else:
            get_logger().info(f"User {sender=} is not eligible to process PR {api_url=}")


# The check run each automatic command publishes its output to when `github.publish_as_check_run`
# is on, keyed by command and valued by the `name` the tool passes to `_publish_check_run`.
_AUTO_COMMAND_CHECK_RUNS = {"describe": "describe", "review": "review", "improve": "suggestions"}


def _check_run_provider(api_url: str):
    """The provider whose check runs the automatic commands complete, or None when they publish none."""
    if not get_settings().get("github.publish_as_check_run", False):
        return None
    try:
        provider = get_git_provider_with_context(pr_url=api_url)
    except Exception as e:
        get_logger().warning(f"Cannot open check runs for {api_url=}: {e}")
        return None
    return provider if callable(getattr(provider, "start_check_run", None)) else None


def _auto_command_check_run(command) -> tuple[str, str | None]:
    """The command's leading token and the check run its tool publishes to, if any."""
    tokens = command if isinstance(command, list) else str(command).split()
    action = tokens[0] if tokens else ""
    return action, _AUTO_COMMAND_CHECK_RUNS.get(action.lstrip("/").lower())


def _start_auto_command_check_run(provider, command) -> str | None:
    """Open the command's check run as in_progress: the first sign the pull request was picked up."""
    action, name = _auto_command_check_run(command)
    if provider is None or name is None:
        return None
    try:
        provider.start_check_run(name, f"PR-Agent is running {action}")
    except Exception as e:
        get_logger().warning(f"Failed to open the {name} check run: {e}")
    return name


def _finish_auto_command_check_run(provider, name: str | None, command, succeeded: bool) -> None:
    """Complete the command's check run when its tool did not, so it cannot stay in_progress."""
    if provider is None or name is None:
        return
    action, _ = _auto_command_check_run(command)
    summary = f"PR-Agent ran {action}" if succeeded else f"PR-Agent could not finish {action}"
    try:
        provider.finish_check_run(name, "success" if succeeded else "failure", summary)
    except Exception as e:
        get_logger().warning(f"Failed to complete the {name} check run: {e}")


def _normalise_setting_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def matches_review_state(review_state: Any, configured_states: Any) -> bool:
    """Return whether a review state matches one of the configured states."""
    if not isinstance(review_state, str) or not review_state.strip():
        return False
    configured_states = _normalise_setting_list(configured_states)
    if not configured_states:
        return False
    normalized_state = review_state.strip().lower()
    return any(
        isinstance(state, str) and state.strip().lower() == normalized_state
        for state in configured_states
    )


async def handle_pull_request_review_submitted(body: Dict[str, Any],
                                               event: str,
                                               sender: str,
                                               sender_id: str,
                                               sender_type: str,
                                               action: str,
                                               log_context: Dict[str, Any],
                                               agent: PRAgent):
    pull_request, api_url = _check_pull_request_event(action, body, log_context)
    if not (pull_request and api_url):
        get_logger().info(f"Invalid PR review event: {action=} {api_url=}")
        return {}
    if sender_type == "Bot":
        get_logger().info(f"Skipping pull_request_review from bot sender: {sender=}")
        return {}

    apply_repo_settings(api_url)
    review = body.get("review", {})
    review_state = review.get("state", "") if isinstance(review, dict) else ""
    review_author_type = ""
    if isinstance(review, dict):
        review_author = review.get("user", {})
        if isinstance(review_author, dict):
            review_author_type = str(review_author.get("type", "")).strip().lower()
    review_author_types = {
        str(author_type).strip().lower()
        for author_type in _normalise_setting_list(
            get_settings().get("GITHUB_APP.REVIEW_AUTHOR_TYPES", ["User"])
        )
        if str(author_type).strip()
    }
    if review_author_type not in review_author_types:
        get_logger().info(
            f"Skipping review submission from {review_author_type=}: author type is not configured"
        )
        return {}
    if not matches_review_state(
        review_state, get_settings().get("GITHUB_APP.REVIEW_STATES", ["changes_requested"])
    ):
        get_logger().info(f"Skipping review submission with {review_state=}: state is not configured")
        return {}

    if get_identity_provider().verify_eligibility("github", sender_id, api_url) is not Eligibility.NOT_ELIGIBLE:
        return await _perform_auto_commands_github("review_commands", agent, body, api_url, log_context)
    else:
        get_logger().info(f"User {sender=} is not eligible to process review on PR {api_url=}")


async def handle_push_trigger_for_new_commits(body: Dict[str, Any],
                        event: str,
                        sender: str,
                        sender_id: str,
                        action: str,
                        log_context: Dict[str, Any],
                        agent: PRAgent):
    pull_request, api_url = _check_pull_request_event(action, body, log_context)
    if not (pull_request and api_url):
        return {}

    # we need to apply the repo settings to get the correct settings for the PR.
    # This is quite expensive - a call to the git provider is made for each PR event.
    apply_repo_settings(api_url)
    if not get_settings().github_app.handle_push_trigger:
        return {}

    # TODO: do we still want to get the list of commits to filter bot/merge commits?
    before_sha = body.get("before")
    after_sha = body.get("after")
    merge_commit_sha = pull_request.get("merge_commit_sha")
    if before_sha == after_sha:
        return {}
    if get_settings().github_app.push_trigger_ignore_merge_commits and after_sha == merge_commit_sha:
        return {}

    async with push_trigger_slot(
        api_url,
        allow_backlog=get_settings().github_app.push_trigger_pending_tasks_backlog,
        ttl=get_settings().github_app.push_trigger_pending_tasks_ttl,
    ) as proceed:
        if not proceed:
            return {}
        if get_identity_provider().verify_eligibility("github", sender_id, api_url) is not Eligibility.NOT_ELIGIBLE:
            get_logger().info(f"Performing incremental review for {api_url=} because of {event=} and {action=}")
            return await _perform_auto_commands_github("push_commands", agent, body, api_url, log_context)


def handle_closed_pr(body, event, action, log_context):
    pull_request = body.get("pull_request", {})
    is_merged = pull_request.get("merged", False)
    if not is_merged:
        return
    api_url = pull_request.get("url", "")
    pr_statistics = get_git_provider()(pr_url=api_url).calc_pr_statistics(pull_request)
    log_context["api_url"] = api_url
    get_logger().info("PR-Agent statistics for closed PR", analytics=True, pr_statistics=pr_statistics, **log_context)


def get_log_context(body, event, action, build_number):
    sender = ""
    sender_id = ""
    sender_type = ""
    try:
        sender = body.get("sender", {}).get("login")
        sender_id = body.get("sender", {}).get("id")
        sender_type = body.get("sender", {}).get("type")
        repo = body.get("repository", {}).get("full_name", "")
        git_org = body.get("organization", {}).get("login", "")
        installation_id = body.get("installation", {}).get("id", "")
        app_name = get_settings().get("CONFIG.APP_NAME", "Unknown")
        log_context = {"action": action, "event": event, "sender": sender, "server_type": "github_app",
                       "request_id": uuid.uuid4().hex, "build_number": build_number, "app_name": app_name,
                        "repo": repo, "git_org": git_org, "installation_id": installation_id}
    except Exception as e:
        get_logger().error("Failed to get log context", artifact={'error': e})
        log_context = {}
    return log_context, sender, sender_id, sender_type


def is_bot_user(sender, sender_type):
    try:
        # logic to ignore PRs opened by bot
        ignore_bot_pr = get_settings().get("GITHUB_APP.IGNORE_BOT_PR", None)
        if ignore_bot_pr is None:
            ignore_bot_pr = get_settings().get("GITHUB.IGNORE_BOT_PR", False)
        if ignore_bot_pr and sender_type == "Bot":
            if 'pr-agent' not in sender:
                get_logger().info(f"Ignoring PR from '{sender=}' because it is a bot")
            return True
    except Exception as e:
        get_logger().error(f"Failed 'is_bot_user' logic: {e}")
    return False


def should_process_pr_logic(body) -> bool:
    return shared_should_process_pr_logic(body, provider="github")


async def _dispatch_request(body: Dict[str, Any], event: str, action: str):
    """Return False when a command fails, preserving ignored events as completed work."""
    agent = PRAgent()
    log_context, sender, sender_id, sender_type = get_log_context(body, event, action, build_number)

    # logic to ignore PRs opened by bot, PRs with specific titles, labels, source branches, or target branches
    if is_bot_user(sender, sender_type) and 'check_run' not in body:
        get_logger().debug("Request ignored: bot user detected")
        return {}
    if action != 'created' and 'check_run' not in body:
        if not should_process_pr_logic(body):
            get_logger().debug("Request ignored: PR logic filtering")
            return {}

    if 'check_run' in body:  # handle failed checks
        # get_logger().debug(f'Request body', artifact=body, event=event) # added inside handle_checks
        pass
    # handle submitted pull request reviews
    elif event == 'pull_request_review' and action == 'submitted':
        get_logger().debug('Request body', artifact=body, event=event)
        return await handle_pull_request_review_submitted(
            body, event, sender, sender_id, sender_type, action, log_context, agent
        )
    # handle comments on PRs
    elif action == 'created':
        get_logger().debug('Request body', artifact=body, event=event)
        return await handle_comments_on_pr(body, event, sender, sender_id, action, log_context, agent)
    # handle new PRs
    elif event == 'pull_request' and action != 'synchronize' and action != 'closed':
        get_logger().debug('Request body', artifact=body, event=event)
        return await handle_new_pr_opened(body, event, sender, sender_id, action, log_context, agent)
    elif event == "issue_comment" and 'edited' in action:
        pass # handle_checkbox_clicked
    # handle pull_request event with synchronize action - "push trigger" for new commits
    elif event == 'pull_request' and action == 'synchronize':
        return await handle_push_trigger_for_new_commits(body, event, sender,sender_id,  action, log_context, agent)
    elif event == 'pull_request' and action == 'closed':
        if get_settings().get("CONFIG.ANALYTICS_FOLDER", ""):
            handle_closed_pr(body, event, action, log_context)
    else:
        get_logger().info(f"event {event=} action {action=} does not require any handling")
    return {}


async def handle_request(body: Dict[str, Any], event: str, delivery_id: str | None = None):
    """
    Handle incoming GitHub webhook requests.

    Args:
        body: The request body.
        event: The GitHub event type (e.g. "pull_request", "issue_comment", etc.).
        delivery_id: GitHub's stable identifier for this webhook delivery and its redeliveries.
    """
    action = body.get("action") # "created", "opened", "reopened", "ready_for_review", "review_requested", "synchronize"
    get_logger().debug(f"Handling request with event: {event}, action: {action}")
    if not action:
        get_logger().debug("No action found in request body, exiting handle_request")
        return {}
    settings = get_settings()
    if not delivery_id or not settings.get("GITHUB_APP.WEBHOOK_DELIVERY_DEDUPLICATION", False):
        await _dispatch_request(body, event, action)
        return {}

    async with push_trigger_slot(
        f"github-webhook-delivery:{delivery_id}",
        allow_backlog=False,
        ttl=_WEBHOOK_DELIVERY_TTL,
    ) as proceed:
        if not proceed or _completed_webhook_deliveries[delivery_id] > time.monotonic():
            return {}
        succeeded = await _dispatch_request(body, event, action)
        if succeeded is not False:
            _completed_webhook_deliveries[delivery_id] = time.monotonic() + _WEBHOOK_DELIVERY_TTL
    return {}


def handle_line_comments(body: Dict, comment_body: [str, Any]):
    if not comment_body:
        return ""
    start_line = body["comment"]["start_line"] or body["comment"].get("original_start_line")
    end_line = body["comment"]["line"] or body["comment"].get("original_line")
    start_line = end_line if not start_line else start_line
    question = comment_body.replace('/ask', '').strip()
    diff_hunk = body["comment"]["diff_hunk"]
    get_settings().set("ask_diff_hunk", diff_hunk)
    path = body["comment"]["path"]
    side = body["comment"]["side"]
    comment_id = body["comment"]["id"]
    if '/ask' in comment_body:
        # Build an argv list rather than concatenating into a shell-style
        # command string. PRAgent._handle_request() tokenises string requests
        # with single quotes treated literally, which neutralises any
        # shlex.quote() output and re-introduces the CLI-argument injection
        # vector (a quoted value containing whitespace splits into multiple
        # argv tokens). Passing a list bypasses the shlex path entirely.
        cmd = [
            "/ask_line",
            f"--line_start={start_line}",
            f"--line_end={end_line}",
            f"--side={side}",
            f"--file_name={path}",
            f"--comment_id={comment_id}",
        ]
        if question:
            cmd.append(question)
        return cmd
    return comment_body


def _check_pull_request_event(action: str, body: dict, log_context: dict) -> Tuple[Dict[str, Any], str]:
    invalid_result = {}, ""
    pull_request = body.get("pull_request")
    if not pull_request:
        return invalid_result
    api_url = pull_request.get("url")
    if not api_url:
        return invalid_result
    log_context["api_url"] = api_url
    if pull_request.get("state") != "open":
        return invalid_result
    if (action in ("review_requested", "synchronize")
            and pull_request.get("created_at") == pull_request.get("updated_at")):
        # avoid double reviews when opening a PR for the first time
        return invalid_result
    return pull_request, api_url


async def _perform_auto_commands_github(commands_conf: str, agent: PRAgent, body: dict, api_url: str,
                                        log_context: dict):
    feedback_on_draft = get_settings().github_app.feedback_on_draft_pr
    if commands_conf == "pr_commands" and body.get("action") == "ready_for_review" and feedback_on_draft:
        get_logger().info(f"Skipping draft-ready commands because draft feedback is enabled: {api_url}")
        return
    is_draft = body.get("pull_request", {}).get("draft", True)
    if is_draft and not feedback_on_draft:
        get_logger().info(f"Skipping draft PR {api_url=}")
        return
    if commands_conf in ("pr_commands", "review_commands") and get_settings().config.disable_auto_feedback:
        # auto commands for PR/review, and auto feedback is disabled
        get_logger().info(f"Auto feedback is disabled, skipping auto commands for PR {api_url=}")
        return
    if not should_process_pr_logic(body): # Here we already updated the configuration with the repo settings
        return {}
    commands = (
        get_pr_commands("github_app")
        if commands_conf == "pr_commands"
        else get_settings().get(f"github_app.{commands_conf}")
    )
    if not commands:
        get_logger().info(f"No {commands_conf} configured, skipping auto commands")
        return
    get_settings().set("config.is_auto_command", True)
    provider = _check_run_provider(api_url)
    try:
        command_provider = get_git_provider_with_context(pr_url=api_url)
    except Exception as e:
        get_logger().warning(f"Cannot access the GitHub provider for cache reset, {api_url=}: {e}")
        command_provider = None
    succeeded = True
    for command in commands:
        check_run = None
        command_succeeded = True
        # Clear only completed empty diffs at each command boundary.
        if getattr(command_provider, "diff_files", None) == []:
            command_provider.diff_files = None
        try:
            new_command = prepare_command(command)
            get_logger().info(f"{commands_conf}. Performing auto command '{new_command}', for {api_url=}")
            check_run = _start_auto_command_check_run(provider, new_command)
            # Install a fresh collector so `command_failed()` below cannot read a verdict left
            # behind by the previous command; the tool replaces it with its own on entry.
            init_run_details()
            if await agent.handle_request(api_url, new_command) is False:
                command_succeeded = False
            elif command_failed():
                # `propagate_tool_errors` is false by default, so a tool that failed internally
                # still returns normally. Reporting that as success would put a green tick on a
                # pull request that never got its review.
                get_logger().warning(f"Command '{command}' reported success but recorded a failure")
                command_succeeded = False
        except Exception as e:
            command_succeeded = False
            get_logger().error(f"Failed to perform command {command}: {e}")
        _finish_auto_command_check_run(provider, check_run, command, command_succeeded)
        succeeded = succeeded and command_succeeded
    return succeeded


@router.get("/")
async def root():
    return {"status": "ok"}


if get_settings().github_app.override_deployment_type:
    # Override the deployment type to app
    get_settings().set("GITHUB.DEPLOYMENT_TYPE", "app")
# get_settings().set("CONFIG.PUBLISH_OUTPUT_PROGRESS", False)
middleware = [Middleware(RawContextMiddleware)]
if prometheus_metrics_enabled():
    attach_metrics_endpoint(router)
app = FastAPI(middleware=middleware)
app.include_router(router)


def start():
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))


if __name__ == '__main__':
    start()
