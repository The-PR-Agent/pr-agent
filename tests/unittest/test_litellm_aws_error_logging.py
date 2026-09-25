"""Cover AWS credential and region discovery log regressions."""

from unittest.mock import PropertyMock

import pytest
from botocore.exceptions import ClientError, CredentialRetrievalError

import pr_agent.algo.ai_handlers.litellm_ai_handler as litellm_handler
from pr_agent.log import get_logger
from tests.unittest import test_litellm_imds as imds_tests

isolate_aws = imds_tests.isolate_aws
aws_session = imds_tests.aws_session


@pytest.fixture(params=[False, True], ids=["console", "json"])
def aws_log_messages(request):
    messages = []
    sink = get_logger().add(
        messages.append,
        filter=lambda record: record["name"] == litellm_handler.__name__,
        format="{message}",
        serialize=request.param,
        backtrace=True,
        diagnose=True,
    )
    try:
        yield messages
    finally:
        get_logger().remove(sink)


def _assert_safe_aws_error_log(messages, expected_message, level):
    assert messages
    assert "aws-error-secret-sentinel" not in "".join(messages)
    assert "Traceback (most recent call last)" not in "".join(messages)
    assert all(message.record["exception"] is None for message in messages)
    assert any(
        message.record["message"] == expected_message and message.record["level"].name == level
        for message in messages
    )


@pytest.mark.parametrize("error", [
    CredentialRetrievalError(provider="imds", error_msg="aws-error-secret-sentinel"),
    ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "aws-error-secret-sentinel"}}, "AssumeRole",
    ),
    OSError("aws-error-secret-sentinel"),
])
@pytest.mark.parametrize("static_fallback", [False, True])
def test_aws_credential_discovery_errors_do_not_log_details(
    monkeypatch, aws_session, aws_log_messages, error, static_fallback,
):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    if static_fallback:
        monkeypatch.setattr(litellm_handler, "get_settings", imds_tests._static_aws_settings)
    aws_session.get_credentials.side_effect = error

    handler = litellm_handler.LiteLLMAIHandler()

    assert not handler._aws_imds_mode
    assert handler._aws_imds_fell_back is static_fallback
    assert handler._aws_active_creds == (handler._aws_static_creds if static_fallback else {})
    _assert_safe_aws_error_log(
        aws_log_messages,
        "AWS_USE_IMDS: failed to resolve credentials via boto3; falling through to static keys: "
        f"{type(error).__name__}",
        "ERROR",
    )


def test_aws_region_error_does_not_log_details(monkeypatch, aws_session, aws_log_messages):
    monkeypatch.setenv("AWS_USE_IMDS", "true")
    type(aws_session).region_name = PropertyMock(side_effect=ValueError("aws-error-secret-sentinel"))

    handler = litellm_handler.LiteLLMAIHandler()

    assert handler._aws_imds_mode
    assert handler._aws_active_creds["aws_access_key_id"] == "IMDS-KEY"
    assert "aws_region_name" not in handler._aws_active_creds
    _assert_safe_aws_error_log(
        aws_log_messages, "AWS_USE_IMDS: failed to resolve region via boto3: ValueError", "WARNING",
    )
