import asyncio
import base64
import binascii
import copy
import hashlib
import json
import math
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

from pr_agent.agent.pr_agent import PRAgent, prepare_command
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.identity_providers import get_identity_provider
from pr_agent.identity_providers.identity_provider import Eligibility
from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.secret_providers import get_secret_provider, validate_secret_provider_setting
from pr_agent.servers.utils import get_pr_commands

setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
router = APIRouter()


validate_secret_provider_setting()

_secret_provider_state = {}


def _get_request_timeout():
    """Return the host-controlled timeout for offloaded Bitbucket App requests."""
    timeout = global_settings.get("bitbucket_app.request_timeout")
    if isinstance(timeout, bool):
        raise ValueError("bitbucket_app.request_timeout must be a positive finite number")
    try:
        timeout = float(timeout)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("bitbucket_app.request_timeout must be a positive finite number") from None
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("bitbucket_app.request_timeout must be a positive finite number")
    return timeout


def get_fork_safe_secret_provider():
    """Return this process's secret provider, building it on first use after a fork."""
    pid = os.getpid()
    if _secret_provider_state.get("pid") != pid:
        _secret_provider_state["provider"] = get_secret_provider()
        _secret_provider_state["pid"] = pid
    return _secret_provider_state["provider"]


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
        response = await asyncio.to_thread(
            requests.request,
            "POST",
            url,
            headers=headers,
            data=payload,
            timeout=_get_request_timeout(),
        )
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
        response = await asyncio.to_thread(
            requests.get,
            commits_api,
            headers=headers,
            timeout=_get_request_timeout(),
        )
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
            get_logger().debug("Too much time passed since last commit",
                               artifact={'updated': time_pr_updated, 'last_commit': time_last_commit})
    except Exception as e:
        get_logger().exception("Failed to validate time difference between last commit and PR update",
                               artifact={'error': e, 'data': data})
    return is_valid_push

async def _perform_commands_bitbucket(commands_conf: str, agent: PRAgent, api_url: str, log_context: dict, data: dict):
    apply_repo_settings(api_url)
    if commands_conf == "pr_commands" and get_settings().config.disable_auto_feedback:  # auto commands for PR, and auto feedback is disabled
        get_logger().info(f"Auto feedback is disabled, skipping auto commands for PR {api_url=}")
        return
    if commands_conf == "push_commands":
        if not get_settings().get("bitbucket_app.handle_push_trigger"):
            get_logger().info(
                "Bitbucket push trigger handling disabled via config; skipping push commands")
            return
    if data.get("event", "") == "pullrequest:created":
        if not should_process_pr_logic(data):
            return
    commands = (
        get_pr_commands("bitbucket_app")
        if commands_conf == "pr_commands"
        else get_settings().get(f"bitbucket_app.{commands_conf}", {})
    )
    get_settings().set("config.is_auto_command", True)
    if commands_conf == "push_commands":
        is_valid_push = await _validate_time_from_last_commit_to_pr_update(data)
        if not is_valid_push:
            get_logger().info("Bitbucket skipping 'pullrequest:updated' for push commands")
            return
    for command in commands:
        try:
            new_command = prepare_command(command)
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
    jwt_header = request.headers.get("authorization", None)
    jwt_parts = jwt_header.split() if jwt_header else []
    if len(jwt_parts) != 2 or jwt_parts[0].casefold() != "jwt":
        get_logger().error("Bitbucket webhook authorization header is malformed")
        return "OK"
    input_jwt = jwt_parts[1]
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
            # Decode the claims first so we can look up the shared secret, but
            # treat the JWT as untrusted until jwt.decode() validates its
            # signature against that secret. Using the unverified iss claim as
            # the audience parameter (the previous behavior) meant the audience
            # check was effectively tautological: an attacker could forge a JWT
            # with iss == aud and skip signature verification by supplying their
            # own secret via secret_provider. Look up the secret by the
            # unverified iss, then validate the JWT with a fixed audience so
            # the signature check actually rejects forged tokens.
            jwt_parts = input_jwt.split(".")
            if len(jwt_parts) < 2:
                get_logger().error("Bitbucket webhook JWT is malformed (missing segments)")
                return
            try:
                claim_part = jwt_parts[1]
                claim_part += "=" * (-len(claim_part) % 4)
                decoded_claims = json.loads(base64.urlsafe_b64decode(claim_part))
            except (binascii.Error, ValueError, json.JSONDecodeError, UnicodeDecodeError) as e:
                get_logger().error(f"Bitbucket webhook JWT claims could not be decoded: {e}")
                return
            if not isinstance(decoded_claims, dict):
                get_logger().error("Bitbucket webhook JWT claims are not a JSON object")
                return
            client_key = decoded_claims.get("iss", "")
            if not client_key or not isinstance(client_key, str):
                get_logger().error("Bitbucket webhook JWT is missing 'iss' claim")
                return
            try:
                secrets = json.loads(get_fork_safe_secret_provider().get_secret(client_key))
                shared_secret = secrets["shared_secret"]
            except Exception as e:
                get_logger().error(f"Failed to look up Bitbucket shared secret: {e}")
                return
            # Atlassian Connect issues JWTs with aud == uri of the app descriptor.
            # Pin the audience to the configured base_url so a forged JWT cannot
            # satisfy the audience check by mirroring its own iss. Guard against
            # the key being absent (it's not in the shipped .secrets_template.toml)
            # so a missing-config deployment fails cleanly instead of raising
            # AttributeError on every webhook and rejecting valid tokens.
            try:
                expected_audience = get_settings().bitbucket.base_url
            except AttributeError:
                get_logger().error(
                    "Bitbucket webhook JWT validation skipped: bitbucket.base_url is not configured"
                )
                return
            if not expected_audience:
                get_logger().error(
                    "Bitbucket webhook JWT validation skipped: bitbucket.base_url is empty"
                )
                return
            try:
                jwt.decode(input_jwt, shared_secret, audience=expected_audience, algorithms=["HS256"])
            except jwt.InvalidTokenError as e:
                get_logger().error(f"Bitbucket webhook JWT validation failed: {e}")
                return
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
                            if get_pr_commands("bitbucket_app"):
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
        data = await request.json()
        shared_secret = data["sharedSecret"]
        client_key = data["clientKey"]
        username = data["principal"]["username"]
        secrets = {
            "shared_secret": shared_secret,
            "client_key": client_key
        }
        get_fork_safe_secret_provider().store_secret(username, json.dumps(secrets))
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
