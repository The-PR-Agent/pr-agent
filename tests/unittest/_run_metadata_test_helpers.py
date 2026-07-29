import pytest


@pytest.fixture(autouse=True)
def isolate_run_metadata():
    """Start each test from a clean run-metadata ContextVar and restore it."""
    from pr_agent.algo import run_metadata

    token = run_metadata._run_metadata.set(None)
    yield
    run_metadata._run_metadata.reset(token)
