"""Guard: every explicit uv pin names the release pyproject.toml requires.

Both Dockerfiles copy a uv image by release and digest, five workflows pin a
setup-uv release, build-and-test.yaml installs uv with pipx, and pyproject.toml
declares the version floor. History shows these are kept in sync by hand:
#3153 added required-version with only a "keep in sync" comment, #4263 updated
four files for a security pin, and #4898 updated five for the 0.13.8 bump.
This test turns that manual rule into a CI failure.

Only the literal forms used here are modeled. The Docker test image copies
three of the five workflows; the native unit job uses a full checkout and sees
all of them. A digest that does not really resolve to its release is out of
scope.
"""
import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
DOCKERFILES = (ROOT / "docker" / "Dockerfile", ROOT / "docker" / "Dockerfile.lambda")
WORKFLOWS_DIR = ROOT / ".github" / "workflows"
UV_COPY = re.compile(r"^[ \t]*COPY[ \t]+--from=ghcr\.io/astral-sh/uv:(\S+)", re.MULTILINE)
PIPX_UV = re.compile(r"\bpipx[ \t]+install[ \t]+['\"]?uv(?![\w.-])([^'\"\s]*)['\"]?")
SETUP_UV = "astral-sh/setup-uv@"


def test_uv_pins_agree() -> None:
    required = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["uv"]["required-version"]
    # '==' broke Dependabot in the past, so keep the '>=X.Y.Z' form and use it
    # as the single source of truth for every other pin.
    assert re.fullmatch(r">=\d+\.\d+\.\d+", required), f"expected a '>=X.Y.Z' floor, got {required!r}"
    expected = required.removeprefix(">=")

    images: dict[str, str] = {}
    for path in DOCKERFILES:
        found = UV_COPY.findall(path.read_text(encoding="utf-8"))
        assert len(found) == 1, f"{path.name} must pin exactly one uv image, found {found}"
        assert re.fullmatch(rf"{re.escape(expected)}@sha256:[0-9a-f]{{64}}", found[0]), (
            f"{path.name}: expected {expected}@sha256:<digest>, got {found[0]}"
        )
        images[path.name] = found[0]
    assert len(set(images.values())) == 1, f"uv image digests disagree: {images}"

    setup_pins: dict[str, object] = {}
    pipx_pins: dict[str, object] = {}
    for path in sorted(WORKFLOWS_DIR.glob("*.y*ml")):
        jobs = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("jobs") or {}
        for job_id, job in jobs.items():
            for index, step in enumerate(job.get("steps") or []):
                location = f"{path.name}:{job_id}[{index}]"
                if str(step.get("uses", "")).startswith(SETUP_UV):
                    setup_pins[f"{location}:setup-uv"] = (step.get("with") or {}).get("version")
                run = "\n".join(
                    line for line in (step.get("run") or "").splitlines()
                    if not line.lstrip().startswith("#")
                )
                for number, spec in enumerate(PIPX_UV.findall(run)):
                    value = spec.removeprefix("==") if spec.startswith("==") else f"uv{spec}"
                    pipx_pins[f"{location}:pipx[{number}]"] = value

    assert setup_pins, "expected setup-uv pins in the workflows"
    assert pipx_pins, "expected pinned pipx install uv in the workflows"
    pins = {**setup_pins, **pipx_pins}
    bad = {location: value for location, value in pins.items() if value != expected}
    assert not bad, f"uv pins disagree with required-version {required!r}: {bad}"
