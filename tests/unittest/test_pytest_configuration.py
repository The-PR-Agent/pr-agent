import os
import subprocess
import sys


_PYTEST_COLLECTION_TIMEOUT_SECONDS = 30


def test_default_pytest_configuration_is_loaded(pytestconfig):
    assert pytestconfig.inipath is not None
    assert pytestconfig.inipath.name == "pyproject.toml"
    assert pytestconfig.getini("testpaths") == ["tests/unittest"]
    assert pytestconfig.getini("asyncio_mode") == "auto"
    assert "--import-mode=importlib" in pytestconfig.getini("addopts")


def test_importlib_mode_collects_duplicate_test_basenames(tmp_path, pytestconfig):
    unit_dir = tmp_path / "tests" / "unittest"
    e2e_dir = tmp_path / "tests" / "e2e_tests"
    unit_dir.mkdir(parents=True)
    e2e_dir.mkdir(parents=True)

    unit_test = unit_dir / "test_duplicate.py"
    e2e_test = e2e_dir / "test_duplicate.py"
    unit_test.write_text("def test_unit():\n    pass\n")
    e2e_test.write_text("def test_e2e():\n    pass\n")

    import_mode = "--import-mode=importlib"
    assert import_mode in pytestconfig.getini("addopts")

    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            import_mode,
            "--rootdir=.",
            "--color=no",
            "--collect-only",
            "-q",
            str(unit_test),
            str(e2e_test),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=_PYTEST_COLLECTION_TIMEOUT_SECONDS,
        check=False,
    )

    output = result.stdout.replace("\\", "/")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "tests/unittest/test_duplicate.py::test_unit" in output
    assert "tests/e2e_tests/test_duplicate.py::test_e2e" in output
