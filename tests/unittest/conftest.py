import os

import pytest

# Match CI (#3475): use the LiteLLM cost map bundled with the version pinned in uv.lock rather than the copy
# LiteLLM fetches at import time, so local runs do not fail on third-party map changes (#3473, #3583).
# Respect an explicit LITELLM_LOCAL_MODEL_COST_MAP from the environment; set it to False to use the live map.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")


@pytest.fixture(autouse=True)
def isolate_run_details():
    """Start each test from a clean run-details ContextVar and restore it.

    The collector lives in a module-level ContextVar, so a test that leaves
    details behind would otherwise be visible to whichever test runs next.
    """
    from pr_agent.algo import run_details

    token = run_details._run_details.set(None)
    yield
    run_details._run_details.reset(token)
