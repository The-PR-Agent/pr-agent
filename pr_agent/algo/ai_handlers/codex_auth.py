import base64
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from pr_agent.config_loader import get_settings

CODEX_DEFAULT_API_BASE = "https://chatgpt.com/backend-api/codex"
CHATGPT_AUTH_CLAIM = "https://api.openai.com/auth"
OPENAI_COMPATIBLE_MODEL_PREFIXES = ("openai/",)
NON_OPENAI_MODEL_PREFIXES = (
    "anthropic/",
    "bedrock/",
    "codestral/",
    "cohere/",
    "databricks/",
    "deepinfra/",
    "deepseek/",
    "gemini/",
    "github/",
    "google/",
    "groq/",
    "huggingface/",
    "mistral/",
    "ollama/",
    "openrouter/",
    "replicate/",
    "sambanova/",
    "vertex_ai/",
    "xai/",
)


@dataclass(frozen=True)
class CodexAuth:
    api_base: Optional[str] = None
    api_key: Optional[str] = None
    access_token: Optional[str] = None
    account_id: Optional[str] = None
    is_fedramp_account: bool = False


def _get_setting(settings: Any, key: str, default: Any = None) -> Any:
    try:
        value = settings.get(key, default)
    except Exception:
        value = default
    return value if value not in (None, "") else default


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2 or not parts[1]:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def _load_auth_json(settings: Any) -> Optional[dict[str, Any]]:
    inline_auth = _get_setting(settings, "CODEX.AUTH_JSON")
    if inline_auth:
        try:
            return json.loads(inline_auth)
        except json.JSONDecodeError as exc:
            raise ValueError(f"CODEX.AUTH_JSON contains invalid JSON: {exc}") from exc

    auth_json_path = _get_setting(settings, "CODEX.AUTH_JSON_PATH")
    if not auth_json_path:
        return None

    path = Path(str(auth_json_path)).expanduser()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"CODEX.AUTH_JSON_PATH does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"CODEX.AUTH_JSON_PATH contains invalid JSON: {path}: {exc}") from exc


def load_codex_auth_from_settings(settings: Any = None) -> Optional[CodexAuth]:
    settings = settings or get_settings()
    auth_json = _load_auth_json(settings)
    if auth_json is None:
        return None
    if not isinstance(auth_json, dict):
        raise ValueError("Codex auth.json must be a JSON object")

    configured_api_base = _get_setting(settings, "CODEX.API_BASE")
    api_key = auth_json.get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key.strip():
        return CodexAuth(api_base=configured_api_base, api_key=api_key.strip())

    tokens = auth_json.get("tokens")
    if not isinstance(tokens, dict):
        raise ValueError("Codex auth.json must contain either OPENAI_API_KEY or tokens.access_token")

    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise ValueError("Codex auth.json must contain either OPENAI_API_KEY or tokens.access_token")

    id_token = tokens.get("id_token")
    auth_claims = _decode_jwt_payload(id_token).get(CHATGPT_AUTH_CLAIM, {}) if isinstance(id_token, str) else {}
    auth_claims = auth_claims if isinstance(auth_claims, dict) else {}

    account_id = tokens.get("account_id") or auth_claims.get("chatgpt_account_id")
    account_id = account_id.strip() if isinstance(account_id, str) and account_id.strip() else None
    is_fedramp_account = bool(auth_claims.get("chatgpt_account_is_fedramp"))

    return CodexAuth(
        api_base=configured_api_base or CODEX_DEFAULT_API_BASE,
        access_token=access_token.strip(),
        account_id=account_id,
        is_fedramp_account=is_fedramp_account,
    )


def should_apply_codex_auth(model: str) -> bool:
    if model.startswith(OPENAI_COMPATIBLE_MODEL_PREFIXES):
        return True
    if model.startswith(NON_OPENAI_MODEL_PREFIXES):
        return False
    return "/" not in model


def apply_codex_auth_to_kwargs(kwargs: dict[str, Any], settings: Any = None) -> None:
    if not should_apply_codex_auth(str(kwargs.get("model", ""))):
        return

    auth = load_codex_auth_from_settings(settings)
    if auth is None:
        return

    if auth.api_base:
        kwargs["api_base"] = auth.api_base
    if auth.api_key:
        kwargs["api_key"] = auth.api_key
        return

    kwargs["api_key"] = auth.access_token
    headers = dict(kwargs.get("extra_headers") or {})
    if auth.account_id:
        headers["ChatGPT-Account-ID"] = auth.account_id
    if auth.is_fedramp_account:
        headers["X-OpenAI-Fedramp"] = "true"
    if headers:
        kwargs["extra_headers"] = headers
