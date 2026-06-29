"""Set the project version in pyproject.toml.

Used by the release workflow so each job can sync pyproject.toml to the
target release version without duplicating the rewrite logic in YAML.

Usage:
    python scripts/set_pyproject_version.py 0.35.0
"""

from __future__ import annotations

import pathlib
import re
import sys


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: set_pyproject_version.py <version>")

    version = sys.argv[1]
    path = pathlib.Path("pyproject.toml")
    text = path.read_text()
    new_text, count = re.subn(
        r'^version = "[^"]+"',
        f'version = "{version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise SystemExit('Could not locate `version = "..."` in pyproject.toml')

    path.write_text(new_text)
    print(f"Set pyproject.toml version to {version}")


if __name__ == "__main__":
    main()
