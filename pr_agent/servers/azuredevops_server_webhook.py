# This file contains the code for the Azure DevOps Server webhook server.
# The server listens for incoming webhooks from Azure DevOps Server and forwards them to the PR Agent.
# ADO webhook documentation: https://learn.microsoft.com/en-us/azure/devops/service-hooks/services/webhooks?view=azure-devops

import copy
import json
import os
import re
from urllib.parse import quote, unquote

import uvicorn
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from starlette import status
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette_context import context
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent, command2class, prepare_command
from pr_agent.algo.utils import encode_user_text_arg
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers import get_git_provider_with_context
from pr_agent.git_providers.azuredevops_provider import AZURE_AGENT_RESPONSE_MARKER, AzureDevopsProvider
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.servers.utils import basic_auth_matches

setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
security = HTTPBasic(auto_error=False)
router = APIRouter()
available_commands_rgx = re.compile(r"^\/(" + "|".join(command2class.keys()) + r")\s*")
# Match current Markdown mentions and legacy HTML mentions.
_AZURE_MARKDOWN_MENTION_PATTERN = re.compile(
    r"^@<(?P<identity>[^>\r\n]+)>\s*(?:[:,\-]\s*)?(?P<question>\S.*)$",
    re.DOTALL,
)
_AZURE_HTML_MENTION_PATTERN = re.compile(
    r"^<a\b(?P<attributes>[^>]*)>(?P<label>.*?)</a>\s*(?:[:,\-]\s*)?(?P<question>\S.*)$",
    re.IGNORECASE | re.DOTALL,
)
_AZURE_HTML_MENTION_ID_PATTERN = re.compile(
    r"data-vss-mention=[\"']version:2\.0,(?P<identity>[^\"']+)[\"']",
    re.IGNORECASE,
)
_CLI_ARG_PATTERN = re.compile(
    r"""(?P<arg>--[A-Za-z0-9_.]+=(?:"[^"]*"|'[^']*'|\S*))(?:\s+|$)"""
)

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
            body = handle_line_comment(body, thread_id, comment_id, provider)
            if body is None:
                return
            is_question = body.startswith("/ask")
            handled = await agent.handle_request(
                url, body, notify=lambda: provider.reply_to_thread(thread_id, "On it! ⏳", True)
            )
            if handled and not is_question:
                provider.set_thread_status(thread_id, "closed")
            if handled:
                provider.remove_initial_comment()
    except Exception as e:
        get_logger().exception("Failed to handle webhook", artifact={"url": url, "body": body}, error=str(e))

def extract_azure_mention(body: str):
    if not isinstance(body, str):
        return None
    if AZURE_AGENT_RESPONSE_MARKER in body:
        return None
    text = body.strip()
    match = _AZURE_MARKDOWN_MENTION_PATTERN.match(text)
    if match:
        return {match["identity"].strip()}, match["question"].strip()
    match = _AZURE_HTML_MENTION_PATTERN.match(text)
    if match:
        mention_id = _AZURE_HTML_MENTION_ID_PATTERN.search(match["attributes"])
        if not mention_id:
            return None
        label = re.sub(r"<[^>]+>", "", match["label"]).strip().lstrip("@").strip()
        identities = {mention_id["identity"].strip()}
        if label:
            identities.add(label)
        return identities, match["question"].strip()
    return None


def extract_agent_question(body: str, aliases=()):
    mention = extract_azure_mention(body)
    if not mention:
        return None
    identities, question = mention
    normalized_aliases = {str(alias).strip().casefold() for alias in aliases if str(alias).strip()}
    if any(identity.casefold() in normalized_aliases for identity in identities):
        return question
    return None


def _split_leading_cli_args(text: str) -> tuple[str, str]:
    cli_args = []
    remainder = text.lstrip()
    while True:
        match = _CLI_ARG_PATTERN.match(remainder)
        if not match:
            break
        cli_args.append(match["arg"].strip())
        remainder = remainder[match.end():]
    return " ".join(cli_args), remainder.strip()


def handle_line_comment(body: str, thread_id: int, comment_id: int, provider: AzureDevopsProvider):
    if not isinstance(body, str):
        return None
    body = body.strip()
    cli_args = ""
    if body == "/ask" or body.startswith("/ask "):
        cli_args, question = _split_leading_cli_args(body[5:])
    elif available_commands_rgx.match(body):
        return body
    else:
        # Mention text is untrusted free-form input, so it is never parsed for overrides.
        question = extract_agent_question(body, provider.get_agent_mention_aliases())
        if not question:
            return None
    encoded_question = encode_user_text_arg(question) if question else ""
    threaded_question = " ".join(part for part in (
        f"/ask --comment_id={thread_id} --origin_comment_id={comment_id}",
        cli_args,
        encoded_question,
    ) if part)
    thread_context = provider.get_thread_context(thread_id)
    if not thread_context:
        return threaded_question

    path = getattr(thread_context, "file_path", None)
    left_start = getattr(thread_context, "left_file_start", None)
    left_end = getattr(thread_context, "left_file_end", None)
    right_start = getattr(thread_context, "right_file_start", None)
    right_end = getattr(thread_context, "right_file_end", None)
    if left_end or left_start:
        start_position = left_start or left_end
        end_position = left_end or start_position
        side = "left"
    elif right_end or right_start:
        start_position = right_start or right_end
        end_position = right_end or start_position
        side = "right"
    else:
        get_logger().info("No line range found in thread context", artifact={"thread_context": thread_context})
        return threaded_question

    start_line = getattr(start_position, "line", None)
    end_line = getattr(end_position, "line", None)
    if (not isinstance(path, str) or not path or not isinstance(start_line, int)
            or isinstance(start_line, bool) or not isinstance(end_line, int)
            or isinstance(end_line, bool) or start_line < 1 or end_line < start_line):
        get_logger().info("Invalid line range in thread context", artifact={"thread_context": thread_context})
        return threaded_question

    encoded_path = quote(path, safe="")
    return " ".join(part for part in (
        f"/ask_line --line_start={start_line} --line_end={end_line} --side={side} "
        f"--file_name={encoded_path} --file_name_encoded=true --comment_id={thread_id} "
        f"--origin_comment_id={comment_id}",
        cli_args,
        encoded_question,
    ) if part)

# currently only basic auth is supported with azure webhooks
# for this reason, https must be enabled to ensure the credentials are not sent in clear text
def authorize(credentials: HTTPBasicCredentials = Depends(security)):
    if not WEBHOOK_USERNAME and not WEBHOOK_PASSWORD:
        return
    if not WEBHOOK_USERNAME or not WEBHOOK_PASSWORD:
        # Fail closed on a half-configured pair rather than reverting to open access.
        get_logger().error("Incomplete azure_devops_server webhook credentials: set both "
                           "webhook_username and webhook_password, or neither")
        raise HTTPException(status_code=500, detail="Webhook authentication is misconfigured.")

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing credentials.",
            headers={"WWW-Authenticate": "Basic"},
        )

    if not basic_auth_matches(credentials, WEBHOOK_USERNAME, WEBHOOK_PASSWORD):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Incorrect username or password.',
            headers={'WWW-Authenticate': 'Basic'},
        )


async def _perform_commands_azure(commands_conf: str, agent: PRAgent, api_url: str, log_context: dict):
    apply_repo_settings(api_url)
    if commands_conf == "pr_commands" and get_settings().config.disable_auto_feedback:  # auto commands for PR, and auto feedback is disabled
        get_logger().info(f"Auto feedback is disabled, skipping auto commands for PR {api_url=}", **log_context)
        return
    commands = get_settings().get(f"azure_devops_server.{commands_conf}")
    if not commands:
        return

    get_settings().set("config.is_auto_command", True)
    for command in commands:
        try:
            new_command = prepare_command(command)
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
        comment_content = comment["content"]
        if (isinstance(comment_content, str)
                and (available_commands_rgx.match(comment_content) or extract_azure_mention(comment_content))):
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
                status_code=status.HTTP_204_NO_CONTENT,
                content=json.dumps({"message": "Comment does not address PR-Agent"}),
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
    context["settings"] = copy.deepcopy(global_settings)
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
