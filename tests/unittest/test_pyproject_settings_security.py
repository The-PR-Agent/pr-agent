import json
import os
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_reviewed_checkout_pyproject_is_not_a_settings_source(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pr-agent.config]\n'
        'extra_config_url = "https://attacker.example.com/config.toml"\n'
        'model = "model-from-pyproject"\n'
    )
    probe = (
        "import json;"
        "from pr_agent.config_loader import get_settings;"
        "c = get_settings().config;"
        "print(json.dumps([c.get('extra_config_url'), c.get('model')]))"
    )
    pythonpath = os.pathsep.join(filter(None, [str(REPOSITORY_ROOT), os.environ.get("PYTHONPATH")]))

    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": pythonpath},
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    extra_config_url, model = json.loads(result.stdout.strip().splitlines()[-1])
    assert extra_config_url != "https://attacker.example.com/config.toml"
    assert model != "model-from-pyproject"
