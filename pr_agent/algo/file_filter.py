import fnmatch
import re

from pr_agent.config_loader import get_settings
from pr_agent.log import get_logger


def filter_ignored(files, platform = 'github'):
    """
    Filter out files that match the ignore patterns.
    """

    try:
        # load regex patterns, and translate glob patterns to regex
        patterns = get_settings().ignore.regex
        if isinstance(patterns, str):
            patterns = [patterns]
        glob_setting = get_settings().ignore.glob
        if isinstance(glob_setting, str):  # --ignore.glob=[.*utils.py], --ignore.glob=.*utils.py
            glob_setting = glob_setting.strip('[]').split(",")
        patterns += translate_globs_to_regexes(glob_setting)

        code_generators = get_settings().config.get('ignore_language_framework', [])
        if isinstance(code_generators, str):
            get_logger().warning("'ignore_language_framework' should be a list. Skipping language framework filtering.")
            code_generators = []
        for cg in code_generators:
            glob_patterns = get_settings().generated_code.get(cg, [])
            if isinstance(glob_patterns, str):
                glob_patterns = [glob_patterns]
            patterns += translate_globs_to_regexes(glob_patterns)

        # compile all valid patterns
        compiled_patterns = []
        for r in patterns:
            try:
                compiled_patterns.append(re.compile(r))
            except re.error as e:
                get_logger().warning(
                    "Skipping invalid ignore pattern; files it was meant to exclude will be "
                    "sent to the model", artifact={"pattern": r, "error": str(e)})

        # keep filenames that _don't_ match the ignore regex
        if files and isinstance(files, list):
            for r in compiled_patterns:
                if platform == 'github':
                    files = [f for f in files if (f.filename and not r.match(f.filename))]
                elif platform == 'bitbucket':
                    # files = [f for f in files if (f.new.path and not r.match(f.new.path))]
                    files_o = []
                    for f in files:
                        if hasattr(f, 'new'):
                            if f.new and f.new.path and not r.match(f.new.path):
                                files_o.append(f)
                                continue
                        if hasattr(f, 'old'):
                            if f.old and f.old.path and not r.match(f.old.path):
                                files_o.append(f)
                                continue
                    files = files_o
                elif platform == 'bitbucket_server':
                    files = [f for f in files if f.get('path', {}).get('toString') and not r.match(f['path']['toString'])]
                elif platform == 'gitlab':
                    # files = [f for f in files if (f['new_path'] and not r.match(f['new_path']))]
                    files_o = []
                    for f in files:
                        if 'new_path' in f and f['new_path'] and not r.match(f['new_path']):
                            files_o.append(f)
                            continue
                        if 'old_path' in f and f['old_path'] and not r.match(f['old_path']):
                            files_o.append(f)
                            continue
                    files = files_o
                elif platform == 'azure':
                    files = [f for f in files if not r.match(f)]
                elif platform == 'gitea':
                    files = [f for f in files if not r.match(f.get("filename", ""))]
                elif platform == "gerrit":
                    files_o = []
                    for f in files:
                        path = f.b_path or f.a_path
                        if path and not r.match(path):
                            files_o.append(f)
                    files = files_o


    except Exception as e:
        print(f"Could not filter file list: {e}")

    return files

def translate_globs_to_regexes(globs: list):
    regexes = []
    for pattern in globs:
        regexes.append(fnmatch.translate(pattern))
        if pattern.startswith("**/"): # cover root-level files
            regexes.append(fnmatch.translate(pattern[3:]))
    return regexes
