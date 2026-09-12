from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pr_agent import cli


def test_run_injects_the_artifact_context_before_handling_the_request():
    """A pipeline that runs the CLI gets the same [artifacts] injection as the GitHub Action."""
    order = []
    fake_settings = SimpleNamespace(litellm={}, set=MagicMock())

    async def fake_handle_request(*_args, **_kwargs):
        order.append("handle_request")
        return True

    with patch("pr_agent.cli.get_settings", return_value=fake_settings), \
         patch("pr_agent.cli.inject_artifact_context", side_effect=lambda: order.append("inject")), \
         patch("pr_agent.cli.PRAgent", return_value=SimpleNamespace(handle_request=fake_handle_request)):
        cli.run(inargs=["--pr_url=https://github.com/a/b/pull/1", "review"])

    assert order == ["inject", "handle_request"]
