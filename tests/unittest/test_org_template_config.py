from pr_agent.config_loader import get_settings
from pr_agent.tools.pr_description import load_org_template


def test_org_toggles_default_false():
    """Test A: both fork toggles read False via the absent-safe .get(<flag>, False) accessor."""
    settings = get_settings()
    assert settings.pr_description.get("enable_conventional_title", False) is False
    assert settings.pr_description.get("enable_org_template", False) is False


def test_absent_flag_read_is_safe():
    """Test B: reading a missing key via .get(<key>, False) returns False and never raises."""
    settings = get_settings()
    assert settings.pr_description.get("some_absent_fork_flag", False) is False


def test_org_template_loads_from_package_path():
    """Test C: the fork-owned template resolves at the package-relative path and has expected content."""
    text = load_org_template()
    assert text.strip()
    assert "What does this MR do? Why?" in text
    assert "Note / Risk" in text
    assert "- [ ]" in text
