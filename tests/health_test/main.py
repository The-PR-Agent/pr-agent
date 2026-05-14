import argparse
import asyncio
import copy
import os
from pathlib import Path

from starlette_context import context, request_cycle_context

from pr_agent.agent.pr_agent import PRAgent, commands
from pr_agent.cli import run_command
from pr_agent.config_loader import get_settings, global_settings
from pr_agent.log import get_logger, setup_logger
from tests.e2e_tests import e2e_utils

log_level = os.environ.get("LOG_LEVEL", "INFO")
setup_logger(log_level)


async def run_async():
    pr_url = os.getenv('TEST_PR_URL', 'https://github.com/Codium-ai/pr-agent/pull/1385')

    get_settings().set("config.git_provider", "github")
    get_settings().set("config.publish_output", False)
    get_settings().set("config.fallback_models", [])

    agent = PRAgent()
    try:
        # Run the 'describe' command
        get_logger().info(f"\nSanity check for the 'describe' command...")
        original_settings = copy.deepcopy(get_settings())
        await agent.handle_request(pr_url, ['describe'])
        pr_header_body = dict(get_settings().data)['artifact']
        assert pr_header_body.startswith('###') and 'PR Type' in pr_header_body and 'Description' in pr_header_body
        context['settings'] = copy.deepcopy(original_settings) # Restore settings state after each test to prevent test interference
        get_logger().info("PR description generated successfully\n")

        # Run the 'review' command
        get_logger().info(f"\nSanity check for the 'review' command...")
        original_settings = copy.deepcopy(get_settings())
        await agent.handle_request(pr_url, ['review'])
        pr_review_body = dict(get_settings().data)['artifact']
        assert pr_review_body.startswith('##') and 'PR Reviewer Guide' in pr_review_body
        context['settings'] = copy.deepcopy(original_settings)  # Restore settings state after each test to prevent test interference
        get_logger().info("PR review generated successfully\n")

        # Run the 'improve' command
        get_logger().info(f"\nSanity check for the 'improve' command...")
        original_settings = copy.deepcopy(get_settings())
        await agent.handle_request(pr_url, ['improve'])
        pr_improve_body = dict(get_settings().data)['artifact']
        assert pr_improve_body.startswith('##') and 'PR Code Suggestions' in pr_improve_body
        context['settings'] = copy.deepcopy(original_settings)  # Restore settings state after each test to prevent test interference
        get_logger().info("PR improvements generated successfully\n")

        get_logger().info(f"\n\n========\nHealth test passed successfully\n========")

    except Exception as e:
        get_logger().exception(f"\n\n========\nHealth test failed\n========")
        raise e


def run():
    with request_cycle_context({}):
        context['settings'] = copy.deepcopy(global_settings)
        asyncio.run(run_async())


if __name__ == '__main__':
    run()
