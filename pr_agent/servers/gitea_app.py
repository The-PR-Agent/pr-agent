import asyncio
import copy
import os
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette_context import context
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent, prepare_command
from pr_agent.agent.request_policy import RULE_FIELDS, RequestOutcome, enforce_request_policy
from pr_agent.algo.run_details import command_failed
from pr_agent.algo.setting_utils import _as_bool_setting, normalize_setting_list
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.servers.request_body_limit import create_server_app
from pr_agent.servers.utils import (
    auto_feedback_disabled,
    get_pr_commands,
    is_command_comment,
    push_trigger_slot,
    shared_should_process_pr_logic,
    verify_signature,
)
from pr_agent.telemetry.prometheus import attach_metrics_endpoint, prometheus_metrics_enabled

# Setup logging and router
setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "INFO"))
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
    """Verify and parse webhook request body"""
    # Verify webhook signature
    webhook_secret = getattr(get_settings().gitea, 'webhook_secret', None)
    if not webhook_secret:
        # Refuse unauthenticated webhooks. Silently accepting requests when the
        # secret is not configured used to allow any internet caller to forge
        # Gitea events and trigger expensive AI commands against arbitrary PRs.
        get_logger().error("Rejecting Gitea webhook: GITEA.WEBHOOK_SECRET is not configured")
        raise HTTPException(status_code=403, detail="Webhook secret not configured")
    body_bytes = await request.body()
    signature_header = request.headers.get('x-gitea-signature', None)
    if not signature_header:
        get_logger().error("Missing signature header")
        raise HTTPException(status_code=400, detail="Missing signature header")

    try:
        verify_signature(body_bytes, webhook_secret, f"sha256={signature_header}")
    except Exception as ex:
        get_logger().error(f"Invalid signature: {ex}")
        raise HTTPException(status_code=401, detail="Invalid signature") from ex

    try:
        body = await request.json()
    except Exception as e:
        get_logger().error("Error parsing request body", artifact={'error': e})
        raise HTTPException(status_code=400, detail="Error parsing request body") from e
    return body

async def handle_request(body: dict[str, Any], event: str):
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

async def handle_pr_event(body: dict[str, Any], event: str, action: str, agent: PRAgent):
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
        result = await _perform_commands_gitea("pr_commands", agent, body, api_url)
        if result is False:
            get_logger().error("Gitea PR auto commands stopped before completion")
        return result
        # for command in commands:
        #     await agent.handle_request(api_url, command)
    elif action == "synchronized":
        # Handle push to PR
        commands_on_push = normalize_setting_list(get_settings().get("gitea.push_commands", {}))
        handle_push_trigger = _as_bool_setting(get_settings().get("gitea.handle_push_trigger", False))
        if not commands_on_push or not handle_push_trigger:
            get_logger().info("Push event, but no push commands found or push trigger is disabled")
            return
        get_logger().debug(f'A push event has been received: {api_url}')
        async with push_trigger_slot(api_url, allow_backlog=True, ttl=300) as proceed:
            if proceed:
                result = await _perform_commands_gitea("push_commands", agent, body, api_url)
                if result is False:
                    get_logger().error("Gitea push auto commands stopped before completion")
                return result
        # for command in commands_on_push:
        #     await agent.handle_request(api_url, command)

async def handle_comment_event(body: dict[str, Any], event: str, action: str, agent: PRAgent):
    """Handle comment events"""
    comment = body.get("comment", {})
    if not comment:
        return

    comment_body = comment.get("body", "")
    if not is_command_comment(comment_body):
        get_logger().info("Ignoring comment not starting with /")
        return

    pr_url = body.get("pull_request", {}).get("url")
    if not pr_url:
        return

    await agent.handle_request(pr_url, comment_body)

async def _perform_commands_gitea(commands_conf: str, agent: PRAgent, body: dict, api_url: str):
    if auto_feedback_disabled():  # auto commands, and auto feedback is disabled
        get_logger().info(f"Auto feedback is disabled, skipping auto commands for PR {api_url=}")
        return
    commands = (
        get_pr_commands("gitea")
        if commands_conf == "pr_commands"
        else normalize_setting_list(get_settings().get(f"gitea.{commands_conf}"))
    )
    if not commands:
        get_logger().info("New PR, but no auto commands configured")
        return
    get_settings().set("config.is_auto_command", True)
    for command in commands:
        try:
            # Reset diff state between auto commands, like the gitlab/github loops (#4372).
            if context.exists():
                provider = context.get("git_provider", {}).get(api_url)
                reset_diff_cache = getattr(provider, "reset_diff_cache_for_command", None)
                if callable(reset_diff_cache):
                    # The provider refresh performs blocking Gitea API I/O.
                    try:
                        await asyncio.to_thread(reset_diff_cache)
                    except Exception:
                        # An unexpected provider failure must not skip one command
                        # and allow later commands to use an unverified snapshot.
                        get_logger().exception("Gitea snapshot refresh failed; stopping auto commands")
                        return False
                    if getattr(provider, "_line_link_snapshot_unverified", False):
                        # Other tools may construct their own GiteaProvider, so
                        # do not dispatch a command with an unverified snapshot.
                        get_logger().warning("Gitea PR snapshot unverified; stopping auto commands")
                        required = {
                            field for field, rule in RULE_FIELDS.items()
                            if get_settings().get(f"config.{rule}", [])
                        }
                        if getattr(provider, "_notice_policy_snapshot_required", True):
                            # Repository/ignore rules were active before command
                            # overrides. Never authorize a notice from mutable
                            # CLI settings after a retarget or failed refresh.
                            get_logger().warning("Suppressing Gitea notice: original policy may have changed")
                            return False
                        if getattr(provider, "_gitea_policy_metadata_unverified", False) and required:
                            get_logger().warning("Suppressing notice: Gitea policy metadata unavailable")
                            return False
                        if required:
                            try:
                                metadata = await asyncio.to_thread(provider.get_request_policy_metadata, required)
                            except Exception:
                                get_logger().exception("Cannot validate Gitea notice policy metadata")
                                return False
                            if not isinstance(metadata, dict) or any(metadata.get(field) is None for field in required):
                                get_logger().warning("Suppressing notice: Gitea policy fields are incomplete")
                                return False
                            if await asyncio.to_thread(enforce_request_policy, api_url, provider=provider) is False:
                                return RequestOutcome.SKIPPED
                        if get_settings().get("CONFIG.PUBLISH_OUTPUT", True):
                            try:
                                await asyncio.to_thread(
                                    provider.publish_comment,
                                    "## PR-Agent auto commands were not run\n\n"
                                    "The pull request version could not be verified, or its target changed "
                                    "during processing. Please retry the commands.",
                                )
                            except Exception:
                                get_logger().exception("Failed to publish the Gitea snapshot notice")
                        return False
            new_command = prepare_command(command)
            get_logger().info(f"{commands_conf}. Performing auto command '{new_command}', for {api_url=}")
            outcome = await agent.handle_request(api_url, new_command)
            if outcome is RequestOutcome.SKIPPED:
                return RequestOutcome.SKIPPED
            if outcome is not True or command_failed():
                get_logger().warning("Stopping Gitea auto commands after a failed command")
                return False
        except Exception:
            get_logger().exception(f"Failed to perform Gitea command {command}; stopping batch")
            return False

def should_process_pr_logic(body) -> bool:
    return shared_should_process_pr_logic(body, provider="gitea")

# FastAPI app setup
middleware = [Middleware(RawContextMiddleware)]
if prometheus_metrics_enabled():
    attach_metrics_endpoint(router)
app = create_server_app(middleware=middleware)
app.include_router(router)

def start():
    """Start the Gitea webhook server"""
    port = int(os.environ.get("PORT", "3000"))
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port)

if __name__ == "__main__":
    start()
