"""Synthetic cases from the comparison in discussion #3991."""

from unittest.mock import MagicMock

import pytest

from pr_agent.algo import artifacts
from pr_agent.git_providers.git_provider import redact_credentials

# Recovered from the comparison's deterministic synthetic corpus, never issued by a provider.
_PROVIDER_TOKENS = [
    ("stripe_key", "sk_live_" + "v92OSd1uYoO7wdcHgRbgjC3C"),
    ("twilio_api_key_sid", "SK" + "0395c55417e531ddd51ab615cb10c22e"),
    ("sendgrid_api_key",
     "SG." + "I4NnX1CZ3R3iAcJNZDlONV" + "." + "lgzxafzfAlyREgHB1kRWjrpApOcpq41l243kWiLBJRH"),
]


@pytest.mark.parametrize("kind, token", _PROVIDER_TOKENS)
def test_synthetic_provider_tokens_are_fully_redacted(kind, token):
    content = f"credential emitted {token}\nBuild failed at step compile.\n"
    counts = {}

    redacted = redact_credentials(content, redaction_counts=counts)

    assert redacted == "credential emitted <redacted>\nBuild failed at step compile.\n"
    assert counts == {kind: 1}
    repeated_counts = {}
    assert redact_credentials(redacted, redaction_counts=repeated_counts) == redacted
    assert repeated_counts == {}


@pytest.mark.parametrize("prefix", ["sk_live_", "sk_test_", "rk_live_", "rk_test_"])
@pytest.mark.parametrize("length", [24, 99, 300])
def test_stripe_secret_and_restricted_keys_are_not_partially_redacted(prefix, length):
    token = prefix + "aB12" * (length // 4) + "X" * (length % 4)

    assert redact_credentials(f"value '{token}'") == "value '<redacted>'"


@pytest.mark.parametrize("ending", ["_", "-"])
def test_sendgrid_urlsafe_suffix_is_fully_redacted(ending):
    token = "SG." + "a_-B" * 5 + "a_" + "." + "x_-Y" * 10 + "a-" + ending

    assert redact_credentials(f"[{token}]") == "[<redacted>]"


@pytest.mark.parametrize("content", [
    "request_id=0dc192a8-e88f-4b41-9f6b-9c0bb8652f50",
    "commit=5f06e679d819c13d5596228d61806169847b2e55",
    "sha256=8bf82d0888d00881d82b332935cce4dc155f82ab89c97c254ee6647e67d7e19b",
    'request_id="xaKDsklcPnreQW2ds1ZOm6Pq4Z38rw6A32sNQ5QgBJeeMbP1"',
    "pk_live_" + "A" * 24,
    "AC" + "a" * 32,
    "sk_live_short SK123 SG.short.value",
    "sk_live_" + "A" * 23,
    "SK" + "a" * 31,
    "SK" + "a" * 33,
    "SK" + "g" * 32,
    "0395c55417e531ddd51ab615cb10c22e",
    "sk_live_" + "A" * 24 + "_",
    "SG." + "a" * 22 + "." + "b" * 42,
    "SG." + "a" * 22 + "." + "b" * 44,
    "SG." + "a" * 22 + "." + "b" * 43 + "_",
    "prefixsk_live_" + "A" * 24,
])
def test_benign_corpus_and_incomplete_formats_are_unchanged(content):
    counts = {}

    assert redact_credentials(content, redaction_counts=counts) == content
    assert counts == {}


def test_existing_and_new_detectors_redact_the_original_mixed_corpus():
    content = (
        "ghp_" + "jxmmWPXs4w6QXTqY8viilxyQ3L2gYaLtfKov" + " and "
        "sk_live_" + "sSI2WdneMh8w2CZ11akcH3t8" + "\nBuild failed at step compile.\n"
    )
    counts = {}

    assert redact_credentials(content, redaction_counts=counts) == (
        "<redacted> and <redacted>\nBuild failed at step compile.\n"
    )
    assert counts == {"github_token": 1, "stripe_key": 1}


@pytest.mark.parametrize("embedded", ["AKIA" + "B" * 16, "glpat-" + "A" * 16])
def test_sendgrid_keys_are_redacted_before_embedded_token_patterns(embedded):
    payload = "-" + embedded + "-"
    token = "SG." + "A" * 22 + "." + payload.ljust(43, "b")
    counts = {}

    assert redact_credentials(token, redaction_counts=counts) == "<redacted>"
    assert counts == {"sendgrid_api_key": 1}


def test_mixed_provider_tokens_preserve_surrounding_text_and_counts():
    content = " | ".join(token for _, token in _PROVIDER_TOKENS)
    counts = {}

    assert redact_credentials(content, redaction_counts=counts) == "<redacted> | <redacted> | <redacted>"
    assert counts == {kind: 1 for kind, _ in _PROVIDER_TOKENS}


@pytest.mark.parametrize("kind, token", _PROVIDER_TOKENS)
def test_artifacts_redact_provider_tokens_before_truncation(tmp_path, monkeypatch, kind, token):
    path = tmp_path / "ci.log"
    path.write_text(token + "\n" + "ordinary build output\n" * 20, encoding="utf-8")
    logger = MagicMock()
    monkeypatch.setattr(artifacts, "get_logger", lambda: logger)

    context = artifacts._read_and_truncate(path, 70)

    assert context == "<redacted>\nordinary bui" + artifacts._TRUNCATION_MARKER
    logger.warning.assert_called_once_with(f"Redacted CI artifact credentials by type: {{{kind!r}: 1}}")
