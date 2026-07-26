"""Dependency-closure guard for the 'langfuse_otel' litellm callback.

pr_agent/mosaico/env_bridge.py registers 'langfuse_otel' as the Langfuse callback
(the legacy 'langfuse' callback raises a ``sdk_integration`` TypeError against
langfuse 3.x). litellm imports pydantic-settings unconditionally from
``litellm/integrations/otel/__init__.py`` but declares it only under its optional
'proxy' extra, so an environment built from requirements.txt without that pin makes
litellm's callback factory return None -- silently, because the factory swallows the
ImportError -- and not a single LLM call is traced.

These tests assert the environment, not pr-agent logic, hence a file of their own
rather than an addition to test_mosaico_env_bridge.py. They are meaningful in CI:
.github/workflows/build-and-test.yaml builds docker/Dockerfile's ``test`` target,
whose dependencies come from requirements.txt via pyproject, and runs this suite
inside that image -- the same base layer the shipped mosaico_agent image is built
from. No network, no live LLM."""
import importlib

import pytest

CALLBACK_NAME = "langfuse_otel"
# Imported at module scope by litellm/integrations/otel/__init__.py, declared only under
# litellm's 'proxy' extra, so it must be pinned explicitly in pr-agent's requirements.txt.
UNDECLARED_LITELLM_DEP = "pydantic_settings"

_REMEDY = (
    f"'{UNDECLARED_LITELLM_DEP}' must be pinned in requirements.txt: litellm imports it "
    f"unconditionally from litellm/integrations/otel/__init__.py but declares it only under "
    f"its optional 'proxy' extra."
)


def _import_error(module_name: str):
    """Return the ImportError raised by importing ``module_name``, or None on success."""
    try:
        importlib.import_module(module_name)
    except ImportError as e:
        return e
    return None


@pytest.fixture
def restore_in_memory_loggers():
    """Constructing the callback appends it to litellm's module-global logger registry.

    Reaching into litellm privates is unavoidable here -- there is no public API that
    reports a callback litellm declined to build -- but it should never be the reason
    this test fails. If the registry is renamed or removed upstream, skip the cleanup
    rather than erroring: the assertions below still carry the signal we care about.
    """
    from litellm.litellm_core_utils import litellm_logging
    registry = getattr(litellm_logging, "_in_memory_loggers", None)
    snapshot = list(registry) if registry is not None else None
    yield
    if snapshot is not None:
        registry[:] = snapshot


class TestLangfuseOtelCallbackDeps:
    def test_pydantic_settings_is_installed(self):
        err = _import_error(UNDECLARED_LITELLM_DEP)
        assert err is None, f"{_REMEDY} Import failed with: {err!r}"

    def test_litellm_otel_integration_imports(self):
        """The unconditional import site itself -- fails before the callback factory is reached."""
        err = _import_error("litellm.integrations.otel")
        assert err is None, f"litellm.integrations.otel is not importable ({err!r}). {_REMEDY}"

    def test_langfuse_otel_callback_constructs(self, restore_in_memory_loggers):
        """The behaviour MOSAICO actually depends on: env_bridge registers this callback name,
        and litellm must be able to build a logger for it or every trace is dropped."""
        from litellm.litellm_core_utils.litellm_logging import \
            _init_custom_logger_compatible_class

        logger = _init_custom_logger_compatible_class(
            CALLBACK_NAME, internal_usage_cache=None, llm_router=None)

        assert logger is not None, (
            f"litellm returned no logger for the '{CALLBACK_NAME}' callback, so MOSAICO's Langfuse "
            f"tracing is dead. The factory swallows the underlying error; the likely cause is "
            f"{_import_error('litellm.integrations.otel')!r}. {_REMEDY}"
        )
        # Substring, not equality: `is not None` above is the assertion that catches the
        # actual defect. This one only guards against litellm handing back some unrelated
        # logger, so it should survive an upstream rename (LangfuseOtelLoggerV2 and the
        # like) rather than failing CI over a refactor that changed nothing that matters.
        assert "langfuse" in type(logger).__name__.lower(), (
            f"Expected a Langfuse logger for the '{CALLBACK_NAME}' callback, got {type(logger).__name__}."
        )
