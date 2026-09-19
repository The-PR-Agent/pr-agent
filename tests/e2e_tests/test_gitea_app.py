import os
import time
from datetime import datetime

import requests

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger, setup_logger
from tests.e2e_tests.e2e_utils import (
    FILE_PATH,
    NEW_FILE_CONTENT,
    NUM_MINUTES,
)

log_level = os.environ.get("LOG_LEVEL", "INFO")
setup_logger(log_level)
logger = get_logger()

def _missing_gitea_tool_results(repo_api_url, pr_number, headers):
    """Check the description and the comments for the default tools' final output."""
    response = requests.get(
        f"{repo_api_url}/pulls/{pr_number}",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    description_lines = (response.json().get("body") or "").splitlines()
    missing = {"/describe", "/review", "/improve"}
    if all(header in description_lines for header in ("### **PR Type**", "### **Description**")):
        missing.remove("/describe")

    comment_markers = {
        "/review": {"<!-- pr-agent:review:full -->"},
        "/improve": {"<!-- pr-agent:improve:summary -->", "<!-- pr-agent:improve:no-suggestions -->"},
    }
    response = requests.get(
        f"{repo_api_url}/issues/{pr_number}/comments",
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    for comment in response.json():
        lines = set((comment.get("body") or "").splitlines())
        for command, markers in comment_markers.items():
            if lines.intersection(markers):
                missing.discard(command)
    return sorted(missing)


def test_e2e_run_gitea_app():
    repo_name = 'pr-agent-tests'
    owner = 'codiumai'
    base_branch = "main"
    new_branch = f"gitea_app_e2e_test-{datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}"
    get_settings().config.git_provider = "gitea"

    headers = None
    pr_number = None
    branch_created = False

    try:
        gitea_url = get_settings().get("GITEA.URL", None)
        gitea_token = get_settings().get("GITEA.TOKEN", None)

        if not gitea_url:
            logger.error("GITEA.URL is not set in the configuration")
            logger.info("Please set GITEA.URL in .env file or environment variables")
            raise AssertionError("GITEA.URL is not set in the configuration")

        if not gitea_token:
            logger.error("GITEA.TOKEN is not set in the configuration")
            logger.info("Please set GITEA.TOKEN in .env file or environment variables")
            raise AssertionError("GITEA.TOKEN is not set in the configuration")

        headers = {
            'Authorization': f'token {gitea_token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

        logger.info(f"Creating a new branch {new_branch} from {base_branch}")

        branch_data = {
            'new_branch_name': new_branch,
            'old_ref_name': base_branch
        }
        response = requests.post(
            f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/branches",
            headers=headers,
            json=branch_data
        )
        response.raise_for_status()
        branch_created = True

        logger.info(f"Updating file {FILE_PATH} in branch {new_branch}")

        import base64
        file_content_encoded = base64.b64encode(NEW_FILE_CONTENT.encode()).decode()

        response = requests.get(
            f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/contents/{FILE_PATH}?ref={new_branch}",
            headers=headers
        )
        file_data = {
            "message": "Update cli_pip.py",
            "content": file_content_encoded,
            "branch": new_branch
        }
        if response.status_code == 404:
            file_data["message"] = "Add cli_pip.py"
            write_file = requests.post
        else:
            response.raise_for_status()
            file_data["sha"] = response.json()["sha"]
            write_file = requests.put

        response = write_file(
            f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/contents/{FILE_PATH}",
            headers=headers,
            json=file_data
        )
        response.raise_for_status()

        logger.info(f"Creating a pull request from {new_branch} to {base_branch}")
        pr_data = {
            'title': f'Test PR from {new_branch}',
            'body': 'update cli_pip.py',
            'head': new_branch,
            'base': base_branch
        }
        response = requests.post(
            f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/pulls",
            headers=headers,
            json=pr_data
        )
        response.raise_for_status()
        pr = response.json()
        pr_number = pr['number']

        missing_tools = ["/describe", "/review", "/improve"]
        for i in range(NUM_MINUTES):
            logger.info("Waiting for the PR to get all the tool results...")
            time.sleep(60)

            missing_tools = _missing_gitea_tool_results(
                f"{gitea_url}/api/v1/repos/{owner}/{repo_name}", pr_number, headers
            )
            if not missing_tools:
                break
            logger.info(f"Still waiting for {', '.join(missing_tools)} after {i + 1} minute(s)")
        else:
            raise AssertionError(f"After {NUM_MINUTES} minutes, missing tool results: {', '.join(missing_tools)}")

        logger.info(f"Cleaning up: closing PR and deleting branch {new_branch}")

        close_data = {'state': 'closed'}
        response = requests.patch(
            f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/pulls/{pr_number}",
            headers=headers,
            json=close_data
        )
        response.raise_for_status()
        pr_number = None

        response = requests.delete(
            f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/branches/{new_branch}",
            headers=headers
        )
        response.raise_for_status()
        branch_created = False

        logger.info("Succeeded in running e2e test for Gitea app on the PR")
    except Exception as e:
        logger.error(f"Failed to run e2e test for Gitea app: {e}")
        raise
    finally:
        if headers is not None and gitea_url is not None:
            if pr_number is not None:
                try:
                    response = requests.patch(
                        f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/pulls/{pr_number}",
                        headers=headers,
                        json={'state': 'closed'}
                    )
                    response.raise_for_status()
                except Exception as cleanup_error:
                    logger.error(f"Failed to clean up after test: {cleanup_error}")

            if branch_created:
                try:
                    response = requests.delete(
                        f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/branches/{new_branch}",
                        headers=headers
                    )
                    response.raise_for_status()
                except Exception as cleanup_error:
                    logger.error(f"Failed to clean up after test: {cleanup_error}")

if __name__ == '__main__':
    test_e2e_run_gitea_app()
