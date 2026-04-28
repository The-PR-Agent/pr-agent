# This file contains the code for the Azure DevOps Server webhook server.
# The server listens for incoming webhooks from Azure DevOps Server and forwards them to the PR Agent.
# ADO webhook documentation: https://learn.microsoft.com/en-us/azure/devops/service-hooks/services/webhooks?view=azure-devops

import json
import os
import re
import secrets
from urllib.parse import unquote

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette import status
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent, command2class
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers.azuredevops_provider import AzureDevopsProvider
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import LoggingFormat, get_logger, setup_logger

setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
security = HTTPBasic(auto_error=False)
router = APIRouter()
available_commands_rgx = re.compile(r"^\/(" + "|".join(command2class.keys()) + r")\s*")
azure_devops_server = get_settings().get("azure_devops_server")
WEBHOOK_USERNAME = azure_devops_server.get("webhook_username", None)
WEBHOOK_PASSWORD = azure_devops_server.get("webhook_password", None)

async def handle_request_comment(url: str, body: str, thread_id: int, comment_id: int, log_context: dict):
    log_context["action"] = body
    log_context["api_url"] = url
    try:
        with get_logger().contextualize(**log_context):
            agent = PRAgent()
            provider = get_git_provider_with_context(pr_url=url)
            body = handle_line_comment(body, thread_id, provider)
            handled = await agent.handle_request(url, body, notify=lambda: provider.reply_to_thread(thread_id, "On it! ⏳", True))
            # mark command comment as closed
            if handled:
                provider.set_thread_status(thread_id, "closed")
                provider.remove_initial_comment()
    except Exception as e:
        get_logger().exception(f"Failed to handle webhook", artifact={"url": url, "body": body}, error=str(e))

def handle_line_comment(body: str, thread_id: int, provider: AzureDevopsProvider):
    body = body.strip()
    if not body.startswith('/ask '):
        return body
    thread_context = provider.get_thread_context(thread_id)
    if not thread_context:
        return body
    
    path = thread_context.file_path
    if thread_context.left_file_end or thread_context.left_file_start:
        start_line = thread_context.left_file_start.line
        end_line = thread_context.left_file_end.line
        side = "left"
    elif thread_context.right_file_end or thread_context.right_file_start:
        start_line = thread_context.right_file_start.line
        end_line = thread_context.right_file_end.line
        side = "right"
    else:
        get_logger().info("No line range found in thread context", artifact={"thread_context": thread_context})
        return body
    
    question = body[5:].lstrip() # remove 4 chars: '/ask '
    return f"/ask_line --line_start={start_line} --line_end={end_line} --side={side} --file_name={path} --comment_id={thread_id} {question}"

# currently only basic auth is supported with azure webhooks
# for this reason, https must be enabled to ensure the credentials are not sent in clear text
def authorize(credentials: HTTPBasicCredentials = Depends(security)):
    if WEBHOOK_USERNAME is None or WEBHOOK_PASSWORD is None:
        return
    
    is_user_ok = secrets.compare_digest(credentials.username, WEBHOOK_USERNAME)
    is_pass_ok = secrets.compare_digest(credentials.password, WEBHOOK_PASSWORD)
    if not (is_user_ok and is_pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Incorrect username or password.',
            headers={'WWW-Authenticate': 'Basic'},
        )


async def _perform_commands_azure(commands_conf: str, agent: PRAgent, api_url: str, log_context: dict):
    apply_repo_settings(api_url)
    if get_settings().config.disable_auto_feedback:
        get_logger().info(f"Auto feedback is disabled, skipping auto commands for {api_url=}", **log_context)
        return
    commands = get_settings().get(f"azure_devops_server.{commands_conf}")
    if not commands:
        return

    get_settings().set("config.is_auto_command", True)
    for command in commands:
        try:
            split_command = command.split(" ")
            command = split_command[0]
            args = split_command[1:]
            other_args = update_settings_from_args(args)
            new_command = ' '.join([command] + other_args)
            get_logger().info(f"Performing command: {new_command}")
            with get_logger().contextualize(**log_context):
                await agent.handle_request(api_url, new_command)
        except Exception as e:
            get_logger().error(f"Failed to perform command {command}: {e}")


async def handle_request_azure(data, log_context):
    if data["eventType"] == "git.pullrequest.created":
        # API V1 (latest)
        pr_url = unquote(data["resource"]["_links"]["web"]["href"].replace("_apis/git/repositories", "_git"))
        log_context["event"] = data["eventType"]
        log_context["api_url"] = pr_url
        await _perform_commands_azure("pr_commands", PRAgent(), pr_url, log_context)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content=jsonable_encoder({"message": "webhook triggered successfully"})
        )
    elif data["eventType"] == "ms.vss-code.git-pullrequest-comment-event" and "content" in data["resource"]["comment"]:
        comment = data["resource"]["comment"]
        if available_commands_rgx.match(comment["content"]):
            if(data["resourceVersion"] == "2.0"):
                repo = data["resource"]["pullRequest"]["repository"]["webUrl"]
                pr_url = unquote(f'{repo}/pullrequest/{data["resource"]["pullRequest"]["pullRequestId"]}')
                action = comment["content"]
                thread_url = comment["_links"]["threads"]["href"]
                thread_id = int(thread_url.split("/")[-1])
                comment_id = int(comment["id"])
                pass
            else:
                # API V1 not supported as it does not contain the PR URL
                return JSONResponse(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    content=json.dumps({"message": "version 1.0 webhook for Azure Devops PR comment is not supported. please upgrade to version 2.0"})),
        else:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content=json.dumps({"message": "Unsupported command"}),
            )
    else:
        return JSONResponse(
            status_code=status.HTTP_204_NO_CONTENT,
            content=json.dumps({"message": "Unsupported event"}),
        )

    log_context["event"] = data["eventType"]
    log_context["api_url"] = pr_url

    try:
        await handle_request_comment(pr_url, action, thread_id, comment_id, log_context)
    except Exception as e:
        get_logger().error("Azure DevOps Trigger failed. Error:" + str(e))
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=json.dumps({"message": "Internal server error"}),
        )
    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED, content=jsonable_encoder({"message": "webhook triggered successfully"})
    )

@router.post("/", dependencies=[Depends(authorize)])
async def handle_webhook(background_tasks: BackgroundTasks, request: Request):
    log_context = {"server_type": "azure_devops_server"}
    data = await request.json()
    # get_logger().info(json.dumps(data))

    background_tasks.add_task(handle_request_azure, data, log_context)

    return JSONResponse(
        status_code=status.HTTP_202_ACCEPTED, content=jsonable_encoder({"message": "webhook triggered successfully"})
    )

@router.get("/")
async def root():
    return {"status": "ok"}

def start():
    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(router)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))

if __name__ == "__main__":
    start()
