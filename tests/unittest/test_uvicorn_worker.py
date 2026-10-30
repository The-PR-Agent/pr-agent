"""Exercise the Gunicorn workers configured for the service images."""

import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GUNICORN_SERVICES = ("github_app", "bitbucket_server_webhook", "gitlab_webhook", "gitea_app")


def _service_command(target):
    stage = None
    for line in (ROOT / "docker/Dockerfile").read_text().splitlines():
        if line.startswith("FROM "):
            stage = line.split()[-1]
        elif stage == target and line.startswith("CMD "):
            return json.loads(line.removeprefix("CMD "))
    raise AssertionError(f"Missing command for {target}")


@pytest.mark.skipif(sys.platform == "win32", reason="Gunicorn requires a Unix platform")
@pytest.mark.parametrize("target", GUNICORN_SERVICES)
def test_service_worker_serves_asgi_and_shuts_down(tmp_path, target):
    (tmp_path / "worker_smoke_app.py").write_text(
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/')\n"
        "async def health():\n"
        "    return {'worker': 'ready'}\n"
    )
    command = _service_command(target)
    command[0:1] = [sys.executable, "-W", "error::DeprecationWarning:uvicorn.workers"]
    config_index = command.index("-c") + 1
    command[config_index] = str(ROOT / command[config_index])
    # Exercise the shipped worker and configuration without provider credentials.
    command[-1] = "worker_smoke_app:app"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["GUNICORN_WORKERS"] = "1"
    env["OTEL__IS_ENABLED"] = "false"
    env["CONFIG__ANALYTICS_FOLDER"] = ""
    env.pop("GUNICORN_CMD_ARGS", None)
    env.pop("PROMETHEUS_MULTIPROC_DIR", None)
    env.pop("FORWARDED_ALLOW_IPS", None)
    log_path = tmp_path / "gunicorn.log"

    with socket.socket() as listener, log_path.open("w") as log:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        command.extend(["--bind", f"fd://{listener.fileno()}", "--graceful-timeout", "5"])
        process = subprocess.Popen(
            command, cwd=tmp_path, env=env, stdout=log, stderr=subprocess.STDOUT,
            pass_fds=(listener.fileno(),), start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                assert process.poll() is None, log_path.read_text()
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
                try:
                    connection.request("GET", "/")
                    response = connection.getresponse()
                    assert response.status == 200
                    assert json.loads(response.read()) == {"worker": "ready"}
                    break
                except (OSError, http.client.HTTPException):
                    time.sleep(0.05)
                finally:
                    connection.close()
            else:
                pytest.fail(f"{target} did not serve requests: {log_path.read_text()}")

            process.terminate()
            assert process.wait(timeout=15) == 0, log_path.read_text()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
