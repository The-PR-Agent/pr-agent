import os
import re
import time
from datetime import datetime

import gitlab

from pr_agent.config_loader import get_settings
from pr_agent.git_providers import get_git_provider
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

def test_e2e_run_github_app():
    # GitLab setup
    GITLAB_URL = "https://gitlab.com"
    GITLAB_TOKEN = get_settings().gitlab.PERSONAL_ACCESS_TOKEN
    gl = gitlab.Gitlab(GITLAB_URL, private_token=GITLAB_TOKEN)
    repo_url = 'pragent/pr-agent-tests'
    project = gl.projects.get(repo_url)

    base_branch = "main"  # or any base branch you want
    new_branch = f"github_app_e2e_test-{datetime.now().strftime('%Y-%m-%d-%H-%M-%S')}"

    try:
        # Create a new branch from the base branch
        logger.info(f"Creating a new branch {new_branch} from {base_branch}")
        project.branches.create({'branch': new_branch, 'ref': base_branch})

        # Get the file you want to edit
        file = project.files.get(file_path=FILE_PATH, ref=base_branch)
        # content = file.decode()

        # Update the file content
        logger.info(f"Updating the file {FILE_PATH}")
        commit_message = "update cli_pip.py"
        file.content = NEW_FILE_CONTENT
        file.save(branch=new_branch, commit_message=commit_message)

        # Create a merge request
        logger.info(f"Creating a merge request from {new_branch} to {base_branch}")
        mr = project.mergerequests.create({
            'source_branch': new_branch,
            'target_branch': base_branch,
            'title': new_branch,
            'description': "update cli_pip.py"
        })
        logger.info(f"Merge request created: {mr.web_url}")

        # check every 1 minute, for 5, minutes if the PR has all the tool results
        for i in range(NUM_MINUTES):
            logger.info(f"Waiting for the MR to get all the tool results...")
            time.sleep(60)
            logger.info(f"Checking the MR {mr.web_url} after {i + 1} minute(s)")
            mr = project.mergerequests.get(mr.iid)
            mr_header_body = mr.description
            comments = mr.notes.list()[::-1]
            # clean all system comments
            comments = [comment for comment in comments if comment.system is False]
            if len(comments) == 2: # "changed the description" is received as the first comment
                comments_body = [comment.body for comment in comments]
                if 'Work in progress' in comments_body[1]:
                    continue
                assert mr_header_body.startswith(PR_HEADER_START_WITH), "DESCRIBE feedback is invalid"
                assert comments_body[0].startswith(REVIEW_START_WITH), "REVIEW feedback is invalid"
                assert re.match(IMPROVE_START_WITH_REGEX_PATTERN, comments_body[1]), "IMPROVE feedback is invalid"
                break
            else:
                logger.info(f"Waiting for the MR to get all the tool results. {i + 1} minute(s) passed")
        else:
            assert False, f"After {NUM_MINUTES} minutes, the MR did not get all the tool results"

        # cleanup - delete the branch
        logger.info(f"Deleting the branch {new_branch}")
        project.branches.delete(new_branch)

        # If we reach here, the test is successful
        logger.info(f"Succeeded in running e2e test for GitLab app on the MR {mr.web_url}")
    except Exception as e:
        logger.error(f"Failed to run e2e test for GitHub app: {e}")
        logger.info(f"Deleting the branch {new_branch}")
        project.branches.delete(new_branch)
        assert False


if __name__ == '__main__':
    test_e2e_run_github_app()
