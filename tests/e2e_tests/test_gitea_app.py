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

        try:
            response = requests.get(
                f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/contents/{FILE_PATH}?ref={new_branch}",
                headers=headers
            )
            response.raise_for_status()
            existing_file = response.json()
            file_sha = existing_file.get('sha')

            file_data = {
                'message': 'Update cli_pip.py',
                'content': file_content_encoded,
                'sha': file_sha,
                'branch': new_branch
            }
        except:
            file_data = {
                'message': 'Add cli_pip.py',
                'content': file_content_encoded,
                'branch': new_branch
            }

        response = requests.put(
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

        for i in range(NUM_MINUTES):
            logger.info("Waiting for the PR to get all the tool results...")
            time.sleep(60)

            response = requests.get(
                f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/issues/{pr_number}/comments",
                headers=headers
            )
            response.raise_for_status()
            comments = response.json()

            if len(comments) >= 5:
                valid_review = False
                for comment in comments:
                    if comment['body'].startswith('## PR Reviewer Guide 🔍'):
                        valid_review = True
                        break
                if valid_review:
                    break
                else:
                    logger.error("REVIEW feedback is invalid")
                    raise Exception("REVIEW feedback is invalid")
            else:
                logger.info(f"Waiting for the PR to get all the tool results. {i + 1} minute(s) passed")
        else:
            raise AssertionError(f"After {NUM_MINUTES} minutes, the PR did not get all the tool results")

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
