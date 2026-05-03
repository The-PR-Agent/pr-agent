import asyncio
import json
import os
from typing import Union

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import get_logger
from pr_agent.servers.github_app import handle_line_comments
from pr_agent.tools.pr_code_suggestions import PRCodeSuggestions
from pr_agent.tools.pr_description import PRDescription
from pr_agent.tools.pr_reviewer import PRReviewer


def is_true(value: Union[str, bool]) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == 'true'
    return False


def get_setting_or_env(key: str, default: Union[str, bool] = None) -> Union[str, bool]:
    try:
        value = get_settings().get(key, default)
    except AttributeError:  # TBD still need to debug why this happens on GitHub Actions
        value = os.getenv(key, None) or os.getenv(key.upper(), None) or os.getenv(key.lower(), None) or default
    return value


async def run_action():
    # Get environment variables
    GITHUB_EVENT_NAME = os.environ.get('GITHUB_EVENT_NAME')
    GITHUB_EVENT_PATH = os.environ.get('GITHUB_EVENT_PATH')
    OPENAI_KEY = os.environ.get('OPENAI_KEY') or os.environ.get('OPENAI.KEY')
    OPENAI_ORG = os.environ.get('OPENAI_ORG') or os.environ.get('OPENAI.ORG')
    GITHUB_TOKEN = os.environ.get('GITHUB_TOKEN')
    # get_settings().set("CONFIG.PUBLISH_OUTPUT_PROGRESS", False)

    # Check if required environment variables are set
    if not GITHUB_EVENT_NAME:
        print("GITHUB_EVENT_NAME not set")
        return
    if not GITHUB_EVENT_PATH:
        print("GITHUB_EVENT_PATH not set")
        return
    if not GITHUB_TOKEN:
        print("GITHUB_TOKEN not set")
        return

    # Set the environment variables in the settings
    if OPENAI_KEY:
        get_settings().set("OPENAI.KEY", OPENAI_KEY)
    else:
        # Might not be set if the user is using models not from OpenAI
        print("OPENAI_KEY not set")
    if OPENAI_ORG:
        get_settings().set("OPENAI.ORG", OPENAI_ORG)
    get_settings().set("GITHUB.USER_TOKEN", GITHUB_TOKEN)
    get_settings().set("GITHUB.DEPLOYMENT_TYPE", "user")
    enable_output = get_setting_or_env("GITHUB_ACTION_CONFIG.ENABLE_OUTPUT", True)
    get_settings().set("GITHUB_ACTION_CONFIG.ENABLE_OUTPUT", enable_output)

    # Load the event payload
    try:
        with open(GITHUB_EVENT_PATH, 'r') as f:
            event_payload = json.load(f)
    except json.decoder.JSONDecodeError as e:
        print(f"Failed to parse JSON: {e}")
        return

    try:
        get_logger().info("Applying repo settings")
        pr_url = event_payload.get("pull_request", {}).get("html_url")
        if pr_url:
            apply_repo_settings(pr_url)
            get_logger().info(f"enable_custom_labels: {get_settings().config.enable_custom_labels}")
    except Exception as e:
        get_logger().info(f"github action: failed to apply repo settings: {e}")

    if get_settings().config.disable_auto_feedback:
        get_logger().info("Auto feedback is disabled, skipping GitHub Action automatic tools")
        return

    # Append the response language in the extra instructions
    try:
        response_language = get_settings().config.get('response_language', 'en-us')
        if response_language.lower() != 'en-us':
            get_logger().info(f'User has set the response language to: {response_language}')

            lang_instruction_text = f"Your response MUST be written in the language corresponding to locale code: '{response_language}'. This is crucial."
            separator_text = "\n======\n\nIn addition, "

            for key in get_settings():
                setting = get_settings().get(key)
                if str(type(setting)) == "<class 'dynaconf.utils.boxing.DynaBox'>":
                    if key.lower() in ['pr_description', 'pr_code_suggestions', 'pr_reviewer']:
                        if hasattr(setting, 'extra_instructions'):
                            extra_instructions = setting.extra_instructions

                            if lang_instruction_text not in str(extra_instructions):
                                updated_instructions = (
                                    str(extra_instructions) + separator_text + lang_instruction_text
                                    if extra_instructions else lang_instruction_text
                                )
                                setting.extra_instructions = updated_instructions
    except Exception as e:
        get_logger().info(f"github action: failed to apply language-specific instructions: {e}")
    # Handle pull request opened event
    if GITHUB_EVENT_NAME == "pull_request" or GITHUB_EVENT_NAME == "pull_request_target":
        action = event_payload.get("action")

        # Retrieve the list of actions from the configuration
        pr_actions = get_settings().get("GITHUB_ACTION_CONFIG.PR_ACTIONS", ["opened", "reopened", "ready_for_review", "review_requested"])

        if action in pr_actions:
            pr_url = event_payload.get("pull_request", {}).get("url")
            if pr_url:
                # legacy - supporting both GITHUB_ACTION and GITHUB_ACTION_CONFIG
                auto_review = get_setting_or_env("GITHUB_ACTION.AUTO_REVIEW", None)
                if auto_review is None:
                    auto_review = get_setting_or_env("GITHUB_ACTION_CONFIG.AUTO_REVIEW", None)
                auto_describe = get_setting_or_env("GITHUB_ACTION.AUTO_DESCRIBE", None)
                if auto_describe is None:
                    auto_describe = get_setting_or_env("GITHUB_ACTION_CONFIG.AUTO_DESCRIBE", None)
                auto_improve = get_setting_or_env("GITHUB_ACTION.AUTO_IMPROVE", None)
                if auto_improve is None:
                    auto_improve = get_setting_or_env("GITHUB_ACTION_CONFIG.AUTO_IMPROVE", None)

                # Set the configuration for auto actions
                get_settings().config.is_auto_command = True  # Set the flag to indicate that the command is auto
                get_settings().pr_description.final_update_message = False  # No final update message when auto_describe is enabled
                get_logger().info(f"Running auto actions: auto_describe={auto_describe}, auto_review={auto_review}, auto_improve={auto_improve}")

                # invoke by default all three tools
                if auto_describe is None or is_true(auto_describe):
                    await PRDescription(pr_url).run()
                if auto_review is None or is_true(auto_review):
                    await PRReviewer(pr_url).run()
                if auto_improve is None or is_true(auto_improve):
                    await PRCodeSuggestions(pr_url).run()
        else:
            get_logger().info(f"Skipping action: {action}")

    # Handle issue comment event
    elif GITHUB_EVENT_NAME == "issue_comment" or GITHUB_EVENT_NAME == "pull_request_review_comment":
        action = event_payload.get("action")
        if action in ["created", "edited"]:
            comment_body = event_payload.get("comment", {}).get("body")
            try:
                if GITHUB_EVENT_NAME == "pull_request_review_comment":
                    if '/ask' in comment_body:
                        comment_body = handle_line_comments(event_payload, comment_body)
            except Exception as e:
                get_logger().error(f"Failed to handle line comments: {e}")
                return
            if comment_body:
                is_pr = False
                disable_eyes = False
                # check if issue is pull request
                if event_payload.get("issue", {}).get("pull_request"):
                    url = event_payload.get("issue", {}).get("pull_request", {}).get("url")
                    is_pr = True
                elif event_payload.get("comment", {}).get("pull_request_url"):  # for 'pull_request_review_comment
                    url = event_payload.get("comment", {}).get("pull_request_url")
                    is_pr = True
                    disable_eyes = True
                else:
                    url = event_payload.get("issue", {}).get("url")

                if url:
                    body = comment_body.strip().lower()
                    comment_id = event_payload.get("comment", {}).get("id")
                    provider = get_git_provider()(pr_url=url)
                    if is_pr:
                        await PRAgent().handle_request(
                            url, body, notify=lambda: provider.add_eyes_reaction(
                                comment_id, disable_eyes=disable_eyes
                            )
                        )
                    else:
                        await PRAgent().handle_request(url, body)


if __name__ == '__main__':
    asyncio.run(run_action())
