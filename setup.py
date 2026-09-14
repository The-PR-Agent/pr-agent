from pathlib import Path
from shutil import rmtree

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py

PROJECT_ROOT = Path(__file__).resolve().parent
HELP_DOCS_SOURCE = PROJECT_ROOT / "docs" / "docs"
HELP_DOCS_PACKAGE = Path("pr_agent") / "_help_docs"


class BuildPy(_build_py):
    """Build the external help corpus as package resources."""

    def _help_docs_output_mapping(self) -> dict[str, str]:
        return {
            str(Path(self.build_lib) / HELP_DOCS_PACKAGE / source.relative_to(HELP_DOCS_SOURCE)): str(source)
            for source in sorted(HELP_DOCS_SOURCE.rglob("*.md"))
            if source.is_file()
        }

    def get_output_mapping(self) -> dict[str, str]:
        mapping = super().get_output_mapping()
        mapping.update(self._help_docs_output_mapping())
        return mapping

    def get_outputs(self, include_bytecode: bool = True) -> list[str]:
        outputs = super().get_outputs(include_bytecode)
        return list(dict.fromkeys([*outputs, *self._help_docs_output_mapping()]))

    def run(self) -> None:
        super().run()
        if getattr(self, "editable_mode", False):
            return

        mapping = self._help_docs_output_mapping()
        if not mapping:
            raise FileNotFoundError(f"No Markdown help documents found under {HELP_DOCS_SOURCE}")

        destination = Path(self.build_lib) / HELP_DOCS_PACKAGE
        if destination.exists() and not self.dry_run:
            rmtree(destination)

        for output, source in mapping.items():
            self.mkpath(str(Path(output).parent))
            self.copy_file(source, output)


setup(cmdclass={"build_py": BuildPy})
