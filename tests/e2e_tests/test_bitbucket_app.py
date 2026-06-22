import hashlib
import os
import re
import time
from datetime import datetime

import jwt
import requests
from atlassian.bitbucket import Cloud
from requests.auth import HTTPBasicAuth

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger, setup_logger
from tests.e2e_tests.e2e_utils import (
    FILE_PATH,
    IMPROVE_START_WITH_REGEX_PATTERN,
    NEW_FILE_CONTENT,
    NUM_MINUTES,
    PR_HEADER_START_WITH,
    REVIEW_START_WITH,
)

log_level = os.environ.get("LOG_LEVEL", "INFO")
setup_logger(log_level)
logger = get_logger()

def test_e2e_run_bitbucket_app():
    repo_slug = 'pr-agent-tests'
    project_key = 'codiumai'
    base_branch = "main"  # or any base branch you want
    new_branch = f"bitbucket_app_e2e_test-{datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}"
    get_settings().config.git_provider = "bitbucket"

    try:
        # Add username and password for authentication
        username = get_settings().get("BITBUCKET.USERNAME", None)
        password = get_settings().get("BITBUCKET.PASSWORD", None)
        s = requests.Session()
        s.auth = (username, password)  # Use HTTP Basic Auth
        bitbucket_client = Cloud(session=s)
        repo = bitbucket_client.workspaces.get(workspace=project_key).repositories.get(repo_slug)

        # Create a new branch from the base branch
        logger.info(f"Creating a new branch {new_branch} from {base_branch}")
        source_branch = repo.branches.get(base_branch)
        target_repo = repo.branches.create(new_branch,source_branch.hash)

        # Update the file content
        url = f"https://api.bitbucket.org/2.0/repositories/{project_key}/{repo_slug}/src"
        files={FILE_PATH: NEW_FILE_CONTENT}
        data={
            "message": "update cli_pip.py",
            "branch": new_branch,
        }
        requests.request("POST", url, auth=HTTPBasicAuth(username, password), data=data, files=files)


        # Create a pull request
        logger.info(f"Creating a pull request from {new_branch} to {base_branch}")
        pr = repo.pullrequests.create(
            title=f'{new_branch}',
            description="update cli_pip.py",
            source_branch=new_branch,
            destination_branch=base_branch
        )

        # check every 1 minute, for 5 minutes if the PR has all the tool results
        for i in range(NUM_MINUTES):
            logger.info(f"Waiting for the PR to get all the tool results...")
            time.sleep(60)
            comments = list(pr.comments())
            comments_raw = [c.raw for c in comments]
            if len(comments) >= 5: # header, 3 suggestions, 1 review
                valid_review = False
                for comment_raw in comments_raw:
                    if comment_raw.startswith('## PR Reviewer Guide 🔍'):
                        valid_review = True
                        break
                if valid_review:
                    break
                else:
                    logger.error(f"REVIEW feedback is invalid")
                    raise Exception("REVIEW feedback is invalid")
            else:
                logger.info(f"Waiting for the PR to get all the tool results. {i + 1} minute(s) passed")
        else:
            assert False, f"After {NUM_MINUTES} minutes, the PR did not get all the tool results"

        # cleanup - delete the branch
        pr.decline()
        repo.branches.delete(new_branch)

        # If we reach here, the test is successful
        logger.info(f"Succeeded in running e2e test for Bitbucket app on the PR")
    except Exception as e:
        logger.error(f"Failed to run e2e test for Bitbucket app: {e}")
        # delete the branch
        pr.decline()
        repo.branches.delete(new_branch)
        assert False


if __name__ == '__main__':
    test_e2e_run_bitbucket_app()
