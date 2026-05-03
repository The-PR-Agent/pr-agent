import ast
import json
import os
import re
from typing import List

import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse
from starlette import status
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.servers.utils import verify_signature

setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
router = APIRouter()


def handle_request(
    background_tasks: BackgroundTasks, url: str, body: str, log_context: dict
):
    log_context["action"] = body
    log_context["api_url"] = url

    async def inner():
        try:
            with get_logger().contextualize(**log_context):
                await PRAgent().handle_request(url, body)
        except Exception as e:
            get_logger().error(f"Failed to handle webhook: {e}")

    background_tasks.add_task(inner)

def should_process_pr_logic(data) -> bool:
    try:
        pr_data = data.get("pullRequest", {})
        title = pr_data.get("title", "")
        
        from_ref = pr_data.get("fromRef", {})
        source_branch = from_ref.get("displayId", "") if from_ref else ""
        
        to_ref = pr_data.get("toRef", {})
        target_branch = to_ref.get("displayId", "") if to_ref else ""
        
        author = pr_data.get("author", {})
        user = author.get("user", {}) if author else {}
        sender = user.get("name", "") if user else ""
        
        repository = to_ref.get("repository", {}) if to_ref else {}
        project = repository.get("project", {}) if repository else {}
        project_key = project.get("key", "") if project else ""
        repo_slug = repository.get("slug", "") if repository else ""
        
        repo_full_name = f"{project_key}/{repo_slug}" if project_key and repo_slug else ""
        pr_id = pr_data.get("id", None)

        # To ignore PRs from specific repositories
        ignore_repos = get_settings().get("CONFIG.IGNORE_REPOSITORIES", [])
        if repo_full_name and ignore_repos:
            if any(re.search(regex, repo_full_name) for regex in ignore_repos):
                get_logger().info(f"Ignoring PR from repository '{repo_full_name}' due to 'config.ignore_repositories' setting")
                return False

        # To ignore PRs from specific users
        ignore_pr_users = get_settings().get("CONFIG.IGNORE_PR_AUTHORS", [])
        if ignore_pr_users and sender:
            if any(re.search(regex, sender) for regex in ignore_pr_users):
                get_logger().info(f"Ignoring PR from user '{sender}' due to 'config.ignore_pr_authors' setting")
                return False

        # To ignore PRs with specific titles
        if title:
            ignore_pr_title_re = get_settings().get("CONFIG.IGNORE_PR_TITLE", [])
            if not isinstance(ignore_pr_title_re, list):
                ignore_pr_title_re = [ignore_pr_title_re]
            if ignore_pr_title_re and any(re.search(regex, title) for regex in ignore_pr_title_re):
                get_logger().info(f"Ignoring PR with title '{title}' due to config.ignore_pr_title setting")
                return False

        ignore_pr_source_branches = get_settings().get("CONFIG.IGNORE_PR_SOURCE_BRANCHES", [])
        ignore_pr_target_branches = get_settings().get("CONFIG.IGNORE_PR_TARGET_BRANCHES", [])
        if (ignore_pr_source_branches or ignore_pr_target_branches):
            if any(re.search(regex, source_branch) for regex in ignore_pr_source_branches):
                get_logger().info(
                    f"Ignoring PR with source branch '{source_branch}' due to config.ignore_pr_source_branches settings")
                return False
            if any(re.search(regex, target_branch) for regex in ignore_pr_target_branches):
                get_logger().info(
                    f"Ignoring PR with target branch '{target_branch}' due to config.ignore_pr_target_branches settings")
                return False

        # Allow_only_specific_folders
        allowed_folders = get_settings().config.get("allow_only_specific_folders", [])
        if allowed_folders and pr_id and project_key and repo_slug:
            from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
            bitbucket_server_url = get_settings().get("BITBUCKET_SERVER.URL", "")
            pr_url = f"{bitbucket_server_url}/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}"
            provider = BitbucketServerProvider(pr_url=pr_url)
            changed_files = provider.get_files()
            if changed_files:
                # Check if ALL files are outside allowed folders
                all_files_outside = True
                for file_path in changed_files:
                    if any(file_path.startswith(folder) for folder in allowed_folders):
                        all_files_outside = False
                        break
                
                if all_files_outside:
                    get_logger().info(f"Ignoring PR because all files {changed_files} are outside allowed folders {allowed_folders}")
                    return False
    except Exception as e:
        get_logger().error(f"Failed 'should_process_pr_logic': {e}")
        return True # On exception - we continue. Otherwise, we could just end up with filtering all PRs
    return True

@router.post("/")
async def redirect_to_webhook():
    return RedirectResponse(url="/webhook")

@router.post("/webhook")
async def handle_webhook(background_tasks: BackgroundTasks, request: Request):
    log_context = {"server_type": "bitbucket_server"}
    data = await request.json()
    get_logger().info(json.dumps(data))

    webhook_secret = get_settings().get("BITBUCKET_SERVER.WEBHOOK_SECRET", None)
    if webhook_secret:
        body_bytes = await request.body()
        if body_bytes.decode('utf-8') == '{"test": true}':
            return JSONResponse(
                status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "connection test successful"})
            )
        signature_header = request.headers.get("x-hub-signature", None)
        verify_signature(body_bytes, webhook_secret, signature_header)

    pr_id = data["pullRequest"]["id"]
    repository_name = data["pullRequest"]["toRef"]["repository"]["slug"]
    project_name = data["pullRequest"]["toRef"]["repository"]["project"]["key"]
    bitbucket_server = get_settings().get("BITBUCKET_SERVER.URL")
    pr_url = f"{bitbucket_server}/projects/{project_name}/repos/{repository_name}/pull-requests/{pr_id}"

    log_context["api_url"] = pr_url
    log_context["event"] = "pull_request"

    commands_to_run = []

    if (data["eventKey"] == "pr:opened"
            or (data["eventKey"] == "repo:refs_changed" and data.get("pullRequest", {}).get("id", -1) != -1)):  # push event; -1 for push unassigned to a PR: #Check auto commands for creation/updating
        apply_repo_settings(pr_url)
        if not should_process_pr_logic(data):
            get_logger().info(f"PR ignored due to config settings", **log_context)
            return JSONResponse(
                status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "PR ignored by config"})
            )
        if get_settings().config.disable_auto_feedback:  # auto commands for PR, and auto feedback is disabled
            get_logger().info(f"Auto feedback is disabled, skipping auto commands for PR {pr_url}", **log_context)
            return JSONResponse(
                status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "PR ignored due to auto feedback not enabled"})
            )
        get_settings().set("config.is_auto_command", True)
        if data["eventKey"] == "pr:opened":
            commands_to_run.extend(_get_commands_list_from_settings('BITBUCKET_SERVER.PR_COMMANDS'))
        else: #Has to be: data["eventKey"] == "pr:from_ref_updated"
            if not get_settings().get("BITBUCKET_SERVER.HANDLE_PUSH_TRIGGER"):
                get_logger().info(f"Push trigger is disabled, skipping push commands for PR {pr_url}", **log_context)
                return JSONResponse(
                    status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "PR ignored due to push trigger not enabled"})
                )

            get_settings().set("config.is_new_pr", False)
            commands_to_run.extend(_get_commands_list_from_settings('BITBUCKET_SERVER.PUSH_COMMANDS'))
    elif data["eventKey"] == "pr:comment:added":
        commands_to_run.append(data["comment"]["text"])
    else:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=json.dumps({"message": "Unsupported event"}),
        )

    async def inner():
        try:
            await _run_commands_sequentially(commands_to_run, pr_url, log_context)
        except Exception as e:
            get_logger().error(f"Failed to handle webhook: {e}")

    background_tasks.add_task(inner)

    return JSONResponse(
        status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "success"})
    )


async def _run_commands_sequentially(commands: List[str], url: str, log_context: dict):
    get_logger().info(f"Running commands sequentially: {commands}")
    if commands is None:
        return

    for command in commands:
        try:
            body = _process_command(command, url)

            log_context["action"] = body
            log_context["api_url"] = url

            with get_logger().contextualize(**log_context):
                await PRAgent().handle_request(url, body)
        except Exception as e:
            get_logger().error(f"Failed to handle command: {command} , error: {e}")

def _process_command(command: str, url) -> str:
    # don't think we need this
    apply_repo_settings(url)
    # Process the command string
    split_command = command.split(" ")
    command = split_command[0]
    args = split_command[1:]
    # do I need this? if yes, shouldn't this be done in PRAgent?
    other_args = update_settings_from_args(args)
    new_command = ' '.join([command] + other_args)
    return new_command


def _to_list(command_string: str) -> list:
    try:
        # Use ast.literal_eval to safely parse the string into a list
        commands = ast.literal_eval(command_string)
        # Check if the parsed object is a list of strings
        if isinstance(commands, list) and all(isinstance(cmd, str) for cmd in commands):
            return commands
        else:
            raise ValueError("Parsed data is not a list of strings.")
    except (SyntaxError, ValueError, TypeError) as e:
        raise ValueError(f"Invalid command string: {e}")


def _get_commands_list_from_settings(setting_key: str) -> list:
    try:
        return get_settings().get(setting_key, [])
    except ValueError as e:
        get_logger().error(f"Failed to get commands list from settings {setting_key}: {e}")
        return []


@router.get("/")
async def root():
    return {"status": "ok"}


def start():
    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(router)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))


if __name__ == "__main__":
    start()
