"""Ack-then-work hand-off for the Lambda webhook deployments (#4748).

Under Mangum the Lambda result is only produced once the whole ASGI call —
background tasks included — has finished, and Lambda freezes the container on
return.  A webhook sender that expects its acknowledgement within ~10 seconds
therefore records the delivery as timed out while the review is still running.
The Lambda webhook images instead split the work over two invocations:

1. The first invocation runs the webhook route up to and including signature
   verification, re-invokes the *same* Lambda function asynchronously with the
   verified payload and the headers it needs (``lambda:InvokeFunction`` with
   ``InvocationType="Event"``), and returns 200 immediately.
2. The second invocation recognises the hand-off event and runs the webhook
   dispatcher (``github_app.handle_request`` / the GitLab event processor) to
   completion.  Nobody waits for its result: the asynchronous invocation
   response is discarded, and an exception in it is surfaced by Lambda's
   asynchronous-invocation error handling (retries, then the DLQ if one is
   configured) instead of a webhook sender.

The execution role needs ``lambda:InvokeFunction`` on the function itself, and
the asynchronous invocation payload is limited to 256 KB.  When the invoke
fails for either reason, the caller falls back to processing the webhook
inline, which restores the previous behaviour — a slow acknowledgement and a
timed-out delivery — so a verified webhook is never dropped.

The delivery and event UUIDs are carried across the hand-off so the existing
webhook dedup keys keep working for redeliveries.
"""

import asyncio
import copy
import json
import os

from starlette_context import context, request_cycle_context

from pr_agent.config_loader import global_settings
from pr_agent.log import get_logger

HANDOFF_EVENT_KEY = "pr_agent_lambda_handoff"

# Headers the second invocation needs. The delivery and event UUIDs keep the
# existing webhook dedup keys working across the hand-off. Credentials must
# not be retained in async events or their failure destinations.
_GITHUB_HANDOFF_HEADERS = ("x-github-event", "x-github-delivery")
_GITLAB_HANDOFF_HEADERS = ("x-gitlab-event-uuid",)


def handoff_enabled() -> bool:
    """True inside the Lambda runtime, where the hand-off applies.

    ``AWS_LAMBDA_FUNCTION_NAME`` is set by the Lambda runtime only, so the
    uvicorn/gunicorn deployments keep the background-task behaviour.
    """
    return bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


def build_handoff_event(provider: str, body, headers, *, gitlab_secret_name: str | None = None) -> dict:
    """Build the second invocation's event from a verified webhook."""
    wanted = _GITHUB_HANDOFF_HEADERS if provider == "github" else _GITLAB_HANDOFF_HEADERS
    carried = {}
    for name in wanted:
        value = headers.get(name)
        if value is not None:
            carried[name] = value
    handoff = {"provider": provider, "body": body, "headers": carried}
    if provider == "gitlab":
        handoff["gitlab_auth"] = {"secret_name": gitlab_secret_name}
    return {HANDOFF_EVENT_KEY: handoff}


def extract_handoff(event):
    """Return the hand-off payload when ``event`` is a self-invocation, else None."""
    if not isinstance(event, dict):
        return None
    handoff = event.get(HANDOFF_EVENT_KEY)
    if isinstance(handoff, dict) and handoff.get("provider") in ("github", "gitlab"):
        return handoff
    return None


def _invoke_self_async(event: dict) -> None:
    """Re-invoke this Lambda function asynchronously with the hand-off event."""
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME")
    if not function_name:
        raise RuntimeError("AWS_LAMBDA_FUNCTION_NAME is not set; cannot hand the webhook off")
    import boto3  # imported lazily: only the Lambda images install it

    boto3.client("lambda").invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(event).encode("utf-8"),
    )


async def try_handoff(provider: str, body, headers, *, gitlab_secret_name: str | None = None) -> bool:
    """Hand a verified webhook to a second invocation; True when it was accepted.

    Failures — a missing IAM ``lambda:InvokeFunction`` permission, a payload over
    the 256 KB asynchronous-invocation quota, throttling — are logged and
    returned as False so the caller can process the webhook inline instead.
    """
    event = build_handoff_event(provider, body, headers, gitlab_secret_name=gitlab_secret_name)
    headers_carried = event[HANDOFF_EVENT_KEY]["headers"]
    delivery = headers_carried.get("x-github-delivery") or headers_carried.get("x-gitlab-event-uuid")
    try:
        await asyncio.to_thread(_invoke_self_async, event)
    except Exception as e:
        get_logger().error(
            "Could not hand the webhook off to a second invocation; it will be processed inline",
            artifact={"provider": provider, "delivery": delivery, "error": e},
        )
        return False
    get_logger().info(
        "Handed the webhook off to a second invocation",
        artifact={"provider": provider, "delivery": delivery},
    )
    return True


def run_lambda_handler(event, context, provider: str, http_handler):
    """Lambda entry point: hand-off events go to the dispatcher, others through Mangum."""
    handoff = extract_handoff(event)
    if handoff is None:
        return http_handler(event, context)
    return _run_handoff_on_event_loop(handoff)


def _run_handoff_on_event_loop(handoff: dict):
    """Run the hand-off coroutine on this thread's event loop, like Mangum does.

    ``asyncio.run`` clears the thread's current loop when it finishes, which
    would break Mangum's ``asyncio.get_event_loop()`` on the next invocation
    of a warm Lambda container, so an existing loop is reused and left set.
    """
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = None
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(_run_handoff(handoff))


async def _run_handoff(handoff: dict) -> dict:
    """Dispatch a hand-off event to the provider's webhook processor."""
    if handoff["provider"] == "github":
        return await _run_github_handoff(handoff)
    return await _run_gitlab_handoff(handoff)


async def _run_github_handoff(handoff: dict) -> dict:
    # Imported here so the helper stays importable from the server modules
    # (which are imported first) and without the optional provider SDKs.
    from pr_agent.servers import github_app

    body = handoff["body"]
    headers = handoff.get("headers") or {}
    get_logger().info("Processing a handed-off GitHub webhook",
                      artifact={"delivery": headers.get("x-github-delivery")})
    with request_cycle_context({}):
        # The request-scoped state the /api/v1/github_webhooks route sets up
        # before its background task, replicated for the second invocation.
        context["installation_id"] = body.get("installation", {}).get("id")
        context["settings"] = copy.deepcopy(global_settings)
        context["git_provider"] = {}
        await github_app.handle_request(
            body,
            event=headers.get("x-github-event"),
            delivery_id=headers.get("x-github-delivery"),
        )
    return {}


async def _run_gitlab_handoff(handoff: dict) -> dict:
    # Imported here: github-only deployments do not install python-gitlab.
    from pr_agent.servers import gitlab_webhook

    body = handoff["body"]
    headers = handoff.get("headers") or {}
    get_logger().info("Processing a handed-off GitLab webhook",
                      artifact={"delivery": headers.get("x-gitlab-event-uuid")})
    with request_cycle_context({}):
        # Only IAM-authorized direct invocations carry this envelope. The public
        # webhook already authenticated it; restore credentials by reference.
        context["settings"] = copy.deepcopy(global_settings)
        log_context = {"server_type": "gitlab_app"}
        await gitlab_webhook.restore_gitlab_handoff_auth(handoff.get("gitlab_auth"), log_context)
        await gitlab_webhook.process_gitlab_event(body, log_context)
    return {}
