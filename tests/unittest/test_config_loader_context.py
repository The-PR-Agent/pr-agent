import copy

from starlette_context import request_cycle_context

from pr_agent.config_loader import get_settings, global_settings


def test_get_settings_can_explicitly_bypass_request_context():
    original_model = global_settings.get("CONFIG.MODEL")
    request_settings = copy.deepcopy(global_settings)
    request_settings.set("CONFIG.MODEL", "request-model")
    global_settings.set("CONFIG.MODEL", "global-model")

    try:
        with request_cycle_context({"settings": request_settings}):
            assert get_settings().get("CONFIG.MODEL") == "request-model"
            assert get_settings(use_context=False).get("CONFIG.MODEL") == "global-model"
    finally:
        global_settings.set("CONFIG.MODEL", original_model)
