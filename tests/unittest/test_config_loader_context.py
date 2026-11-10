import copy

from starlette_context import request_cycle_context

from pr_agent.config_loader import get_settings, global_settings


def test_get_settings_can_bypass_request_context():
    request_settings = copy.deepcopy(global_settings)

    with request_cycle_context({"settings": request_settings}):
        assert get_settings() is request_settings
        unscoped = get_settings(use_context=False)

    assert unscoped is global_settings

def test_get_settings_uses_global_settings_without_context():
    assert get_settings() is global_settings
    assert get_settings(use_context=False) is global_settings
