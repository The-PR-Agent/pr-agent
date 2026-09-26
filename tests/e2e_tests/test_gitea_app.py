import json
import os
import time
from threading import Event, Lock, Thread
from time import monotonic
from traceback import TracebackException
from uuid import uuid4

import requests
from requests.exceptions import (
    ChunkedEncodingError,
    ConnectionError,
    ContentDecodingError,
    HTTPError,
    Timeout,
)

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger, setup_logger
from tests.e2e_tests.e2e_utils import (
    FILE_PATH,
    NEW_FILE_CONTENT,
    NUM_MINUTES,
)

_RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS = 30
_UNCERTAIN_BRANCH_CLEANUP_WINDOW_SECONDS = _RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
_UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS = 5
_UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS = 5
_UNCERTAIN_BRANCH_CLEANUP_FINAL_REQUEST_BUDGET_SECONDS = 1
_MAX_GITEA_JSON_RESPONSE_BYTES = 4 * 1024 * 1024


class _TransientGiteaResponseError(ValueError):
    """Report a successful Gitea response whose JSON shape is unusable."""


def _log_exception_without_locals(logger, message, exception_info):
    """Log a traceback without letting Loguru render frame-local values."""
    traceback_text = "".join(
        TracebackException(*exception_info, capture_locals=False).format()
    ).rstrip()
    logger.error(f"{message}\n{traceback_text}" if traceback_text else message)


def _pull_request_number(payload):
    if not isinstance(payload, dict):
        raise _TransientGiteaResponseError("Gitea pull-request response is not an object")
    number = payload.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        raise _TransientGiteaResponseError("Gitea pull-request response has an invalid number")
    return number


def _validated_pull_request_page(payload):
    if not isinstance(payload, list):
        raise _TransientGiteaResponseError("Gitea pull-request list response is not an array")

    for pull_request in payload:
        _pull_request_number(pull_request)
        base = pull_request.get("base")
        head = pull_request.get("head")
        if not isinstance(base, dict) or not isinstance(head, dict):
            raise _TransientGiteaResponseError("Gitea pull-request list item has invalid refs")
        if not isinstance(base.get("ref"), str) or not isinstance(head.get("ref"), str):
            raise _TransientGiteaResponseError("Gitea pull-request list item has invalid refs")
    return payload


def _is_retryable_branch_cleanup_error(cleanup_error, branch_creation_uncertain):
    if isinstance(cleanup_error, (Timeout, ConnectionError)):
        return True
    if isinstance(cleanup_error, HTTPError) and cleanup_error.response is not None:
        status_code = cleanup_error.response.status_code
        if status_code == 404:
            return branch_creation_uncertain
        return status_code in {408, 429} or status_code >= 500
    return False


def _is_ambiguous_branch_delete_error(cleanup_error):
    if isinstance(cleanup_error, (Timeout, ConnectionError)):
        return True
    if isinstance(cleanup_error, HTTPError) and cleanup_error.response is not None:
        status_code = cleanup_error.response.status_code
        return status_code == 408 or status_code >= 500
    return False


def _delete_branch_with_retry(
    gitea_url,
    owner,
    repo_name,
    new_branch,
    headers,
    logger,
    branch_creation_uncertain,
    suppress_errors=True,
    delete_outcome_state=None,
):
    if delete_outcome_state is None:
        delete_outcome_state = {"uncertain": False}

    cleanup_deadline = monotonic() + _UNCERTAIN_BRANCH_CLEANUP_WINDOW_SECONDS
    last_cleanup_error = None
    last_cleanup_exception = None

    while True:
        remaining_cleanup_time = cleanup_deadline - monotonic()
        if remaining_cleanup_time <= 0:
            if last_cleanup_error is None:
                last_cleanup_error = Timeout("Timed out while deleting Gitea branch")
                last_cleanup_exception = (
                    type(last_cleanup_error),
                    last_cleanup_error,
                    last_cleanup_error.__traceback__,
                )
            if suppress_errors:
                _log_exception_without_locals(
                    logger,
                    f"Failed to clean up after test: {last_cleanup_error}",
                    last_cleanup_exception,
                )
                return
            raise last_cleanup_error.with_traceback(last_cleanup_exception[2])

        request_timeout = min(_UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS, remaining_cleanup_time)
        phase_timeout = request_timeout / 2
        try:

            def delete_branch():
                response = requests.delete(
                    f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/branches/{new_branch}",
                    headers=headers,
                    timeout=(phase_timeout, phase_timeout),
                    stream=True,
                )
                try:
                    response.raise_for_status()
                finally:
                    response.close()

            _run_with_deadline(
                delete_branch,
                cleanup_deadline,
                "Timed out while deleting Gitea branch",
            )
            return
        except Exception as cleanup_error:
            if (
                not branch_creation_uncertain
                and delete_outcome_state["uncertain"]
                and isinstance(cleanup_error, HTTPError)
                and cleanup_error.response is not None
                and cleanup_error.response.status_code == 404
            ):
                return

            delete_outcome_state["uncertain"] = (
                delete_outcome_state["uncertain"] or _is_ambiguous_branch_delete_error(cleanup_error)
            )
            last_cleanup_error = cleanup_error
            last_cleanup_exception = (type(cleanup_error), cleanup_error, cleanup_error.__traceback__)
            remaining_cleanup_time = cleanup_deadline - monotonic()
            should_retry = (
                _is_retryable_branch_cleanup_error(cleanup_error, branch_creation_uncertain)
                and remaining_cleanup_time > _UNCERTAIN_BRANCH_CLEANUP_FINAL_REQUEST_BUDGET_SECONDS
            )
            if not should_retry:
                if suppress_errors:
                    _log_exception_without_locals(
                        logger,
                        f"Failed to clean up after test: {cleanup_error}",
                        (type(cleanup_error), cleanup_error, cleanup_error.__traceback__),
                    )
                    return
                raise

            sleep_seconds = min(
                _UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS,
                max(
                    0.0,
                    remaining_cleanup_time - _UNCERTAIN_BRANCH_CLEANUP_FINAL_REQUEST_BUDGET_SECONDS,
                ),
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)


def _is_retryable_pull_request_lookup_error(lookup_error):
    if isinstance(
        lookup_error,
        (
            Timeout,
            ConnectionError,
            ChunkedEncodingError,
            ContentDecodingError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            _TransientGiteaResponseError,
        ),
    ):
        return True
    if isinstance(lookup_error, HTTPError) and lookup_error.response is not None:
        status_code = lookup_error.response.status_code
        return status_code in {404, 408, 429} or status_code >= 500
    return False


def _is_retryable_pull_request_cleanup_error(cleanup_error):
    if isinstance(cleanup_error, (Timeout, ConnectionError)):
        return True
    if isinstance(cleanup_error, HTTPError) and cleanup_error.response is not None:
        status_code = cleanup_error.response.status_code
        return status_code in {404, 408, 429} or status_code >= 500
    return False


def _run_with_deadline(operation, deadline, timeout_message):
    """Bound caller wait time without claiming to cancel blocking worker I/O.

    Expect the daemon worker to finish after this helper raises Timeout. Keep
    resource-creation requests synchronous; use this helper only for response
    consumption and idempotent cleanup/recovery work.
    """
    remaining_time = deadline - monotonic()
    if remaining_time <= 0:
        raise Timeout(timeout_message)

    result = {}
    failure = {}
    done = Event()

    def run_operation():
        try:
            result["value"] = operation()
        except Exception as error:
            failure["error"] = error
        finally:
            done.set()

    worker = Thread(target=run_operation, daemon=True)
    worker.start()
    if not done.wait(remaining_time):
        raise Timeout(timeout_message)
    if monotonic() >= deadline:
        raise Timeout(timeout_message)
    if "error" in failure:
        raise failure["error"]
    return result.get("value")


def _load_json_response(response, deadline=None, timeout_message=None):
    body = bytearray()
    for chunk in response.iter_content(chunk_size=8192):
        if deadline is not None and monotonic() >= deadline:
            raise Timeout(timeout_message or "Timed out while reading Gitea response")
        if chunk:
            body.extend(chunk)
            if len(body) > _MAX_GITEA_JSON_RESPONSE_BYTES:
                raise _TransientGiteaResponseError("Gitea JSON response exceeds the maximum allowed size")
    return json.loads(body)


def _read_json_response_with_deadline(response, deadline, timeout_message):
    ownership_lock = Lock()
    ownership = {"reader": False, "caller": False}

    def read_response():
        with ownership_lock:
            if ownership["caller"]:
                return None
            ownership["reader"] = True
        try:
            return _load_json_response(response, deadline, timeout_message)
        finally:
            response.close()

    try:
        return _run_with_deadline(read_response, deadline, timeout_message)
    except Exception:
        caller_should_close = False
        with ownership_lock:
            if not ownership["reader"]:
                ownership["caller"] = True
                caller_should_close = True
        if caller_should_close:
            response.close()
        raise


def _log_last_pull_lookup_error(logger, lookup_error, exception_info):
    if lookup_error is None:
        lookup_error = Timeout("Timed out while resolving Gitea pull request")
        exception_info = (type(lookup_error), lookup_error, lookup_error.__traceback__)
    _log_exception_without_locals(
        logger,
        f"Failed to resolve uncertain pull request during cleanup: {lookup_error}",
        exception_info,
    )


def _resolve_uncertain_pull_request(
    gitea_url,
    owner,
    repo_name,
    base_branch,
    new_branch,
    headers,
    logger,
    lookup_deadline=None,
):
    """Resolve a possibly created pull request within one bounded lookup window."""
    if lookup_deadline is None:
        lookup_deadline = monotonic() + _RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
    last_lookup_error = None
    last_lookup_exception = None

    while True:
        page = 1
        try:
            while True:
                remaining_lookup_time = lookup_deadline - monotonic()
                if remaining_lookup_time <= 0:
                    _log_last_pull_lookup_error(logger, last_lookup_error, last_lookup_exception)
                    return None

                request_timeout = min(_UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS, remaining_lookup_time)
                phase_timeout = request_timeout / 2

                def list_pull_requests(page=page):
                    response = requests.get(
                        f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/pulls",
                        headers=headers,
                        params={"state": "open", "base_branch": base_branch, "page": page, "limit": 50},
                        timeout=(phase_timeout, phase_timeout),
                        stream=True,
                    )
                    try:
                        response.raise_for_status()
                        return _validated_pull_request_page(
                            _load_json_response(
                                response,
                                lookup_deadline,
                                "Timed out while resolving Gitea pull request",
                            )
                        )
                    finally:
                        response.close()

                pull_requests = _run_with_deadline(
                    list_pull_requests,
                    lookup_deadline,
                    "Timed out while resolving Gitea pull request",
                )

                for pull_request in pull_requests:
                    if (
                        pull_request.get("base", {}).get("ref") == base_branch
                        and pull_request.get("head", {}).get("ref") == new_branch
                    ):
                        return _pull_request_number(pull_request)

                if not pull_requests:
                    break
                page += 1
        except Exception as lookup_error:
            if not _is_retryable_pull_request_lookup_error(lookup_error):
                _log_exception_without_locals(
                    logger,
                    f"Failed to resolve uncertain pull request during cleanup: {lookup_error}",
                    (type(lookup_error), lookup_error, lookup_error.__traceback__),
                )
                return None
            last_lookup_error = lookup_error
            last_lookup_exception = (type(lookup_error), lookup_error, lookup_error.__traceback__)

        remaining_lookup_time = lookup_deadline - monotonic()
        if remaining_lookup_time <= _UNCERTAIN_BRANCH_CLEANUP_FINAL_REQUEST_BUDGET_SECONDS:
            _log_last_pull_lookup_error(logger, last_lookup_error, last_lookup_exception)
            return None

        sleep_seconds = min(
            _UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS,
            remaining_lookup_time - _UNCERTAIN_BRANCH_CLEANUP_FINAL_REQUEST_BUDGET_SECONDS,
        )
        time.sleep(sleep_seconds)


def _close_pull_request_with_retry(
    gitea_url,
    owner,
    repo_name,
    pr_number,
    headers,
    logger,
    suppress_errors=True,
):
    """Close a pull request within one bounded cleanup window."""
    cleanup_deadline = monotonic() + _RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
    last_cleanup_error = None
    last_cleanup_exception = None

    while True:
        remaining_cleanup_time = cleanup_deadline - monotonic()
        if remaining_cleanup_time <= 0:
            if last_cleanup_error is None:
                last_cleanup_error = Timeout("Timed out while closing Gitea pull request")
                last_cleanup_exception = (
                    type(last_cleanup_error),
                    last_cleanup_error,
                    last_cleanup_error.__traceback__,
                )
            if suppress_errors:
                _log_exception_without_locals(
                    logger,
                    f"Failed to clean up after test: {last_cleanup_error}",
                    last_cleanup_exception,
                )
                return
            raise last_cleanup_error.with_traceback(last_cleanup_exception[2])

        request_timeout = min(_UNCERTAIN_BRANCH_CLEANUP_REQUEST_TIMEOUT_SECONDS, remaining_cleanup_time)
        phase_timeout = request_timeout / 2
        try:

            def close_pull_request():
                response = requests.patch(
                    f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/pulls/{pr_number}",
                    headers=headers,
                    json={"state": "closed"},
                    timeout=(phase_timeout, phase_timeout),
                    stream=True,
                )
                try:
                    response.raise_for_status()
                finally:
                    response.close()

            _run_with_deadline(
                close_pull_request,
                cleanup_deadline,
                "Timed out while closing Gitea pull request",
            )
            return
        except Exception as cleanup_error:
            last_cleanup_error = cleanup_error
            last_cleanup_exception = (type(cleanup_error), cleanup_error, cleanup_error.__traceback__)
            remaining_cleanup_time = cleanup_deadline - monotonic()
            should_retry = (
                _is_retryable_pull_request_cleanup_error(cleanup_error)
                and remaining_cleanup_time > _UNCERTAIN_BRANCH_CLEANUP_FINAL_REQUEST_BUDGET_SECONDS
            )
            if not should_retry:
                if suppress_errors:
                    _log_exception_without_locals(
                        logger,
                        f"Failed to clean up after test: {cleanup_error}",
                        (type(cleanup_error), cleanup_error, cleanup_error.__traceback__),
                    )
                    return
                raise

            sleep_seconds = min(
                _UNCERTAIN_BRANCH_CLEANUP_RETRY_DELAY_SECONDS,
                max(
                    0.0,
                    remaining_cleanup_time - _UNCERTAIN_BRANCH_CLEANUP_FINAL_REQUEST_BUDGET_SECONDS,
                ),
            )
            if sleep_seconds > 0:
                time.sleep(sleep_seconds)


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
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    setup_logger(log_level)
    logger = get_logger()

    repo_name = 'pr-agent-tests'
    owner = 'codiumai'
    base_branch = "main"
    new_branch = f"gitea_app_e2e_test-{uuid4().hex}"
    get_settings().config.git_provider = "gitea"

    headers = None
    pr_number = None
    gitea_url = None
    branch_cleanup_needed = False
    branch_creation_uncertain = False
    branch_delete_outcome_state = {"uncertain": False}
    pending_branch_cleanup_exception = None
    pending_pr_cleanup_exception = None
    test_failure_active = False
    pr_creation_uncertain = False

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
        # Account for a request that creates the branch but loses its response.
        branch_cleanup_needed = True
        branch_creation_uncertain = True
        try:
            phase_timeout = _RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS / 2
            response = requests.post(
                f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/branches",
                headers=headers,
                json=branch_data,
                timeout=(phase_timeout, phase_timeout),
                stream=True,
            )
            try:
                response.raise_for_status()
                branch_creation_uncertain = False
            finally:
                response.close()
        except HTTPError as creation_error:
            if (
                creation_error.response is not None
                and 400 <= creation_error.response.status_code < 500
                and creation_error.response.status_code not in {408, 429}
            ):
                # Skip cleanup when a definitive client rejection confirms this run did not create the branch.
                branch_cleanup_needed = False
                branch_creation_uncertain = False
            raise

        logger.info(f"Updating file {FILE_PATH} in branch {new_branch}")

        import base64
        file_content_encoded = base64.b64encode(NEW_FILE_CONTENT.encode()).decode()

        response = requests.get(
            f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/contents/{FILE_PATH}?ref={new_branch}",
            headers=headers,
            timeout=_RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS,
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
            json=file_data,
            timeout=_RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()

        logger.info(f"Creating a pull request from {new_branch} to {base_branch}")
        pr_data = {
            'title': f'Test PR from {new_branch}',
            'body': 'update cli_pip.py',
            'head': new_branch,
            'base': base_branch
        }
        pr_creation_uncertain = True
        pr_creation_deadline = monotonic() + _RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS
        try:
            phase_timeout = _RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS / 2
            response = requests.post(
                f"{gitea_url}/api/v1/repos/{owner}/{repo_name}/pulls",
                headers=headers,
                json=pr_data,
                timeout=(phase_timeout, phase_timeout),
                stream=True,
            )
            try:
                response.raise_for_status()
            except Exception:
                response.close()
                raise

            created_pull_request = _read_json_response_with_deadline(
                response,
                pr_creation_deadline,
                "Timed out while reading Gitea pull-request creation response",
            )
            pr_number = _pull_request_number(created_pull_request)
            pr_creation_uncertain = False
        except HTTPError as creation_error:
            if (
                creation_error.response is not None
                and 400 <= creation_error.response.status_code < 500
                and creation_error.response.status_code not in {408, 429}
            ):
                pr_creation_uncertain = False
            raise

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

        try:
            _close_pull_request_with_retry(
                gitea_url,
                owner,
                repo_name,
                pr_number,
                headers,
                logger,
                suppress_errors=False,
            )
        except Exception as cleanup_error:
            if not _is_retryable_pull_request_cleanup_error(cleanup_error):
                raise
            pending_pr_cleanup_exception = (
                type(cleanup_error),
                cleanup_error,
                cleanup_error.__traceback__,
            )
        else:
            pr_number = None

            try:
                _delete_branch_with_retry(
                    gitea_url,
                    owner,
                    repo_name,
                    new_branch,
                    headers,
                    logger,
                    branch_creation_uncertain=False,
                    suppress_errors=False,
                    delete_outcome_state=branch_delete_outcome_state,
                )
            except Exception as cleanup_error:
                if not _is_retryable_branch_cleanup_error(
                    cleanup_error,
                    branch_creation_uncertain=False,
                ):
                    raise
                pending_branch_cleanup_exception = (
                    type(cleanup_error),
                    cleanup_error,
                    cleanup_error.__traceback__,
                )
            else:
                branch_cleanup_needed = False
    except Exception as e:
        test_failure_active = True
        logger.error(f"Failed to run e2e test for Gitea app: {e}")
        raise
    finally:
        if headers is not None and gitea_url is not None:
            if pr_number is None and pr_creation_uncertain:
                try:
                    pr_number = _resolve_uncertain_pull_request(
                        gitea_url,
                        owner,
                        repo_name,
                        base_branch,
                        new_branch,
                        headers,
                        logger,
                        lookup_deadline=monotonic() + _RESOURCE_LIFECYCLE_REQUEST_TIMEOUT_SECONDS,
                    )
                except Exception as cleanup_error:
                    _log_exception_without_locals(
                        logger,
                        f"Failed to clean up after test: {cleanup_error}",
                        (type(cleanup_error), cleanup_error, cleanup_error.__traceback__),
                    )

            if pr_number is not None:
                try:
                    _close_pull_request_with_retry(
                        gitea_url,
                        owner,
                        repo_name,
                        pr_number,
                        headers,
                        logger,
                        suppress_errors=pending_pr_cleanup_exception is None,
                    )
                    if pending_pr_cleanup_exception is not None:
                        pending_pr_cleanup_exception = None
                except Exception as cleanup_error:
                    _log_exception_without_locals(
                        logger,
                        f"Failed to clean up after test: {cleanup_error}",
                        (type(cleanup_error), cleanup_error, cleanup_error.__traceback__),
                    )

            if branch_cleanup_needed:
                preserve_prior_failure = test_failure_active or pending_pr_cleanup_exception is not None
                suppress_branch_cleanup_errors = (
                    pending_branch_cleanup_exception is None and preserve_prior_failure
                )
                try:
                    _delete_branch_with_retry(
                        gitea_url,
                        owner,
                        repo_name,
                        new_branch,
                        headers,
                        logger,
                        branch_creation_uncertain,
                        suppress_errors=suppress_branch_cleanup_errors,
                        delete_outcome_state=branch_delete_outcome_state,
                    )
                    if pending_branch_cleanup_exception is not None:
                        pending_branch_cleanup_exception = None
                except Exception as cleanup_error:
                    _log_exception_without_locals(
                        logger,
                        f"Failed to clean up after test: {cleanup_error}",
                        (type(cleanup_error), cleanup_error, cleanup_error.__traceback__),
                    )
                    if pending_branch_cleanup_exception is None and not preserve_prior_failure:
                        pending_branch_cleanup_exception = (
                            type(cleanup_error),
                            cleanup_error,
                            cleanup_error.__traceback__,
                        )

    if pending_pr_cleanup_exception is not None:
        cleanup_error = pending_pr_cleanup_exception[1]
        logger.error(f"Failed to run e2e test for Gitea app: {cleanup_error}")
        raise cleanup_error.with_traceback(pending_pr_cleanup_exception[2])

    if pending_branch_cleanup_exception is not None:
        cleanup_error = pending_branch_cleanup_exception[1]
        logger.error(f"Failed to run e2e test for Gitea app: {cleanup_error}")
        raise cleanup_error.with_traceback(pending_branch_cleanup_exception[2])

    logger.info("Succeeded in running e2e test for Gitea app on the PR")


if __name__ == '__main__':
    test_e2e_run_gitea_app()
