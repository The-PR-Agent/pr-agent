def test_default_pytest_configuration_is_loaded(pytestconfig):
    assert pytestconfig.inipath is not None
    assert pytestconfig.inipath.name == "pyproject.toml"
    assert pytestconfig.getini("testpaths") == ["tests/unittest"]
    assert pytestconfig.getini("asyncio_mode") == "auto"
