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
    msg: str


@router.post("/api/v1/gerrit/{action}")
async def handle_gerrit_request(action: Action, item: Item):
    get_logger().debug("Received a Gerrit request")
    context["settings"] = copy.deepcopy(global_settings)

    if action == Action.ask:
        if not item.msg:
            return HTTPException(
                status_code=400,
                detail="msg is required for ask command"
            )
    await PRAgent().handle_request(
        f"{item.project}:{item.refspec}",
        f"/{item.msg.strip()}"
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
    get_settings().set("CONFIG.GIT_PROVIDER", "gerrit")
    # to prevent adding help messages with the output
    get_settings().set("CONFIG.CLI_MODE", True)
    middleware = [Middleware(RawContextMiddleware)]
    app = FastAPI(middleware=middleware)
    app.include_router(router)

    uvicorn.run(app, host="0.0.0.0", port=3000)


if __name__ == '__main__':
    start()
