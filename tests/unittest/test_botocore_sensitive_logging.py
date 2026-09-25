import io
import logging
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
from botocore.credentials import RefreshableCredentials
from botocore.exceptions import CredentialRetrievalError

from pr_agent.log import _install_botocore_credential_filter

PROCESS_SECRET = "credential-process-stderr-secret-sentinel"
CONTAINER_ERROR = "container-metadata-error-sentinel"
OTHER_ERROR = "other-refresh-error-sentinel"


def _refreshing_credentials(error, period):
    seconds_until_expiry = 1 if period == "mandatory" else 45
    return RefreshableCredentials(
        access_key="testing",
        secret_key="testing",
        token="testing",
        expiry_time=datetime.now(UTC) + timedelta(seconds=seconds_until_expiry),
        refresh_using=MagicMock(side_effect=error),
        method="test",
        advisory_timeout=60,
        mandatory_timeout=30,
    )


def _refresh(credentials, error, period):
    if period == "mandatory":
        with pytest.raises(type(error)) as caught:
            credentials.get_frozen_credentials()
        assert caught.value is error
    else:
        frozen = credentials.get_frozen_credentials()
        assert frozen.access_key == "testing"


@pytest.fixture
def botocore_credential_log():
    logger = logging.getLogger("botocore.credentials")
    previous_filters = list(logger.filters)
    previous_handlers = list(logger.handlers)
    previous_level = logger.level
    previous_propagate = logger.propagate

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.filters.clear()
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    _install_botocore_credential_filter()

    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.filters[:] = previous_filters
        logger.handlers[:] = previous_handlers
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate


@pytest.mark.parametrize("period", ["advisory", "mandatory"])
def test_credential_process_refresh_warning_omits_exception_details(botocore_credential_log, period):
    error = CredentialRetrievalError(provider="custom-process", error_msg=PROCESS_SECRET)

    _refresh(_refreshing_credentials(error, period), error, period)

    output = botocore_credential_log.getvalue()
    assert f"Refreshing temporary credentials failed during {period} refresh period." in output
    assert PROCESS_SECRET not in output
    assert "Traceback (most recent call last)" not in output


@pytest.mark.parametrize("period", ["advisory", "mandatory"])
def test_non_process_credential_error_keeps_diagnostics(botocore_credential_log, period):
    error = CredentialRetrievalError(provider="container-role", error_msg=CONTAINER_ERROR)

    _refresh(_refreshing_credentials(error, period), error, period)

    output = botocore_credential_log.getvalue()
    assert CONTAINER_ERROR in output
    assert "Traceback (most recent call last)" in output


def test_embedded_pr_agent_import_redacts_without_setup_logger():
    script = """
import io
import logging

from botocore.exceptions import CredentialRetrievalError

from pr_agent.agent.pr_agent import PRAgent  # noqa: F401

stream = io.StringIO()
handler = logging.StreamHandler(stream)
logger = logging.getLogger("botocore.credentials")
logger.handlers.clear()
logger.propagate = False
logger.setLevel(logging.WARNING)
logger.addHandler(handler)

try:
    raise CredentialRetrievalError(
        provider="custom-process",
        error_msg="embedded-credential-process-secret-sentinel",
    )
except CredentialRetrievalError:
    logger.warning(
        "Refreshing temporary credentials failed during %s refresh period.",
        "mandatory",
        exc_info=True,
    )

print(stream.getvalue())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Refreshing temporary credentials failed during mandatory refresh period." in result.stdout
    assert "embedded-credential-process-secret-sentinel" not in result.stdout
    assert "Traceback (most recent call last)" not in result.stdout


def test_other_refresh_errors_keep_exception_details(botocore_credential_log):
    error = RuntimeError(OTHER_ERROR)

    _refresh(_refreshing_credentials(error, "mandatory"), error, "mandatory")

    output = botocore_credential_log.getvalue()
    assert OTHER_ERROR in output
    assert "Traceback (most recent call last)" in output
