import copy
from enum import Enum
from json import JSONDecodeError

import uvicorn
from fastapi import APIRouter, FastAPI, HTTPException
from pydantic import BaseModel
from starlette.middleware import Middleware
from starlette_context import context
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.git_providers.gerrit_provider import GerritProvider
from pr_agent.log import get_logger, setup_logger

setup_logger()
router = APIRouter()


class Action(str, Enum):
    review = "review"
    describe = "describe"
    ask = "ask"
    improve = "improve"
    reflect = "reflect"
    answer = "answer"


class Item(BaseModel):
    refspec: str
    project: str
    msg: str = ""


@router.post("/api/v1/gerrit/{action}")
async def handle_gerrit_request(action: Action, item: Item):
    get_logger().debug("Received a Gerrit request")
    context["settings"] = copy.deepcopy(global_settings)

    # For the "ask" action, the question must come from item.msg.
    # For all other actions, use the action path parameter as the command.
    if action == Action.ask:
        if not item.msg:
            raise HTTPException(
                status_code=400,
                detail="msg is required for ask command"
            )
        command = f"/{action.value} {item.msg.strip()}"
    else:
        command = f"/{action.value}"

    try:
        await PRAgent().handle_request(
            f"{item.project}:{item.refspec}",
            command
        )
    finally:
        # Clean up the cloned temp repo created by GerritProvider.
        # The provider is cached in the starlette context during
        # get_git_provider_with_context().
        #
        # We guard against two failure modes:
        #   1. The starlette context is inaccessible (e.g. middleware not
        #      active) — caught by the outer try/except.
        #   2. The provider was never stored in the context (e.g. an error
        #      occurred before get_git_provider_with_context ran) — the
        #      dict will simply be empty, and the GerritProvider.__del__
        #      safety net handles cleanup on garbage collection.
        try:
            git_providers = context.get("git_provider", {})
            if isinstance(git_providers, dict):
                for provider in git_providers.values():
                    if isinstance(provider, GerritProvider):
                        provider.cleanup()
        except (LookupError, RuntimeError):
            get_logger().debug(
                "Could not retrieve GerritProvider for cleanup; "
                "temp directory will be cleaned up by __del__"
            )


async def get_body(request):
    try:
        body = await request.json()
    except JSONDecodeError as e:
        get_logger().error("Error parsing request body", e)
        return {}
    return body


@router.get("/")
async def root():
    return {"status": "ok"}


def start():
    # to prevent adding help messages with the output
    get_settings().set("CONFIG.CLI_MODE", True)
    middleware = [Middleware(RawContextMiddleware)]
    app = FastAPI(middleware=middleware)
    app.include_router(router)

    uvicorn.run(app, host="0.0.0.0", port=3000)


if __name__ == '__main__':
    start()
