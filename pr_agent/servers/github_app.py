import asyncio.locks
import copy
import os
import re
import uuid
from typing import Any, Dict, Tuple

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette_context import context
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers import (get_git_provider,
                                    get_git_provider_with_context)
from pr_agent.git_providers.git_provider import IncrementalPR
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.identity_providers import get_identity_provider
from pr_agent.identity_providers.identity_provider import Eligibility
from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.servers.utils import DefaultDictWithTimeout, verify_signature

setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
base_path = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
build_number_path = os.path.join(base_path, "build_number.txt")
if os.path.exists(build_number_path):
    with open(build_number_path) as f:
        build_number = f.read().strip()
else:
    build_number = "unknown"
router = APIRouter()


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
    background_tasks.add_task(handle_request, body, event=request.headers.get("X-GitHub-Event", None))
    return {}


@router.post("/api/v1/marketplace_webhooks")
async def handle_marketplace_webhooks(request: Request, response: Response):
    body = await get_body(request)
    get_logger().info(f'Request body:\n{body}')


async def get_body(request):
    try:
        body = await request.json()
    except Exception as e:
        get_logger().error("Error parsing request body", artifact={'error': e})
        raise HTTPException(status_code=400, detail="Error parsing request body") from e
    webhook_secret = getattr(get_settings().github, 'webhook_secret', None)
    if webhook_secret:
        body_bytes = await request.body()
        signature_header = request.headers.get('x-hub-signature-256', None)
        verify_signature(body_bytes, webhook_secret, signature_header)
    return body


_duplicate_push_triggers = DefaultDictWithTimeout(ttl=get_settings().github_app.push_trigger_pending_tasks_ttl)
_pending_task_duplicate_push_conditions = DefaultDictWithTimeout(asyncio.locks.Condition, ttl=get_settings().github_app.push_trigger_pending_tasks_ttl)

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
        if '/ask' in comment_body and comment_body.strip().startswith('> ![image]'):
            comment_body_split = comment_body.split('/ask')
            comment_body = '/ask' + comment_body_split[1] +' \n' +comment_body_split[0].strip().lstrip('>')
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
    with get_logger().contextualize(**log_context):
        if get_identity_provider().verify_eligibility("github", sender_id, api_url) is not Eligibility.NOT_ELIGIBLE:
            get_logger().info(f"Processing comment on PR {api_url=}, comment_body={comment_body}")
            await agent.handle_request(api_url, comment_body,
                        notify=lambda: provider.add_eyes_reaction(comment_id, disable_eyes=disable_eyes))
        else:
            get_logger().info(f"User {sender=} is not eligible to process comment on PR {api_url=}")

async def handle_new_pr_opened(body: Dict[str, Any],
                               event: str,
                               sender: str,
                               sender_id: str,
                               action: str,
                               log_context: Dict[str, Any],
                               agent: PRAgent):
    title = body.get("pull_request", {}).get("title", "")

    pull_request, api_url = _check_pull_request_event(action, body, log_context)
    if not (pull_request and api_url):
        get_logger().info(f"Invalid PR event: {action=} {api_url=}")
        return {}
    if action in get_settings().github_app.handle_pr_actions:  # ['opened', 'reopened', 'ready_for_review']
        # logic to ignore PRs with specific titles (e.g. "[Auto] ...")
        apply_repo_settings(api_url)
        if get_identity_provider().verify_eligibility("github", sender_id, api_url) is not Eligibility.NOT_ELIGIBLE:
            await _perform_auto_commands_github("pr_commands", agent, body, api_url, log_context)
        else:
            get_logger().info(f"User {sender=} is not eligible to process PR {api_url=}")

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

    apply_repo_settings(api_url) # we need to apply the repo settings to get the correct settings for the PR. This is quite expensive - a call to the git provider is made for each PR event.
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

    # Prevent triggering multiple times for subsequent push triggers when one is enough:
    # The first push will trigger the processing, and if there's a second push in the meanwhile it will wait.
    # Any more events will be discarded, because they will all trigger the exact same processing on the PR.
    # We let the second event wait instead of discarding it because while the first event was being processed,
    # more commits may have been pushed that led to the subsequent events,
    # so we keep just one waiting as a delegate to trigger the processing for the new commits when done waiting.
    current_active_tasks = _duplicate_push_triggers.setdefault(api_url, 0)
    max_active_tasks = 2 if get_settings().github_app.push_trigger_pending_tasks_backlog else 1
    if current_active_tasks < max_active_tasks:
        # first task can enter, and second tasks too if backlog is enabled
        get_logger().info(
            f"Continue processing push trigger for {api_url=} because there are {current_active_tasks} active tasks"
        )
        _duplicate_push_triggers[api_url] += 1
    else:
        get_logger().info(
            f"Skipping push trigger for {api_url=} because another event already triggered the same processing"
        )
        return {}
    async with _pending_task_duplicate_push_conditions[api_url]:
        if current_active_tasks == 1:
            # second task waits
            get_logger().info(
                f"Waiting to process push trigger for {api_url=} because the first task is still in progress"
            )
            await _pending_task_duplicate_push_conditions[api_url].wait()
            get_logger().info(f"Finished waiting to process push trigger for {api_url=} - continue with flow")

    try:
        if get_identity_provider().verify_eligibility("github", sender_id, api_url) is not Eligibility.NOT_ELIGIBLE:
            get_logger().info(f"Performing incremental review for {api_url=} because of {event=} and {action=}")
            await _perform_auto_commands_github("push_commands", agent, body, api_url, log_context)

    finally:
        # release the waiting task block
        async with _pending_task_duplicate_push_conditions[api_url]:
            _pending_task_duplicate_push_conditions[api_url].notify(1)
            _duplicate_push_triggers[api_url] -= 1


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
        get_logger().error(f"Failed to get log context", artifact={'error': e})
        log_context = {}
    return log_context, sender, sender_id, sender_type


def is_bot_user(sender, sender_type):
    try:
        # logic to ignore PRs opened by bot
        if get_settings().get("GITHUB_APP.IGNORE_BOT_PR", False) and sender_type == "Bot":
            if 'pr-agent' not in sender:
                get_logger().info(f"Ignoring PR from '{sender=}' because it is a bot")
            return True
    except Exception as e:
        get_logger().error(f"Failed 'is_bot_user' logic: {e}")
    return False


def should_process_pr_logic(body) -> bool:
    try:
        pull_request = body.get("pull_request", {})
        title = pull_request.get("title", "")
        pr_labels = pull_request.get("labels", [])
        source_branch = pull_request.get("head", {}).get("ref", "")
        target_branch = pull_request.get("base", {}).get("ref", "")
        sender = body.get("sender", {}).get("login")
        repo_full_name = body.get("repository", {}).get("full_name", "")

        # logic to ignore PRs from specific repositories
        ignore_repos = get_settings().get("CONFIG.IGNORE_REPOSITORIES", [])
        if ignore_repos and repo_full_name:
            if any(re.search(regex, repo_full_name) for regex in ignore_repos):
                get_logger().info(f"Ignoring PR from repository '{repo_full_name}' due to 'config.ignore_repositories' setting")
                return False

        # logic to ignore PRs from specific users
        ignore_pr_users = get_settings().get("CONFIG.IGNORE_PR_AUTHORS", [])
        if ignore_pr_users and sender:
            if any(re.search(regex, sender) for regex in ignore_pr_users):
                get_logger().info(f"Ignoring PR from user '{sender}' due to 'config.ignore_pr_authors' setting")
                return False

        # logic to ignore PRs with specific titles
        if title:
            ignore_pr_title_re = get_settings().get("CONFIG.IGNORE_PR_TITLE", [])
            if not isinstance(ignore_pr_title_re, list):
                ignore_pr_title_re = [ignore_pr_title_re]
            if ignore_pr_title_re and any(re.search(regex, title) for regex in ignore_pr_title_re):
                get_logger().info(f"Ignoring PR with title '{title}' due to config.ignore_pr_title setting")
                return False

        # logic to ignore PRs with specific labels or source branches or target branches.
        ignore_pr_labels = get_settings().get("CONFIG.IGNORE_PR_LABELS", [])
        if pr_labels and ignore_pr_labels:
            labels = [label['name'] for label in pr_labels]
            if any(label in ignore_pr_labels for label in labels):
                labels_str = ", ".join(labels)
                get_logger().info(f"Ignoring PR with labels '{labels_str}' due to config.ignore_pr_labels settings")
                return False

        # logic to ignore PRs with specific source or target branches
        ignore_pr_source_branches = get_settings().get("CONFIG.IGNORE_PR_SOURCE_BRANCHES", [])
        ignore_pr_target_branches = get_settings().get("CONFIG.IGNORE_PR_TARGET_BRANCHES", [])
        if pull_request and (ignore_pr_source_branches or ignore_pr_target_branches):
            if any(re.search(regex, source_branch) for regex in ignore_pr_source_branches):
                get_logger().info(
                    f"Ignoring PR with source branch '{source_branch}' due to config.ignore_pr_source_branches settings")
                return False
            if any(re.search(regex, target_branch) for regex in ignore_pr_target_branches):
                get_logger().info(
                    f"Ignoring PR with target branch '{target_branch}' due to config.ignore_pr_target_branches settings")
                return False
    except Exception as e:
        get_logger().error(f"Failed 'should_process_pr_logic': {e}")
    return True


async def handle_request(body: Dict[str, Any], event: str):
    """
    Handle incoming GitHub webhook requests.

    Args:
        body: The request body.
        event: The GitHub event type (e.g. "pull_request", "issue_comment", etc.).
    """
    action = body.get("action")  # "created", "opened", "reopened", "ready_for_review", "review_requested", "synchronize"
    get_logger().debug(f"Handling request with event: {event}, action: {action}")
    if not action:
        get_logger().debug(f"No action found in request body, exiting handle_request")
        return {}
    agent = PRAgent()
    log_context, sender, sender_id, sender_type = get_log_context(body, event, action, build_number)

    # logic to ignore PRs opened by bot, PRs with specific titles, labels, source branches, or target branches
    if is_bot_user(sender, sender_type) and 'check_run' not in body:
        get_logger().debug(f"Request ignored: bot user detected")
        return {}
    if action != 'created' and 'check_run' not in body:
        if not should_process_pr_logic(body):
            get_logger().debug(f"Request ignored: PR logic filtering")
            return {}

    if 'check_run' in body:  # handle failed checks
        # get_logger().debug(f'Request body', artifact=body, event=event) # added inside handle_checks
        pass
    # handle comments on PRs
    elif action == 'created':
        get_logger().debug(f'Request body', artifact=body, event=event)
        await handle_comments_on_pr(body, event, sender, sender_id, action, log_context, agent)
    # handle new PRs
    elif event == 'pull_request' and action != 'synchronize' and action != 'closed':
        get_logger().debug(f'Request body', artifact=body, event=event)
        await handle_new_pr_opened(body, event, sender, sender_id, action, log_context, agent)
    elif event == "issue_comment" and 'edited' in action:
        pass # handle_checkbox_clicked
    # handle pull_request event with synchronize action - "push trigger" for new commits
    elif event == 'pull_request' and action == 'synchronize':
        await handle_push_trigger_for_new_commits(body, event, sender,sender_id,  action, log_context, agent)
    elif event == 'pull_request' and action == 'closed':
        if get_settings().get("CONFIG.ANALYTICS_FOLDER", ""):
            handle_closed_pr(body, event, action, log_context)
    else:
        get_logger().info(f"event {event=} action {action=} does not require any handling")
    return {}


def handle_line_comments(body: Dict, comment_body: [str, Any]) -> str:
    if not comment_body:
        return ""
    start_line = body["comment"]["start_line"]
    end_line = body["comment"]["line"]
    start_line = end_line if not start_line else start_line
    question = comment_body.replace('/ask', '').strip()
    diff_hunk = body["comment"]["diff_hunk"]
    get_settings().set("ask_diff_hunk", diff_hunk)
    path = body["comment"]["path"]
    side = body["comment"]["side"]
    comment_id = body["comment"]["id"]
    if '/ask' in comment_body:
        comment_body = f"/ask_line --line_start={start_line} --line_end={end_line} --side={side} --file_name={path} --comment_id={comment_id} {question}"
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
    if pull_request.get("draft", True) or pull_request.get("state") != "open":
        return invalid_result
    if action in ("review_requested", "synchronize") and pull_request.get("created_at") == pull_request.get("updated_at"):
        # avoid double reviews when opening a PR for the first time
        return invalid_result
    return pull_request, api_url


async def _perform_auto_commands_github(commands_conf: str, agent: PRAgent, body: dict, api_url: str,
                                        log_context: dict):
    apply_repo_settings(api_url)
    if get_settings().config.disable_auto_feedback:
        get_logger().info(f"Auto feedback is disabled, skipping auto commands for {api_url=}")
        return
    if not should_process_pr_logic(body): # Here we already updated the configuration with the repo settings
        return {}
    commands = get_settings().get(f"github_app.{commands_conf}")
    if not commands:
        get_logger().info(f"New PR, but no auto commands configured")
        return
    get_settings().set("config.is_auto_command", True)
    for command in commands:
        split_command = command.split(" ")
        command = split_command[0]
        args = split_command[1:]
        other_args = update_settings_from_args(args)
        new_command = ' '.join([command] + other_args)
        get_logger().info(f"{commands_conf}. Performing auto command '{new_command}', for {api_url=}")
        await agent.handle_request(api_url, new_command)


@router.get("/")
async def root():
    return {"status": "ok"}


if get_settings().github_app.override_deployment_type:
    # Override the deployment type to app
    get_settings().set("GITHUB.DEPLOYMENT_TYPE", "app")
# get_settings().set("CONFIG.PUBLISH_OUTPUT_PROGRESS", False)
middleware = [Middleware(RawContextMiddleware)]
app = FastAPI(middleware=middleware)
app.include_router(router)


def start():
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))


if __name__ == '__main__':
    start()
