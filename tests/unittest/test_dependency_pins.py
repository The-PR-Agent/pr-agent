import tomllib
from pathlib import Path

from packaging.version import Version


def test_langchain_extra_does_not_pin_vulnerable_core():
    repo_root = Path(__file__).resolve().parents[2]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text())
    langchain_extra = pyproject["project"]["optional-dependencies"]["langchain"]
    core_pin = next((pin for pin in langchain_extra if pin.startswith("langchain-core==")), None)

    assert core_pin is not None
    assert Version(core_pin.split("==", 1)[1]) >= Version("0.3.81")
