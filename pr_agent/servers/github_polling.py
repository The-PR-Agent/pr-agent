import asyncio
import multiprocessing
import time
import traceback
from collections import deque
from datetime import datetime, timezone

import aiohttp
import requests

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider
from pr_agent.log import LoggingFormat, get_logger, setup_logger

setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
NOTIFICATION_URL = "https://api.github.com/notifications"


async def mark_notification_as_read(headers, notification, session):
    async with session.patch(
            f"https://api.github.com/notifications/threads/{notification['id']}",
            headers=headers) as mark_read_response:
        if mark_read_response.status != 205:
            get_logger().error(
                f"Failed to mark notification as read. Status code: {mark_read_response.status}")


def now() -> str:
    """
    Get the current UTC time in ISO 8601 format.

    Returns:
        str: The current UTC time in ISO 8601 format.
    """
    now_utc = datetime.now(timezone.utc).isoformat()
    now_utc = now_utc.replace("+00:00", "Z")
    return now_utc

async def async_handle_request(pr_url, rest_of_comment, comment_id, git_provider):
    agent = PRAgent()
    success = await agent.handle_request(
        pr_url,
        rest_of_comment,
        notify=lambda: git_provider.add_eyes_reaction(comment_id)
    )
    return success

def run_handle_request(pr_url, rest_of_comment, comment_id, git_provider):
    return asyncio.run(async_handle_request(pr_url, rest_of_comment, comment_id, git_provider))


def process_comment_sync(pr_url, rest_of_comment, comment_id):
    try:
        # Run the async handle_request in a separate function
        git_provider = get_git_provider()(pr_url=pr_url)
        success = run_handle_request(pr_url, rest_of_comment, comment_id, git_provider)
    except Exception as e:
        get_logger().error(f"Error processing comment: {e}", artifact={"traceback": traceback.format_exc()})


async def process_comment(pr_url, rest_of_comment, comment_id):
    try:
        git_provider = get_git_provider()(pr_url=pr_url)
        git_provider.set_pr(pr_url)
        agent = PRAgent()
        success = await agent.handle_request(
            pr_url,
            rest_of_comment,
            notify=lambda: git_provider.add_eyes_reaction(comment_id)
        )
        get_logger().info(f"Finished processing comment for PR: {pr_url}")
    except Exception as e:
        get_logger().error(f"Error processing comment: {e}", artifact={"traceback": traceback.format_exc()})

async def is_valid_notification(notification, headers, handled_ids, session, user_id):
    try:
        if 'reason' in notification and notification['reason'] == 'mention':
            if 'subject' in notification and notification['subject']['type'] == 'PullRequest':
                pr_url = notification['subject']['url']
                latest_comment = notification['subject']['latest_comment_url']
                if not latest_comment or not isinstance(latest_comment, str):
                    get_logger().debug(f"no latest_comment")
                    return False, handled_ids
                async with session.get(latest_comment, headers=headers) as comment_response:
                    check_prev_comments = False
                    user_tag = "@" + user_id
                    if comment_response.status == 200:
                        comment = await comment_response.json()
                        if 'id' in comment:
                            if comment['id'] in handled_ids:
                                get_logger().debug(f"comment['id'] in handled_ids")
                                return False, handled_ids
                            else:
                                handled_ids.add(comment['id'])
                        if 'user' in comment and 'login' in comment['user']:
                            if comment['user']['login'] == user_id:
                                get_logger().debug(f"comment['user']['login'] == user_id")
                                check_prev_comments = True
                        comment_body = comment.get('body', '')
                        if not comment_body:
                            get_logger().debug(f"no comment_body")
                            check_prev_comments = True
                        else:
                            if user_tag not in comment_body:
                                get_logger().debug(f"user_tag not in comment_body")
                                check_prev_comments = True
                            else:
                                get_logger().info(f"Polling, pr_url: {pr_url}",
                                                  artifact={"comment": comment_body})

                        if not check_prev_comments:
                            return True, handled_ids, comment, comment_body, pr_url, user_tag
                        else: # we could not find the user tag in the latest comment. Check previous comments
                            # get all comments in the PR
                            requests_url = f"{pr_url}/comments".replace("pulls", "issues")
                            comments_response = requests.get(requests_url, headers=headers)
                            comments = comments_response.json()[::-1]
                            max_comment_to_scan = 4
                            for comment in comments[:max_comment_to_scan]:
                                if 'user' in comment and 'login' in comment['user']:
                                    if comment['user']['login'] == user_id:
                                        continue
                                comment_body = comment.get('body', '')
                                if not comment_body:
                                    continue
                                if user_tag in comment_body:
                                    get_logger().info("found user tag in previous comments")
                                    get_logger().info(f"Polling, pr_url: {pr_url}",
                                                      artifact={"comment": comment_body})
                                    return True, handled_ids, comment, comment_body, pr_url, user_tag

                            get_logger().warning(f"Failed to fetch comments for PR: {pr_url}",
                                                    artifact={"comments": comments})
                            return False, handled_ids

        return False, handled_ids
    except Exception as e:
        get_logger().exception(f"Error processing polling notification",
                               artifact={"notification": notification, "error": e})
        return False, handled_ids



async def polling_loop():
    """
    Polls for notifications and handles them accordingly.
    """
    handled_ids = set()
    since = [now()]
    last_modified = [None]
    git_provider = get_git_provider()()
    user_id = git_provider.get_user_id()
    get_settings().set("CONFIG.PUBLISH_OUTPUT_PROGRESS", False)
    get_settings().set("pr_description.publish_description_as_comment", True)

    try:
        deployment_type = get_settings().github.deployment_type
        token = get_settings().github.user_token
    except AttributeError:
        deployment_type = 'none'
        token = None

    if deployment_type != 'user':
        raise ValueError("Deployment mode must be set to 'user' to get notifications")
    if not token:
        raise ValueError("User token must be set to get notifications")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await asyncio.sleep(5)
                headers = {
                    "Accept": "application/vnd.github.v3+json",
                    "Authorization": f"Bearer {token}"
                }
                params = {
                    "participating": "true"
                }
                if since[0]:
                    params["since"] = since[0]
                if last_modified[0]:
                    headers["If-Modified-Since"] = last_modified[0]

                async with session.get(NOTIFICATION_URL, headers=headers, params=params) as response:
                    if response.status == 200:
                        if 'Last-Modified' in response.headers:
                            last_modified[0] = response.headers['Last-Modified']
                            since[0] = None
                        notifications = await response.json()
                        if not notifications:
                            continue
                        get_logger().info(f"Received {len(notifications)} notifications")
                        task_queue = deque()
                        for notification in notifications:
                            if not notification:
                                continue
                            # mark notification as read
                            await mark_notification_as_read(headers, notification, session)

                            handled_ids.add(notification['id'])
                            output = await is_valid_notification(notification, headers, handled_ids, session, user_id)
                            if output[0]:
                                _, handled_ids, comment, comment_body, pr_url, user_tag = output
                                rest_of_comment = comment_body.split(user_tag)[1].strip()
                                comment_id = comment['id']

                                # Add to the task queue
                                get_logger().info(
                                    f"Adding comment processing to task queue for PR, {pr_url}, comment_body: {comment_body}")
                                task_queue.append((process_comment_sync, (pr_url, rest_of_comment, comment_id)))
                                get_logger().info(f"Queued comment processing for PR: {pr_url}")
                            else:
                                get_logger().debug(f"Skipping comment processing for PR")

                        max_allowed_parallel_tasks = 10
                        if task_queue:
                            processes = []
                            for i, (func, args) in enumerate(task_queue):  # Create  parallel tasks
                                p = multiprocessing.Process(target=func, args=args)
                                processes.append(p)
                                p.start()
                                if i > max_allowed_parallel_tasks:
                                    get_logger().error(
                                        f"Dropping {len(task_queue) - max_allowed_parallel_tasks} tasks from polling session")
                                    break
                            task_queue.clear()

                            # Don't wait for all processes to complete. Move on to the next iteration
                            # for p in processes:
                            #     p.join()

                    elif response.status != 304:
                        print(f"Failed to fetch notifications. Status code: {response.status}")

            except Exception as e:
                get_logger().error(f"Polling exception during processing of a notification: {e}",
                                   artifact={"traceback": traceback.format_exc()})


if __name__ == '__main__':
    asyncio.run(polling_loop())
