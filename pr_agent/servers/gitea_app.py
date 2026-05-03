import copy
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
    """Parse and verify webhook request body"""
    try:
        body = await request.json()
    except Exception as e:
        get_logger().error("Error parsing request body", artifact={'error': e})
        raise HTTPException(status_code=400, detail="Error parsing request body") from e


    # Verify webhook signature
    webhook_secret = getattr(get_settings().gitea, 'webhook_secret', None)
    if webhook_secret:
        body_bytes = await request.body()
        signature_header = request.headers.get('x-gitea-signature', None)
        if not signature_header:
            get_logger().error("Missing signature header")
            raise HTTPException(status_code=400, detail="Missing signature header")

        try:
            verify_signature(body_bytes, webhook_secret, f"sha256={signature_header}")
        except Exception as ex:
            get_logger().error(f"Invalid signature: {ex}")
            raise HTTPException(status_code=401, detail="Invalid signature")

    return body

async def handle_request(body: Dict[str, Any], event: str):
    """Process Gitea webhook events"""
    action = body.get("action")
    if not action:
        get_logger().debug("No action found in request body")
        return {}

    agent = PRAgent()

    # Handle different event types
    if event == "pull_request":
        if not should_process_pr_logic(body):
            get_logger().debug(f"Request ignored: PR logic filtering")
            return {}
        if action in ["opened", "reopened", "synchronized"]:
            await handle_pr_event(body, event, action, agent)
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
        await _perform_commands_gitea("push_commands", agent, body, api_url)
        # for command in commands_on_push:
        #     await agent.handle_request(api_url, command)

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

async def _perform_commands_gitea(commands_conf: str, agent: PRAgent, body: dict, api_url: str):
    apply_repo_settings(api_url)
    if get_settings().config.disable_auto_feedback:
        get_logger().info(f"Auto feedback is disabled, skipping auto commands for {api_url=}")
        return
    if not should_process_pr_logic(body): # Here we already updated the configuration with the repo settings
        return {}
    commands = get_settings().get(f"gitea.{commands_conf}")
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
