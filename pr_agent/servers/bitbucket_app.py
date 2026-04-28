import base64
import copy
import hashlib
import json
import os
import re
import time

import jwt
import requests
import uvicorn
from fastapi import APIRouter, FastAPI, Request, Response
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette_context import context
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.identity_providers import get_identity_provider
from pr_agent.identity_providers.identity_provider import Eligibility
from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.secret_providers import get_secret_provider

setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
router = APIRouter()
secret_provider = get_secret_provider() if get_settings().get("CONFIG.SECRET_PROVIDER") else None


async def get_bearer_token(shared_secret: str, client_key: str):
    try:
        now = int(time.time())
        url = "https://bitbucket.org/site/oauth2/access_token"
        canonical_url = "GET&/site/oauth2/access_token&"
        qsh = hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()
        app_key = get_settings().bitbucket.app_key

        payload = {
            "iss": app_key,
            "iat": now,
            "exp": now + 240,
            "qsh": qsh,
            "sub": client_key,
            }
        token = jwt.encode(payload, shared_secret, algorithm="HS256")
        payload = 'grant_type=urn%3Abitbucket%3Aoauth2%3Ajwt'
        headers = {
            'Authorization': f'JWT {token}',
            'Content-Type': 'application/x-www-form-urlencoded'
        }
        response = requests.request("POST", url, headers=headers, data=payload)
        bearer_token = response.json()["access_token"]
        return bearer_token
    except Exception as e:
        get_logger().error(f"Failed to get bearer token: {e}")
        raise e

@router.get("/")
async def handle_manifest(request: Request, response: Response):
    cur_dir = os.path.dirname(os.path.abspath(__file__))
    manifest = open(os.path.join(cur_dir, "atlassian-connect.json"), "rt").read()
    try:
        manifest = manifest.replace("app_key", get_settings().bitbucket.app_key)
        manifest = manifest.replace("base_url", get_settings().bitbucket.base_url)
    except:
        get_logger().error("Failed to replace api_key in Bitbucket manifest, trying to continue")
    manifest_obj = json.loads(manifest)
    return JSONResponse(manifest_obj)


def _get_username(data):
    actor = data.get("data", {}).get("actor", {})
    if actor:
        if "username" in actor:
            return actor["username"]
        elif "display_name" in actor:
            return actor["display_name"]
        elif "nickname" in actor:
            return actor["nickname"]
    return ""


async def _validate_time_from_last_commit_to_pr_update(data: dict) -> bool:
    is_valid_push = False
    try:
        data_inner = data.get('data', {})
        if not data_inner:
            get_logger().error("No data found in the webhook payload")
            return True
        pull_request = data_inner.get('pullrequest', {})
        commits_api = pull_request.get('links', {}).get('commits', {}).get('href')
        if not commits_api:
            return False
        if not pull_request.get('updated_on'):
            return False
        bearer_token = context.get('bitbucket_bearer_token')
        headers = {
            'Authorization': f'Bearer {bearer_token}',
            'Accept': 'application/json'
        }
        response = requests.get(commits_api, headers=headers)
        if response.status_code != 200:
            get_logger().warning(f"Bitbucket commits API returned {response.status_code} for {commits_api}")
            return False

        username =_get_username(data)
        commits_data = response.json() or {}
        values = commits_data.get('values') or []
        if (not values or not isinstance(values, list) or not values[0].get('author') or not values[0]['author'].get('user')
                or not values[0]['author']['user'].get('display_name')):
            get_logger().warning("No commits returned for pull request or one of the required fields missing; skipping push validation",
                                 artifact={'values': values})
            return False
        commit_username = commits_data['values'][0]['author']['user']['display_name']
        if username != commit_username:
            get_logger().warning(f"Mismatch in username {username} vs. commit_username {commit_username}")
            return False

        time_pr_updated = pull_request['updated_on']
        time_last_commit = commits_data['values'][0]['date']
        from datetime import datetime
        ts1 = datetime.fromisoformat(time_pr_updated)
        ts2 = datetime.fromisoformat(time_last_commit)
        diff = (ts1 - ts2).total_seconds()
        max_delta_seconds = 15
        if diff > 0 and diff < max_delta_seconds:
            is_valid_push = True
        else:
            get_logger().debug(f"Too much time passed since last commit",
                               artifact={'updated': time_pr_updated, 'last_commit': time_last_commit})
    except Exception as e:
        get_logger().exception(f"Failed to validate time difference between last commit and PR update",
                               artifact={'error': e, 'data': data})
    return is_valid_push

async def _perform_commands_bitbucket(commands_conf: str, agent: PRAgent, api_url: str, log_context: dict, data: dict):
    apply_repo_settings(api_url)
    if get_settings().config.disable_auto_feedback:
        get_logger().info(f"Auto feedback is disabled, skipping auto commands for {api_url=}")
        return
    if commands_conf == "push_commands":
        if not get_settings().get("bitbucket_app.handle_push_trigger"):
            get_logger().info(
                "Bitbucket push trigger handling disabled via config; skipping push commands")
            return
    if data.get("event", "") == "pullrequest:created":
        if not should_process_pr_logic(data):
            return
    commands = get_settings().get(f"bitbucket_app.{commands_conf}", {})
    get_settings().set("config.is_auto_command", True)
    if commands_conf == "push_commands":
        is_valid_push = await _validate_time_from_last_commit_to_pr_update(data)
        if not is_valid_push:
            get_logger().info(f"Bitbucket skipping 'pullrequest:updated' for push commands")
            return
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


def is_bot_user(data) -> bool:
    try:
        actor = data.get("data", {}).get("actor", {})
        # allow actor type: user . if it's "AppUser" or "team" then it is a bot user
        allowed_actor_types = {"user"}
        if actor and actor["type"].lower() not in allowed_actor_types:
            get_logger().info(f"BitBucket actor type is not 'user', skipping: {actor}")
            return True
    except Exception as e:
        get_logger().error(f"Failed 'is_bot_user' logic: {e}")
    return False


def should_process_pr_logic(data) -> bool:
    try:
        pr_data = data.get("data", {}).get("pullrequest", {})
        title = pr_data.get("title", "")
        source_branch = pr_data.get("source", {}).get("branch", {}).get("name", "")
        target_branch = pr_data.get("destination", {}).get("branch", {}).get("name", "")
        sender = _get_username(data)
        repo_full_name = pr_data.get("destination", {}).get("repository", {}).get("full_name", "")

        # logic to ignore PRs from specific repositories
        ignore_repos = get_settings().get("CONFIG.IGNORE_REPOSITORIES", [])
        if repo_full_name and ignore_repos:
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
    except Exception as e:
        get_logger().error(f"Failed 'should_process_pr_logic': {e}")
    return True


@router.post("/webhook")
async def handle_github_webhooks(background_tasks: BackgroundTasks, request: Request):
    app_name = get_settings().get("CONFIG.APP_NAME", "Unknown")
    log_context = {"server_type": "bitbucket_app", "app_name": app_name}
    get_logger().debug(request.headers)
    jwt_header = request.headers.get("authorization", None)
    if jwt_header:
        input_jwt = jwt_header.split(" ")[1]
    data = await request.json()
    get_logger().debug(data)

    async def inner():
        try:
            # ignore bot users
            if is_bot_user(data):
                return "OK"

            # Check if the PR should be processed
            if data.get("event", "") == "pullrequest:created":
                if not should_process_pr_logic(data):
                    return "OK"

            # Get the username of the sender
            log_context["sender"] = _get_username(data)

            sender_id = data.get("data", {}).get("actor", {}).get("account_id", "")
            log_context["sender_id"] = sender_id
            jwt_parts = input_jwt.split(".")
            claim_part = jwt_parts[1]
            claim_part += "=" * (-len(claim_part) % 4)
            decoded_claims = base64.urlsafe_b64decode(claim_part)
            claims = json.loads(decoded_claims)
            client_key = claims["iss"]
            secrets = json.loads(secret_provider.get_secret(client_key))
            shared_secret = secrets["shared_secret"]
            jwt.decode(input_jwt, shared_secret, audience=client_key, algorithms=["HS256"])
            bearer_token = await get_bearer_token(shared_secret, client_key)
            context['bitbucket_bearer_token'] = bearer_token
            context["settings"] = copy.deepcopy(global_settings)
            event = data["event"]
            agent = PRAgent()
            if event == "pullrequest:created":
                pr_url = data["data"]["pullrequest"]["links"]["html"]["href"]
                log_context["api_url"] = pr_url
                log_context["event"] = "pull_request"
                if pr_url:
                    with get_logger().contextualize(**log_context):
                        if get_identity_provider().verify_eligibility("bitbucket",
                                                        sender_id, pr_url) is not Eligibility.NOT_ELIGIBLE:
                            if get_settings().get("bitbucket_app.pr_commands"):
                                await _perform_commands_bitbucket("pr_commands", agent, pr_url, log_context, data)
            elif event == "pullrequest:updated": # PR updated, might be from a push (we will validate this later)
                pr_url = data["data"]["pullrequest"]["links"]["html"]["href"]
                log_context["api_url"] = pr_url
                log_context["event"] = "pull_request"
                if pr_url:
                    with get_logger().contextualize(**log_context):
                        if get_identity_provider().verify_eligibility("bitbucket",
                                                        sender_id, pr_url) is not Eligibility.NOT_ELIGIBLE:

                            if get_settings().get("bitbucket_app.push_commands"):
                                await _perform_commands_bitbucket("push_commands", agent, pr_url, log_context, data)
            elif event == "pullrequest:comment_created":
                pr_url = data["data"]["pullrequest"]["links"]["html"]["href"]
                log_context["api_url"] = pr_url
                log_context["event"] = "comment"
                comment_body = data["data"]["comment"]["content"]["raw"]
                with get_logger().contextualize(**log_context):
                    if get_identity_provider().verify_eligibility("bitbucket",
                                                                     sender_id, pr_url) is not Eligibility.NOT_ELIGIBLE:
                        await agent.handle_request(pr_url, comment_body)
        except Exception as e:
            get_logger().error(f"Failed to handle webhook: {e}")
    background_tasks.add_task(inner)
    return "OK"

@router.get("/webhook")
async def handle_github_webhooks(request: Request, response: Response):
    return "Webhook server online!"

@router.post("/installed")
async def handle_installed_webhooks(request: Request, response: Response):
    try:
        get_logger().info("handle_installed_webhooks")
        get_logger().info(request.headers)
        data = await request.json()
        get_logger().info(data)
        shared_secret = data["sharedSecret"]
        client_key = data["clientKey"]
        username = data["principal"]["username"]
        secrets = {
            "shared_secret": shared_secret,
            "client_key": client_key
        }
        secret_provider.store_secret(username, json.dumps(secrets))
    except Exception as e:
        get_logger().error(f"Failed to register user: {e}")
        return JSONResponse({"error": "Unable to register user"}, status_code=500)

@router.post("/uninstalled")
async def handle_uninstalled_webhooks(request: Request, response: Response):
    get_logger().info("handle_uninstalled_webhooks")

    data = await request.json()
    get_logger().info(data)


def start():
    get_settings().set("CONFIG.PUBLISH_OUTPUT_PROGRESS", False)
    get_settings().set("CONFIG.GIT_PROVIDER", "bitbucket")
    get_settings().set("PR_DESCRIPTION.PUBLISH_DESCRIPTION_AS_COMMENT", True)
    middleware = [Middleware(RawContextMiddleware)]
    app = FastAPI(middleware=middleware)
    app.include_router(router)

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "3000")))


if __name__ == '__main__':
    start()
