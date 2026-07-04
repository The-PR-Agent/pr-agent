import copy
import json
import os
import re
from typing import Any, Dict

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette_context import context
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.servers.utils import verify_signature

# Setup logging and router
setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
router = APIRouter()

@router.post("/api/v1/gitea_webhooks")
async def handle_gitea_webhooks(background_tasks: BackgroundTasks, request: Request, response: Response):
    """Handle incoming Gitea webhook requests"""
    get_logger().debug("Received a Gitea webhook")

    body = await get_body(request)

    # Set context for the request
    context["settings"] = copy.deepcopy(global_settings)
    context["git_provider"] = {}

    # Handle the webhook in background
    background_tasks.add_task(handle_request, body, event=request.headers.get("X-Gitea-Event", None))
    return {}

async def get_body(request: Request):
    """Parse and verify webhook request body.

    The raw bytes are read FIRST and the JSON is parsed from those same bytes.
    HMAC verification must run over the exact bytes the client signed; reading
    ``request.json()`` before ``request.body()`` risks the body stream already
    being consumed, so ``request.body()`` could yield empty bytes and the
    signature check would run over the wrong payload.
    """
    try:
        body_bytes = await request.body()
    except Exception as e:
        get_logger().error("Error reading request body", artifact={'error': e})
        raise HTTPException(status_code=400, detail="Error reading request body") from e

    # Verify webhook signature over the exact raw bytes that were received.
    webhook_secret = getattr(get_settings().gitea, 'webhook_secret', None)
    if webhook_secret:
        signature_header = request.headers.get('x-gitea-signature', None)
        if not signature_header:
            get_logger().error("Missing signature header")
            raise HTTPException(status_code=400, detail="Missing signature header")

        try:
            verify_signature(body_bytes, webhook_secret, f"sha256={signature_header}")
        except Exception as ex:
            get_logger().error(f"Invalid signature: {ex}")
            raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        body = json.loads(body_bytes)
    except Exception as e:
        get_logger().error("Error parsing request body", artifact={'error': e})
        raise HTTPException(status_code=400, detail="Error parsing request body") from e

    return body

def get_bot_user() -> str:
    """The login of the account whose token the agent runs as.

    Used to filter out the agent's own comments so it never triggers on
    itself. Configurable via ``gitea.bot_user``; defaults to "pr-agent".
    """
    return str(get_settings().get("GITEA.BOT_USER", "pr-agent") or "pr-agent")


def is_self_comment(body: Dict[str, Any]) -> bool:
    """True if the comment/event was raised by the agent's own bot account."""
    sender = str((body.get("sender") or {}).get("login", ""))
    return bool(sender) and sender.lower() == get_bot_user().lower()


async def handle_request(body: Dict[str, Any], event: str):
    """Process Gitea webhook events"""
    action = body.get("action")
    if not action:
        get_logger().debug("No action found in request body")
        return {}

    # Never act on an event the agent itself raised (e.g. its own review-submitted
    # echo or its own comment), which would otherwise self-trigger an endless loop.
    if is_self_comment(body):
        get_logger().debug("Request ignored: event raised by the bot user itself")
        return {}

    agent = PRAgent()

    # Handle different event types
    if event == "pull_request":
        if not should_process_pr_logic(body):
            get_logger().debug(f"Request ignored: PR logic filtering")
            return {}
        if action in ["opened", "reopened", "synchronized"]:
            await handle_pr_event(body, event, action, agent)
        elif action == "review_requested":
            await handle_review_requested_event(body, event, action, agent)
    elif event == "issue_comment":
        if action == "created":
            await handle_comment_event(body, event, action, agent)

    return {}

async def handle_pr_event(body: Dict[str, Any], event: str, action: str, agent: PRAgent):
    """Handle pull request events"""
    pr = body.get("pull_request", {})
    if not pr:
        return

    api_url = pr.get("url")
    if not api_url:
        return

    # Handle PR based on action
    if action in ["opened", "reopened"]:
        # commands = get_settings().get("gitea.pr_commands", [])
        await _perform_commands_gitea("pr_commands", agent, body, api_url)
        # for command in commands:
        #     await agent.handle_request(api_url, command)
    elif action == "synchronized":
        # Handle push to PR
        commands_on_push = get_settings().get(f"gitea.push_commands", {})
        handle_push_trigger = get_settings().get(f"gitea.handle_push_trigger", False)
        if not commands_on_push or not handle_push_trigger:
            get_logger().info("Push event, but no push commands found or push trigger is disabled")
            return
        get_logger().debug(f'A push event has been received: {api_url}')

        # Gitea's synchronized payload carries the head-branch SHAs before/after
        # the push. When incremental push review is enabled we anchor the review
        # to that range (before...after) so a re-review only looks at the newly
        # pushed commits. Full review remains the default/fallback: incremental
        # only kicks in when the flag is set AND both SHAs are present.
        before_sha = body.get("before")
        after_sha = body.get("after")
        incremental_push = get_settings().get("gitea.incremental_review_on_push", False)
        if incremental_push and before_sha and after_sha and before_sha != after_sha:
            get_logger().info(
                f"Incremental push review for {api_url}: {before_sha}...{after_sha}"
            )
            await _perform_commands_gitea(
                "push_commands", agent, body, api_url,
                incremental=True, before_sha=before_sha, after_sha=after_sha,
            )
        else:
            await _perform_commands_gitea("push_commands", agent, body, api_url)

def review_requested_for_bot(body: Dict[str, Any]) -> bool:
    """True when the (re-)requested reviewer is the agent's bot account."""
    bot = get_bot_user().lower()
    requested = body.get("requested_reviewer") or {}
    if isinstance(requested, dict) and str(requested.get("login", "")).lower() == bot:
        return True
    # fall back to the PR's current requested-reviewers list
    for user in (body.get("pull_request") or {}).get("requested_reviewers") or []:
        if isinstance(user, dict) and str(user.get("login", "")).lower() == bot:
            return True
    return False


async def handle_review_requested_event(body: Dict[str, Any], event: str, action: str, agent: PRAgent):
    """Auto-run /review when the bot is (re-)requested as a reviewer.

    Gitea fires ``pull_request`` with ``action=review_requested`` when a
    reviewer is added (including the "re-request review" button). Upstream
    never wired this action, so a reviewer request was a no-op. We run the
    configured ``pr_commands`` (which include /review) so the reviewer slot is
    filled on demand.
    """
    if not review_requested_for_bot(body):
        get_logger().debug("Review requested, but not for the bot user; ignoring")
        return
    if not should_process_pr_logic(body):
        get_logger().debug("Request ignored: PR logic filtering")
        return

    pr = body.get("pull_request", {})
    api_url = pr.get("url")
    if not api_url:
        return

    get_logger().info(f"Bot (re-)requested as reviewer, running pr_commands for {api_url=}")
    await _perform_commands_gitea("pr_commands", agent, body, api_url)


async def handle_comment_event(body: Dict[str, Any], event: str, action: str, agent: PRAgent):
    """Handle comment events"""
    comment = body.get("comment", {})
    if not comment:
        return

    comment_body = comment.get("body", "")
    if not comment_body or not comment_body.startswith("/"):
        return

    pr_url = body.get("pull_request", {}).get("url")
    if not pr_url:
        return

    await agent.handle_request(pr_url, comment_body)

async def _perform_commands_gitea(commands_conf: str, agent: PRAgent, body: dict, api_url: str,
                                  incremental: bool = False, before_sha: str = None,
                                  after_sha: str = None):
    apply_repo_settings(api_url)
    if commands_conf == "pr_commands" and get_settings().config.disable_auto_feedback:  # auto commands for PR, and auto feedback is disabled
        get_logger().info(f"Auto feedback is disabled, skipping auto commands for PR {api_url=}")
        return
    if not should_process_pr_logic(body): # Here we already updated the configuration with the repo settings
        return {}
    commands = get_settings().get(f"gitea.{commands_conf}")
    if not commands:
        get_logger().info(f"New PR, but no auto commands configured")
        return
    if incremental:
        # Expose the pushed range so the provider can anchor the incremental base
        # to the push `before` SHA when there is no prior review comment to
        # diff against (full review stays the fallback inside the provider).
        get_settings().set("gitea.incremental_push_before_sha", before_sha)
        get_settings().set("gitea.incremental_push_after_sha", after_sha)
    get_settings().set("config.is_auto_command", True)
    for command in commands:
        split_command = command.split(" ")
        command = split_command[0]
        args = split_command[1:]
        # Turn `/review` into `/review -i` on an incremental push so only the
        # newly pushed commits are re-reviewed. Other commands are untouched.
        if incremental and command == "/review" and "-i" not in args:
            args = ["-i"] + args
        other_args = update_settings_from_args(args)
        new_command = ' '.join([command] + other_args)
        get_logger().info(f"{commands_conf}. Performing auto command '{new_command}', for {api_url=}")
        await agent.handle_request(api_url, new_command)

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

# FastAPI app setup
middleware = [Middleware(RawContextMiddleware)]
app = FastAPI(middleware=middleware)
app.include_router(router)

def start():
    """Start the Gitea webhook server"""
    port = int(os.environ.get("PORT", "3000"))
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    start()
