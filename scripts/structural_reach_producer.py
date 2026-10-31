"""Write a structural_reach_input_file from a tree-sitter reverse-import graph.

An example producer for repositories without a build-graph tool. It reads the base commit with git, parses
Python, TypeScript and JavaScript imports, and follows importers from the changed files. Test files are left
out of reach and domains. Run it as a CI step before the review; it is not a pr-agent dependency:

    pip install "tree-sitter-language-pack==1.22.1"
    python scripts/structural_reach_producer.py --repo . --base "$BASE_SHA" --head "$HEAD_SHA" \\
        --unit module --out structural-reach.json
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import subprocess
import sys
from collections import Counter, deque

from tree_sitter_language_pack import get_parser

LANGUAGES = {
    ".py": "python",
    ".ts": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
}
TS_EXTENSIONS = (".ts", ".tsx", ".d.ts", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
PROJECT_MANIFESTS = {
    "project.json", "package.json", "pyproject.toml", "setup.py", "setup.cfg",
    "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "build.gradle.kts",
}
TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|e2e|e2e_tests|__mocks__)/"
    r"|(^|/)(test_[^/]*|[^/]*_test|conftest)\.py$"
    r"|\.(spec|test|e2e)\.[cm]?[jt]sx?$"
)
UNITS = {"file": "files", "module": "modules", "project": "projects"}
MAX_NAME_CHARS = 200
MAX_DEPENDENTS_LISTED = 1000

_parsers: dict = {}


def main(argv=None):
    args = parse_args(argv)
    tree = ls_tree(args.repo, args.base)
    changed = changed_paths(args.repo, args.base, args.head)
    graph = ImportGraph(args.repo, tree)
    text = json.dumps(structural_reach(graph, changed, args.unit, args.domain_depth), indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--unit", choices=tuple(UNITS), default="module")
    parser.add_argument("--domain-depth", type=int, default=1)
    parser.add_argument("--out")
    return parser.parse_args(argv)


def structural_reach(graph, changed, unit, domain_depth):
    affected = [path for path in graph.affected(changed) if language_of(path) and not is_test(path)]
    to_unit = graph.unit_of(unit)
    counts = Counter(to_unit(path) for path in affected)
    ranked = sorted(counts, key=lambda name: (-counts[name], name))
    domains = sorted({domain_of(path, domain_depth) for path in affected} - {""})
    return {
        "version": 1,
        "reach": len(ranked),
        "reach_unit": UNITS[unit],
        "dependents": [name for name in ranked if len(name) <= MAX_NAME_CHARS][:MAX_DEPENDENTS_LISTED],
        "domains": domains,
        "cross_domain": len(domains) > 1,
    }


def is_test(path):
    return bool(TEST_PATH.search(path))


def domain_of(path, depth):
    parts = path.split("/")[:-1]
    return "/".join(parts[:depth]) if parts else ""


class ImportGraph:
    def __init__(self, repo, tree, cache=None):
        self.repo = repo
        self.files = set(tree)
        self.cache = cache if cache is not None else {}
        self.py_roots = [""] + [root for root in ("src/", "lib/") if any(p.startswith(root) for p in self.files)]
        self.ts_paths = load_ts_paths(repo, tree)
        for pattern, targets in load_workspace_aliases(repo, tree).items():
            self.ts_paths.setdefault(pattern, []).extend(targets)
        self.reverse = self._build(tree)
        self._projects = sorted(
            {posixpath.dirname(p) for p in self.files if posixpath.basename(p) in PROJECT_MANIFESTS},
            key=len, reverse=True,
        )

    def affected(self, changed):
        seen = set(changed)
        queue = deque(changed)
        while queue:
            for importer in self.reverse.get(queue.popleft(), ()):
                if importer not in seen:
                    seen.add(importer)
                    queue.append(importer)
        return seen

    def unit_of(self, unit):
        if unit == "file":
            return lambda path: path
        if unit == "module":
            return lambda path: posixpath.dirname(path) or "."
        return self.project_of

    def project_of(self, path):
        for project in self._projects:
            if project == "" or path.startswith(project + "/"):
                return project or "."
        return "."

    def _build(self, tree):
        sources = {path: oid for path, oid in tree.items() if language_of(path)}
        missing = list({oid for path, oid in sources.items() if (oid, language_of(path)) not in self.cache})
        for oid, content in cat_blobs(self.repo, missing):
            self.cache[oid] = content
        reverse: dict[str, set] = {}
        for path, oid in sources.items():
            for target in self._resolve_all(path, self._imports(path, oid)):
                if target != path:
                    reverse.setdefault(target, set()).add(path)
        return reverse

    def _imports(self, path, oid):
        key = (oid, language_of(path))
        imports = self.cache.get(key)
        if imports is None:
            content = self.cache.get(oid)
            imports = extract_imports(content, key[1]) if isinstance(content, bytes) else []
            self.cache[key] = imports
            self.cache.pop(oid, None)
        return imports

    def _resolve_all(self, path, imports):
        resolve = self._resolve_py if path.endswith(".py") else self._resolve_ts
        for spec in imports:
            yield from resolve(path, spec)

    def _resolve_py(self, path, spec):
        level, module, names = spec
        if level:
            package = posixpath.dirname(path).split("/") if posixpath.dirname(path) else []
            package = package[: len(package) - (level - 1)] if level > 1 else package
            bases = ["/".join(package + module.split(".")) if module else "/".join(package)]
        else:
            bases = [root + module.replace(".", "/") for root in self.py_roots]
        for base in bases:
            base = base.strip("/")
            hits = [hit for name in names if (hit := self._py_file(f"{base}/{name}" if base else name))]
            if hits:
                yield from hits
                if len(hits) == len(names):
                    return
            parts = base.split("/")
            while parts:
                hit = self._py_file("/".join(parts))
                if hit:
                    yield hit
                    return
                if level:
                    break
                parts.pop()

    def _py_file(self, stem):
        for candidate in (f"{stem}.py", f"{stem}/__init__.py"):
            if candidate in self.files:
                return candidate
        return None

    def _resolve_ts(self, path, specifier):
        if specifier.startswith((".", "/")):
            targets = [posixpath.normpath(posixpath.join(posixpath.dirname(path), specifier)).lstrip("/")]
        else:
            targets = list(match_ts_paths(self.ts_paths, specifier))
        for target in targets:
            hit = self._ts_file(target)
            if hit:
                yield hit
                return

    def _ts_file(self, target):
        if target in self.files:
            return target
        stem = re.sub(r"\.[cm]?jsx?$", "", target)
        for base in dict.fromkeys((target, stem)):
            for ext in TS_EXTENSIONS:
                if base + ext in self.files:
                    return base + ext
            for ext in TS_EXTENSIONS:
                if f"{base}/index{ext}" in self.files:
                    return f"{base}/index{ext}"
        return None


def language_of(path):
    for ext, language in LANGUAGES.items():
        if path.endswith(ext):
            return language
    return None


def extract_imports(content, language):
    if language not in _parsers:
        _parsers[language] = get_parser(language)
    root = _parsers[language].parse(content).root_node
    return python_imports(root) if language == "python" else ts_imports(root)


def python_imports(root):
    out = []
    for node in walk(root, ("import_statement", "import_from_statement")):
        if node.type == "import_statement":
            for child in node.children_by_field_name("name"):
                out.append((0, _dotted(child), []))
            continue
        module_node = node.child_by_field_name("module_name")
        level, module = 0, ""
        if module_node is not None and module_node.type == "relative_import":
            for child in module_node.children:
                if child.type == "import_prefix":
                    level = len(text(child))
                elif child.type == "dotted_name":
                    module = text(child)
        elif module_node is not None:
            module = text(module_node)
        names = [_dotted(child).split(".")[0] for child in node.children_by_field_name("name")]
        out.append((level, module, names))
    return out


def _dotted(node):
    if node.type == "aliased_import":
        node = node.child_by_field_name("name")
    return text(node)


def ts_imports(root):
    out = []
    for node in walk(root, ("import_statement", "export_statement", "import_require_clause", "call_expression")):
        if node.type == "call_expression":
            fn = node.child_by_field_name("function")
            args = node.child_by_field_name("arguments")
            if fn is None or args is None or not (fn.type == "import" or text(fn) == "require"):
                continue
            source = next((c for c in args.named_children if c.type == "string"), None)
        else:
            source = node.child_by_field_name("source")
        if source is not None and source.type == "string":
            out.append(text(source)[1:-1])
    return out


def walk(root, types):
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type in types:
            yield node
        stack.extend(node.children)


def text(node):
    return node.text.decode("utf-8", "replace")


def load_ts_paths(repo, tree):
    paths = {}
    for name in ("tsconfig.json", "tsconfig.base.json"):
        oid = tree.get(name)
        if not oid:
            continue
        for _, content in cat_blobs(repo, [oid]):
            options = ts_compiler_options(name, content.decode("utf-8", "replace"))
            base_url = options.get("baseUrl", ".")
            for pattern, targets in (options.get("paths") or {}).items():
                paths[pattern] = [posixpath.normpath(posixpath.join(base_url, t)) for t in targets]
    return paths


def ts_compiler_options(name, raw):
    """The compilerOptions object of a tsconfig file, or {} with a warning when the file can't be used."""
    try:
        config = loads_jsonc(raw)
    except ValueError as error:
        warn(f"skipping {name}: not valid JSON ({error})")
        return {}
    if not isinstance(config, dict):
        warn(f"skipping {name}: the top level is not an object")
        return {}
    options = config.get("compilerOptions", {})
    if not isinstance(options, dict):
        warn(f"skipping {name}: compilerOptions is not an object")
        return {}
    if not isinstance(options.get("baseUrl", "."), str):
        warn(f"skipping {name}: compilerOptions.baseUrl is not a string")
        return {}
    paths = options.get("paths") or {}
    if not isinstance(paths, dict) or not all(
        isinstance(targets, list) and all(isinstance(target, str) for target in targets) for targets in paths.values()
    ):
        warn(f"skipping {name}: compilerOptions.paths is not an object of string arrays")
        return {}
    return options


def warn(message):
    print(f"warning: {message}", file=sys.stderr)


def load_workspace_aliases(repo, tree):
    manifests = {
        oid: path for path, oid in tree.items()
        if posixpath.basename(path) == "package.json" and path != "package.json" and "node_modules/" not in path
    }
    aliases: dict[str, list] = {}
    for oid, content in cat_blobs(repo, list(manifests)):
        try:
            manifest = json.loads(content)
        except ValueError:
            continue
        name = manifest.get("name") if isinstance(manifest, dict) else None
        if not isinstance(name, str) or not name:
            continue
        root = posixpath.dirname(manifests[oid])
        for subpath, targets in export_map(manifest).items():
            resolved = (posixpath.normpath(posixpath.join(root, t)) for t in targets)
            aliases.setdefault(name + subpath[1:], []).extend(resolved)
        aliases.setdefault(name, []).extend([f"{root}/src", root])
        aliases.setdefault(f"{name}/*", []).extend([f"{root}/src/*", f"{root}/*"])
    return aliases


def export_map(manifest):
    exports = manifest.get("exports")
    if not isinstance(exports, dict) or not any(key.startswith(".") for key in exports):
        exports = {".": exports} if exports else {}
    out = {key: list(_strings(value)) for key, value in exports.items() if key.startswith(".")}
    fields = ("source", "types", "typings", "module", "main")
    out.setdefault(".", []).extend(manifest[field] for field in fields if isinstance(manifest.get(field), str))
    return out


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def match_ts_paths(paths, specifier):
    for pattern, targets in paths.items():
        if "*" in pattern:
            prefix, suffix = pattern.split("*", 1)
            if specifier.startswith(prefix) and specifier.endswith(suffix) and len(specifier) >= len(prefix + suffix):
                star = specifier[len(prefix): len(specifier) - len(suffix)]
                yield from (t.replace("*", star) for t in targets)
        elif pattern == specifier:
            yield from targets


def loads_jsonc(raw):
    out, i, in_string = [], 0, False
    while i < len(raw):
        ch = raw[i]
        if in_string:
            out.append(ch)
            if ch == "\\":
                out.append(raw[i + 1: i + 2])
                i += 1
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
            out.append(ch)
        elif raw.startswith("//", i):
            i = raw.find("\n", i)
            i = len(raw) if i < 0 else i
            continue
        elif raw.startswith("/*", i):
            end = raw.find("*/", i + 2)
            if end < 0:
                raise ValueError("unterminated /* comment")
            i = end + 2
            continue
        else:
            out.append(ch)
        i += 1
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", "".join(out)))


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout


def ls_tree(repo, sha):
    tree = {}
    for line in git(repo, "ls-tree", "-r", "-z", sha).split("\0"):
        if not line:
            continue
        meta, path = line.split("\t", 1)
        _, kind, oid = meta.split()
        if kind == "blob":
            tree[path] = oid
    return tree


def changed_paths(repo, base, head):
    return [p for p in git(repo, "diff", "--name-only", "--no-renames", "-z", base, head).split("\0") if p]


def cat_blobs(repo, oids):
    if not oids:
        return
    proc = subprocess.run(
        ["git", "-C", repo, "cat-file", "--batch"],
        input="".join(f"{oid}\n" for oid in oids).encode(), capture_output=True, check=True,
    )
    data, pos = proc.stdout, 0
    for oid in oids:
        end = data.index(b"\n", pos)
        header = data[pos:end].split()
        if len(header) < 3:
            pos = end + 1
            continue
        size = int(header[2])
        yield oid, data[end + 1: end + 1 + size]
        pos = end + 1 + size + 1


if __name__ == "__main__":
    main()
