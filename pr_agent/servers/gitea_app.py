import copy
import os
from typing import Any, Dict

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette_context import context
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent, prepare_command
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.servers.utils import (
    get_pr_commands,
    push_trigger_slot,
    verify_signature,
)
from pr_agent.servers.utils import (
    should_process_pr_logic as _should_process_pr_logic,
)
from pr_agent.telemetry.prometheus import attach_metrics_endpoint, prometheus_metrics_enabled

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
            get_logger().debug("Request ignored: PR logic filtering")
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

    apply_repo_settings(api_url)
    if not should_process_pr_logic(body):
        return {}

    # Handle PR based on action
    if action in ["opened", "reopened"]:
        # commands = get_settings().get("gitea.pr_commands", [])
        await _perform_commands_gitea("pr_commands", agent, body, api_url)
        # for command in commands:
        #     await agent.handle_request(api_url, command)
    elif action == "synchronized":
        # Handle push to PR
        commands_on_push = get_settings().get("gitea.push_commands", {})
        handle_push_trigger = get_settings().get("gitea.handle_push_trigger", False)
        if not commands_on_push or not handle_push_trigger:
            get_logger().info("Push event, but no push commands found or push trigger is disabled")
            return
        get_logger().debug(f'A push event has been received: {api_url}')
        async with push_trigger_slot(api_url, allow_backlog=True, ttl=300) as proceed:
            if proceed:
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
    if commands_conf == "pr_commands" and get_settings().config.disable_auto_feedback:  # auto commands for PR, and auto feedback is disabled
        get_logger().info(f"Auto feedback is disabled, skipping auto commands for PR {api_url=}")
        return
    commands = (
        get_pr_commands("gitea")
        if commands_conf == "pr_commands"
        else get_settings().get(f"gitea.{commands_conf}")
    )
    if not commands:
        get_logger().info("New PR, but no auto commands configured")
        return
    get_settings().set("config.is_auto_command", True)
    for command in commands:
        try:
            new_command = prepare_command(command)
            get_logger().info(f"{commands_conf}. Performing auto command '{new_command}', for {api_url=}")
            await agent.handle_request(api_url, new_command)
        except Exception as e:
            get_logger().error(f"Failed to perform command {command}: {e}")

def should_process_pr_logic(body) -> bool:
    return _should_process_pr_logic(body, provider="gitea")

# FastAPI app setup
middleware = [Middleware(RawContextMiddleware)]
if prometheus_metrics_enabled():
    attach_metrics_endpoint(router)
app = FastAPI(middleware=middleware)
app.include_router(router)

def start():
    """Start the Gitea webhook server"""
    port = int(os.environ.get("PORT", "3000"))
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    start()
