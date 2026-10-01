"""Guard: every interpreter pyproject singles out is exercised by build-and-test.yaml.

`requires-python = ">=3.12"` on its own is open-ended, but the per-interpreter
dependency pins say which minors support is actually claimed for -- currently
google-cloud-storage, split at `python_version >= '3.13'` for #2480. CI used to
run only inside docker/Dockerfile's base image, so whichever minor that image
did not carry went declared and never tested (#3187). The base has since moved
to 3.14 (#3294), which is why both declared minors now need native jobs.

Keep docker/Dockerfile.lambda on the same Python minor: #3294 left the
Lambda image behind on 3.12 while the main Dockerfile moved to 3.14. The sync
test below pins the two bases together so a one-file bump cannot slip
through silently again.

These tests assert the repository's CI configuration, not pr_agent logic, hence a
file of their own. They read the checked-in workflow and Dockerfiles, which the
`test` docker target copies into the image alongside publish.yml so the suite
carries the same signal in CI as it does locally. No network, no docker.
"""
import re
import tomllib
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BUILD_AND_TEST_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "build-and-test.yaml"
PYPROJECT = REPOSITORY_ROOT / "pyproject.toml"
DOCKERFILE = REPOSITORY_ROOT / "docker" / "Dockerfile"
LAMBDA_DOCKERFILE = REPOSITORY_ROOT / "docker" / "Dockerfile.lambda"

# "google-cloud-storage==3.12.0; python_version >= '3.13'" -> "3.13"
PIN_MARKER_VERSION = re.compile(r"""python_version\s*>=\s*['"](\d+\.\d+)""")
REQUIRES_PYTHON_MINIMUM = re.compile(r">=\s*(\d+\.\d+)")
# Only the base stage names an image; every other stage is "FROM base AS ...".
DOCKER_BASE_VERSION = re.compile(r"^FROM python:(\d+\.\d+)", re.MULTILINE)
LAMBDA_BASE_VERSION = re.compile(r"^FROM public\.ecr\.aws/lambda/python:(\d+\.\d+)", re.MULTILINE)
LAMBDA_INTERPRETER_VERSION = re.compile(
    r"^RUN uv sync\b.*--python /var/lang/bin/python(\d+\.\d+)(?:\s|$)",
    re.MULTILINE,
)
LAMBDA_SITE_PACKAGES_VERSION = re.compile(
    r'^ENV PYTHONPATH=["\']?/opt/venv/lib/python(\d+\.\d+)/site-packages',
    re.MULTILINE,
)
UV_PYTHON_FLAG = re.compile(r"--python[ =](\d+\.\d+)")


def _declared_python_versions() -> set[str]:
    """Minors pyproject claims support for: the floor, plus every shipped pin marker."""
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    shipped_dependencies = [
        *project["dependencies"],
        *project["optional-dependencies"].get("all", []),
    ]
    versions = set(PIN_MARKER_VERSION.findall(" ".join(shipped_dependencies)))
    versions.add(REQUIRES_PYTHON_MINIMUM.search(project["requires-python"]).group(1))
    return versions


def _tested_python_versions() -> set[str]:
    """Minors build-and-test.yaml runs the unit suite on.

    The Dockerfile base counts because the workflow builds its ``test`` target and
    runs pytest inside it; native jobs pin their interpreter on the uv command line,
    through setup-python, or over a matrix. Matrix legs are read from the strategy
    rather than the step, because the step only carries the expression.
    """
    versions = set(DOCKER_BASE_VERSION.findall(DOCKERFILE.read_text(encoding="utf-8")))
    workflow = yaml.safe_load(BUILD_AND_TEST_WORKFLOW.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        matrix = (job.get("strategy") or {}).get("matrix") or {}
        versions.update(str(leg) for leg in matrix.get("python-version", []))
        for step in job.get("steps", []):
            versions.update(UV_PYTHON_FLAG.findall(step.get("run", "")))
            pinned = (step.get("with") or {}).get("python-version")
            if pinned:
                versions.add(str(pinned))
    return versions


def test_every_declared_python_version_is_tested_in_ci() -> None:
    declared = _declared_python_versions()
    tested = _tested_python_versions()

    # Without a conditional pin the declared set collapses to the floor alone,
    # which the docker base always satisfies -- the assertion below would then
    # pass vacuously however little CI actually ran.
    assert len(declared) > 1, (
        f"Parsed no per-interpreter pin out of pyproject's dependencies, so this guard would "
        f"pass vacuously. Either the pins are gone (and requires-python should be narrowed to "
        f"match) or {PIN_MARKER_VERSION.pattern!r} has gone stale."
    )
    assert declared <= tested, (
        f"pyproject declares support for Python {sorted(declared)}, but build-and-test.yaml only "
        f"exercises {sorted(tested)}. Add a job for {sorted(declared - tested)}, or narrow "
        f"requires-python and drop the now-unreachable dependency pins."
    )


def test_lambda_base_matches_main_base() -> None:
    """docker/Dockerfile.lambda must track docker/Dockerfile's Python minor.

    #3294 moved the main image to 3.14 while the Lambda image stayed on
    3.12, and nothing failed because no test read the Lambda Dockerfile.
    Only the minor is asserted: AWS publishes no CPython patch tags for
    Lambda bases, so the Lambda Dockerfile intentionally floats on the
    minor tag instead of chasing per-build dated tags. The uv interpreter
    path and site-packages PYTHONPATH embed the same minor and are
    asserted too, because nothing builds this image in CI before publish.
    """
    main = DOCKER_BASE_VERSION.search(DOCKERFILE.read_text(encoding="utf-8"))
    lambda_dockerfile = LAMBDA_DOCKERFILE.read_text(encoding="utf-8")
    lambda_base = LAMBDA_BASE_VERSION.search(lambda_dockerfile)
    interpreter = LAMBDA_INTERPRETER_VERSION.search(lambda_dockerfile)
    site_packages = LAMBDA_SITE_PACKAGES_VERSION.search(lambda_dockerfile)
    assert main is not None, f"could not parse a base Python version out of {DOCKERFILE}"
    assert lambda_base is not None, f"could not parse a base Python version out of {LAMBDA_DOCKERFILE}"
    assert lambda_base.group(1) == main.group(1), (
        f"docker/Dockerfile is on Python {main.group(1)} but docker/Dockerfile.lambda is on "
        f"Python {lambda_base.group(1)}. Bump the Lambda base tag, interpreter path, and "
        f"site-packages PYTHONPATH together."
    )
    assert interpreter is not None, (
        f"could not parse the uv --python interpreter version out of {LAMBDA_DOCKERFILE}"
    )
    assert site_packages is not None, (
        f"could not parse the site-packages PYTHONPATH version out of {LAMBDA_DOCKERFILE}"
    )
    assert interpreter.group(1) == lambda_base.group(1) == site_packages.group(1), (
        f"docker/Dockerfile.lambda mixes Python versions: base image {lambda_base.group(1)}, "
        f"interpreter {interpreter.group(1)}, site-packages {site_packages.group(1)}. Bump the "
        f"base tag, interpreter path, and site-packages PYTHONPATH together."
    )
