#!/usr/bin/env python3
"""Apply a pull request's prompt-file changes onto the current checkout, so the eval can measure a contributor's
prompt change with the code and eval of the default branch. No code from the pull request is checked out.

A prompt file is data, but Dynaconf merges every table in it into the settings, so a changed file could also set an
endpoint or a key. A file is applied only when its tables and keys already exist in this checkout's copy of the same
file and every value is a string. Anything else in the pull request (code, other settings) is listed, not applied.

Usage: overlay_pr.py <pr_number> [--remote URL]. Prints a short markdown note for the run summary.
"""
import argparse
import pathlib
import re
import subprocess
import sys
import tomllib

REPO = pathlib.Path(__file__).resolve().parents[2]
SAFE_NAME = re.compile(r"[a-z_]+\.toml")
PLAIN_PATH = re.compile(r"[A-Za-z0-9_./-]{1,200}")


def is_prompt_file(path):
    """pr_agent/settings/[<dir>/]<name>_prompts[...].toml, lower-case names only."""
    parts = path.split("/")
    return (parts[:2] == ["pr_agent", "settings"] and len(parts) in (3, 4) and "_prompts" in parts[-1]
            and SAFE_NAME.fullmatch(parts[-1]) is not None and all(p.isidentifier() for p in parts[2:-1]))


def git(*args):
    return subprocess.run(["git", *args], cwd=REPO, check=True, capture_output=True, text=True).stdout


def same_shape(new, old):
    """True when new only rewrites string values of tables and keys that old already has. A string starting with "@"
    is refused: Dynaconf resolves @format, @jinja, @get and similar tokens against the environment, which holds the
    key."""
    if not isinstance(new, dict) or not set(new) <= set(old):
        return False
    for table, values in new.items():
        if not isinstance(values, dict) or not isinstance(old[table], dict) or not set(values) <= set(old[table]):
            return False
        if not all(isinstance(v, str) and not v.lstrip().startswith("@") for v in values.values()):
            return False
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pr", help="pull request number")
    ap.add_argument("--remote", default="origin")
    args = ap.parse_args()
    if not args.pr.isdigit():
        sys.exit("pull request number must be digits")
    ref = f"refs/remotes/review-eval/pr-{args.pr}"
    git("fetch", "-q", args.remote, f"+refs/pull/{args.pr}/head:{ref}")
    base = git("merge-base", "HEAD", ref).strip()
    changed = [p for p in git("diff", "--name-only", "-z", base, ref).split("\0") if p]
    applied, refused, skipped = [], [], []
    for path in changed:
        if not is_prompt_file(path) or not (REPO / path).is_file():
            skipped.append(path)
            continue
        try:
            text = git("show", f"{ref}:{path}")
            ok = same_shape(tomllib.loads(text), tomllib.loads((REPO / path).read_text()))
        except (subprocess.CalledProcessError, tomllib.TOMLDecodeError):
            ok = False
        if not ok:
            refused.append(path)
            continue
        (REPO / path).write_text(text)
        applied.append(path)
    lines = [f"Pull request #{args.pr}: applied {len(applied)} prompt file(s) onto the default branch."]
    lines += [f"- applied `{p}`" for p in applied]
    lines += [f"- refused `{p}` (adds tables or keys, a non-string value, or an @ token)" for p in refused]
    if skipped:
        shown = [p for p in skipped if PLAIN_PATH.fullmatch(p)][:10]  # paths come from the PR and go into a comment
        lines.append(f"- not applied, {len(skipped)} other changed file(s) (code is never run from a pull request; "
                     "run the eval locally on the branch to include them)"
                     + (": " + ", ".join(f"`{p}`" for p in shown) if shown else "")
                     + (" ..." if len(skipped) > len(shown) else ""))
    print("\n".join(lines))
    if refused or not applied:
        sys.exit(2)


if __name__ == "__main__":
    main()
