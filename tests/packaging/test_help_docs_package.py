"""End-to-end packaging regression for the question-mode /help corpus.

Run explicitly because the default unit-test container does not include the
repository-level build inputs used here::

    uv run pytest -q tests/packaging/test_help_docs_package.py
"""

import json
import os
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tomllib
import zipfile
from pathlib import Path, PurePosixPath

from pr_agent.tools.pr_help_message import _is_help_doc_included

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RESOURCE_PREFIX = "pr_agent/_help_docs/"
SMOKE_SCRIPT = r"""
import asyncio
import json
import os
import site
import sys
from pathlib import Path
from types import SimpleNamespace

blocked_source = Path(os.environ["BLOCKED_SOURCE"]).resolve()
sys.path[:] = [
    entry
    for entry in sys.path
    if not entry or Path(entry).resolve() != blocked_source
]
editable_target = os.environ.get("EDITABLE_TARGET")
if editable_target:
    site.addsitedir(editable_target)
sys.path.append(os.environ["DEPENDENCY_PATH"])

from pr_agent.tools import pr_help_message
from pr_agent.tools.pr_help_message import PRHelpMessage

calls = []

async def fake_retry(_prediction, *, model_type):
    calls.append(model_type)
    assert "==file name==" in tool.vars["snippets"]
    assert "/tools/review.md" in tool.vars["snippets"]
    return "response: ok\nrelevant_sections: []"

pr_help_message.retry_with_fallback_models = fake_retry
tool = PRHelpMessage.__new__(PRHelpMessage)
tool.git_provider = SimpleNamespace(pr_url="https://example.com/org/repo/pull/1")
tool.ai_handler = SimpleNamespace()
tool.question_str = "How does review work?"
tool.return_as_string = False
tool.vars = {"question": tool.question_str, "snippets": ""}
asyncio.run(tool.run())

root = pr_help_message._get_help_docs_root()
documents = [path.as_posix() for path, _resource in pr_help_message._iter_help_docs(root)]
print(json.dumps({
    "module": pr_help_message.__file__,
    "root": str(root),
    "documents": documents,
    "snippet_chars": len(tool.vars["snippets"]),
    "model_calls": len(calls),
}))
"""
ZIP_RESOURCE_SCRIPT = r"""
import json
from importlib.resources import files

root = files("pr_agent").joinpath("_help_docs")

def walk(directory, prefix=""):
    documents = []
    for child in directory.iterdir():
        relative = f"{prefix}/{child.name}" if prefix else child.name
        if child.is_dir():
            documents.extend(walk(child, relative))
        elif child.is_file() and child.name.endswith(".md"):
            assert child.read_text(encoding="utf-8") is not None
            documents.append(relative)
    return documents

print(json.dumps({"root": str(root), "documents": sorted(walk(root))}))
"""


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    assert result.returncode == 0, (
        f"Command failed ({result.returncode}): {' '.join(command)}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _copy_repository(destination: Path) -> None:
    shutil.copytree(
        REPOSITORY_ROOT,
        destination,
        ignore=shutil.ignore_patterns(".git", ".venv", "build", "dist", "__pycache__", ".pytest_cache"),
    )


def _source_documents(repository: Path) -> set[str]:
    docs_root = repository / "docs" / "docs"
    return {
        document.relative_to(docs_root).as_posix()
        for document in docs_root.rglob("*.md")
        if document.is_file()
    }


def _sdist_documents(sdist: Path) -> set[str]:
    with tarfile.open(sdist) as archive:
        return {
            name.split("/docs/docs/", 1)[1]
            for name in archive.getnames()
            if "/docs/docs/" in name and name.endswith(".md")
        }


def _wheel_documents(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        return {
            name.removeprefix(RESOURCE_PREFIX)
            for name in archive.namelist()
            if name.startswith(RESOURCE_PREFIX) and name.endswith(".md")
        }


def _build_sdist(uv: str, repository: Path, output: Path, env: dict[str, str]) -> Path:
    _run([uv, "build", "--sdist", "--out-dir", str(output), str(repository)], cwd=repository, env=env)
    return next(output.glob("*.tar.gz"))


def _build_wheel(uv: str, repository: Path, output: Path, env: dict[str, str]) -> Path:
    _run([uv, "build", "--wheel", "--out-dir", str(output), str(repository)], cwd=repository, env=env)
    return next(output.glob("*.whl"))


def _run_smoke(
    python: str,
    temporary_root: Path,
    *,
    blocked_source: Path,
    python_path: Path | None = None,
    editable_target: Path | None = None,
) -> dict:
    env = os.environ.copy()
    env["BLOCKED_SOURCE"] = str(blocked_source)
    env["DEPENDENCY_PATH"] = sysconfig.get_paths()["purelib"]
    if python_path:
        env["PYTHONPATH"] = str(python_path)
    else:
        env.pop("PYTHONPATH", None)
    if editable_target:
        env["EDITABLE_TARGET"] = str(editable_target)
    else:
        env.pop("EDITABLE_TARGET", None)
    result = _run([python, "-S", "-c", SMOKE_SCRIPT], cwd=temporary_root, env=env)
    return json.loads(result.stdout.strip().splitlines()[-1])


def _run_zip_resource_smoke(python: str, temporary_root: Path, wheel: Path) -> dict:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(wheel)
    result = _run([python, "-S", "-c", ZIP_RESOURCE_SCRIPT], cwd=temporary_root, env=env)
    return json.loads(result.stdout.strip().splitlines()[-1])


def _install_editable(
    uv: str,
    repository: Path,
    target: Path,
    env: dict[str, str],
    *,
    strict: bool,
) -> None:
    command = [uv, "pip", "install", "--target", str(target), "--no-deps", "--editable", str(repository)]
    if strict:
        command.extend(["--config-setting", "editable_mode=strict"])
    _run(command, cwd=repository, env=env)


def _replace_symlinks_with_hardlinks(root: Path) -> None:
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            source = path.resolve()
            path.unlink()
            os.link(source, path)


def test_help_docs_distribution_contract(tmp_path):
    uv = shutil.which("uv")
    assert uv, "uv is required for the packaging regression"
    with (REPOSITORY_ROOT / "pyproject.toml").open("rb") as pyproject:
        required_uv = tomllib.load(pyproject)["tool"]["uv"]["required-version"].removeprefix("==")
    assert _run([uv, "--version"], cwd=tmp_path).stdout.startswith(f"uv {required_uv} ")

    build_env = os.environ.copy()
    build_env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    source_copy = tmp_path / "source"
    _copy_repository(source_copy)
    expected_documents = _source_documents(source_copy)
    assert expected_documents
    expected_runtime_documents = {
        document for document in expected_documents if _is_help_doc_included(PurePosixPath(document))
    }

    sdist = _build_sdist(uv, source_copy, tmp_path / "sdist", build_env)
    assert _sdist_documents(sdist) == expected_documents

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    with tarfile.open(sdist) as archive:
        archive.extractall(extracted, filter="data")
    sdist_source = next(extracted.iterdir())
    wheel = _build_wheel(uv, sdist_source, tmp_path / "wheel", build_env)
    assert _wheel_documents(wheel) == expected_documents

    installed = tmp_path / "installed"
    _run([uv, "pip", "install", "--target", str(installed), "--no-deps", str(wheel)], cwd=tmp_path, env=build_env)
    installed_result = _run_smoke(
        sys.executable,
        tmp_path,
        blocked_source=REPOSITORY_ROOT,
        python_path=installed,
    )
    assert Path(installed_result["module"]).is_relative_to(installed)
    assert set(installed_result["documents"]) == expected_runtime_documents
    assert installed_result["model_calls"] == 1
    assert installed_result["snippet_chars"] > 0

    zip_result = _run_zip_resource_smoke(sys.executable, tmp_path, wheel)
    assert ".whl/pr_agent/_help_docs" in zip_result["root"]
    assert set(zip_result["documents"]) == expected_documents

    default_target = tmp_path / "editable-default"
    _install_editable(uv, source_copy, default_target, build_env, strict=False)
    default_result = _run_smoke(
        sys.executable,
        tmp_path,
        blocked_source=REPOSITORY_ROOT,
        editable_target=default_target,
    )
    assert Path(default_result["module"]).is_relative_to(source_copy)
    assert Path(default_result["root"]).is_relative_to(source_copy / "docs" / "docs")
    assert set(default_result["documents"]) == expected_runtime_documents

    strict_target = tmp_path / "editable-strict"
    _install_editable(uv, source_copy, strict_target, build_env, strict=True)
    strict_trees = list((source_copy / "build").glob("__editable__.*"))
    assert len(strict_trees) == 1
    strict_tree = strict_trees[0]
    strict_resource_root = strict_tree / RESOURCE_PREFIX.removesuffix("/")
    assert {
        document.relative_to(strict_resource_root).as_posix()
        for document in strict_resource_root.rglob("*.md")
    } == expected_documents

    _replace_symlinks_with_hardlinks(strict_tree)
    assert not any(path.is_symlink() for path in strict_tree.rglob("*"))
    strict_result = _run_smoke(
        sys.executable,
        tmp_path,
        blocked_source=REPOSITORY_ROOT,
        editable_target=strict_target,
    )
    assert Path(strict_result["module"]).is_relative_to(strict_tree)
    assert Path(strict_result["root"]).is_relative_to(strict_resource_root)
    assert set(strict_result["documents"]) == expected_runtime_documents
    assert strict_result["model_calls"] == 1

    first_direct_wheel = _build_wheel(uv, source_copy, tmp_path / "first-direct-wheel", build_env)
    assert _wheel_documents(first_direct_wheel) == expected_documents

    removed_document = source_copy / "docs" / "docs" / "summary.md"
    assert removed_document.exists()
    removed_document.unlink()
    rebuilt_wheel = _build_wheel(uv, source_copy, tmp_path / "rebuilt-wheel", build_env)
    assert _wheel_documents(rebuilt_wheel) == expected_documents - {"summary.md"}


def test_lambda_image_materializes_only_help_markdown_in_shared_base():
    dockerfile = (REPOSITORY_ROOT / "docker" / "Dockerfile.lambda").read_text(encoding="utf-8")

    mount = "RUN --mount=type=bind,source=docs/docs,target=/tmp/help-docs-source"
    assert mount in dockerfile
    assert f"{mount},rw" not in dockerfile
    assert f"{mount},readwrite" not in dockerfile
    assert "COPY docs/docs" not in dockerfile
    assert "find /tmp/help-docs-source -type f -name '*.md'" in dockerfile
    assert 'test "$source_count" -gt 0' in dockerfile
    assert 'test "$destination_count" -eq "$source_count"' in dockerfile
    assert "-type f ! -name '*.md' -print -quit" in dockerfile
    assert "FROM base AS github_lambda" in dockerfile
    assert "FROM base AS gitlab_lambda" in dockerfile


def test_lambda_layout_loads_materialized_help_docs(tmp_path):
    lambda_root = tmp_path / "lambda"
    shutil.copytree(REPOSITORY_ROOT / "pr_agent", lambda_root / "pr_agent")
    resource_root = lambda_root / RESOURCE_PREFIX.removesuffix("/")
    expected_documents = _source_documents(REPOSITORY_ROOT)

    for relative_name in expected_documents:
        source = REPOSITORY_ROOT / "docs" / "docs" / relative_name
        destination = resource_root / relative_name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    materialized_documents = {
        document.relative_to(resource_root).as_posix()
        for document in resource_root.rglob("*")
        if document.is_file()
    }
    assert materialized_documents == expected_documents

    result = _run_smoke(
        sys.executable,
        tmp_path,
        blocked_source=REPOSITORY_ROOT,
        python_path=lambda_root,
    )
    assert Path(result["module"]).is_relative_to(lambda_root)
    assert Path(result["root"]).is_relative_to(resource_root)
    assert set(result["documents"]) == {
        document for document in expected_documents if _is_help_doc_included(PurePosixPath(document))
    }
    assert result["model_calls"] == 1
