#!/usr/bin/env python3
"""Build a diff-scoped review-context bundle (G5 recipe) for one PR, on tree-sitter instead of Python's ast.

Same sections, caps and ranking as graph_bundle_v5.py: changed symbols as signature + body lines the diff does not
show, module-level constants they read, 1-hop callees, 1-hop inbound callers (hubs ranked by changed-argument use,
then return-value use), sibling implementations, sibling config/prompt/doc sections, sites still using shapes the
PR removed, similar code, same-pattern sites, tests that exercise the change, importers and a circular-import check.

Language-neutral core over a small IR built once per file from the tree-sitter tree; each language supplies a Lang
class (definitions, references, calls, imports, parameters, bindings, builtins). Python and JavaScript are
implemented. The retrieval scope (similar code, same-pattern sites, cycles) defaults to the top-level directories
of the changed non-test code files instead of a hard-coded package. Never imports or executes code from the target
repo: files are parsed with tree-sitter and read via git.

Usage: graph_bundle_ts.py --repo ROOT --base BASE_SHA --head HEAD_SHA --pr N --out-dir DIR [--name NAME]
       [--scope DIR ...]
"""
import argparse
import bisect
import difflib
import fnmatch
import heapq
import json
import os
import posixpath
import re
import signal
import subprocess
import sys

from tree_sitter import Query, QueryCursor
from tree_sitter_language_pack import (
    PackConfig,
    configure,
    detect_language_from_extension,
    get_language,
    get_parser,
    get_tags_query,
)

sys.setrecursionlimit(20000)

TOKEN_CAP = 12000
CHAR_CAP = min(TOKEN_CAP * 4, 50000 - 500)
CHANGED_SHARE = 0.15
SUPPORT_SHARE = 0.20
RETRIEVAL_SHARE = 0.35
CALLER_SHARE = 0.20
EXTRA_SHARE = 0.10
DIFF_BEFORE, DIFF_AFTER = 8, 4
CHANGED_WINDOWS = (20, 8, 0)
CALLER_WINDOW = 8
CALLER_FULL_LINES = 30
SIBLINGS_PER_SYMBOL = 8
SIBLING_MAX_LINES = 36
REMOVED_SHAPES = 6
REMOVED_SITES = 8
NONCODE_SIBLINGS = 5
NONCODE_MAX_LINES = 40
NONCODE_PROBES = 40
TESTS_PER_SYMBOL = 15
IMPORTERS_LISTED = 30
NONCODE_SUFFIXES = (".toml", ".yaml", ".yml", ".md")
GENERIC_BASES = {"ABC", "ABCMeta", "Protocol", "Generic", "Object", "EventEmitter"}
CALLER_MAX_LINES = 60
CALLEE_MAX_LINES = 10
CALLEE_FULL_LINES = 60
CONST_MAX_LINES = 40
SIMILAR_MAX_LINES = 40
SIMILAR_FULL_RATIO = 0.75
SIMILAR_PER_FUNC = 3
HUB_THRESHOLD = 25
HUB_KEEP = 10
HUB_FILES_LISTED = 25
PATTERN_MIN_SIZE = 4
PATTERN_MAX_SIZE = 30
PATTERN_MAX_MATCHES = 60
PATTERNS_PER_FUNC = 3
PATTERN_SITES = 8
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "site-packages", ".tox", "build", "dist"}
DOTTED = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+$")
PUNCT = set("()[]{},;:.")


# Untrusted input: the head commit's tree is read as git blobs only, never from a working tree, so a symlink,
# submodule or untracked file cannot reach the bundle; oversized and secret-looking files are left out too.
MAX_FILE_BYTES = 1_000_000
DEFAULT_TIMEOUT_S = 120
SECRET_NAMES = (".env", ".env.*", "*.env", ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".secrets.toml",
                "secrets.toml", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore", "id_rsa*", "id_dsa*",
                "id_ecdsa*", "id_ed25519*", "*.tfstate", "*.tfstate.*", "credentials", "credentials.*")
GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1"}


def git_cmd(repo, *args):
    # No fsmonitor or hook programs, and no protocol, so a partial clone cannot fetch from the network.
    return ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
            "-C", repo, *args]


def git(repo, *args):
    return subprocess.run(git_cmd(repo, *args), check=True, capture_output=True, text=True, env=GIT_ENV).stdout


def is_secret_path(path):
    name = posixpath.basename(path).lower()
    return any(fnmatch.fnmatchcase(name, pat) for pat in SECRET_NAMES)


class HeadTree:
    """Regular files tracked at the head commit, read through one `git cat-file --batch` process."""

    def __init__(self, repo, head):
        self.repo, self.blobs, self.dirs, self._cat = repo, {}, {}, None
        out = subprocess.run(git_cmd(repo, "ls-tree", "-r", "-l", "-z", "--full-tree", head), check=True,
                             capture_output=True, env=GIT_ENV).stdout
        for entry in out.split(b"\0"):
            if not entry:
                continue
            meta, raw = entry.split(b"\t", 1)
            mode, kind, sha, size = meta.split()
            path = raw.decode("utf-8", "surrogateescape")
            if (kind == b"blob" and mode in (b"100644", b"100755") and int(size) <= MAX_FILE_BYTES
                    and not is_secret_path(path)):
                self.blobs[path] = sha.decode()
                self.dirs.setdefault(posixpath.dirname(path), []).append(posixpath.basename(path))

    def is_file(self, path):
        return path in self.blobs

    def listdir(self, folder):
        return list(self.dirs.get(folder, []))

    def read_text(self, path):
        sha = self.blobs.get(path)
        if sha is None:
            return None
        if self._cat is None:
            self._cat = subprocess.Popen(git_cmd(self.repo, "cat-file", "--batch"), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, env=GIT_ENV)
        self._cat.stdin.write(sha.encode() + b"\n")
        self._cat.stdin.flush()
        header = self._cat.stdout.readline().split()
        if len(header) != 3:
            return None
        data = self._cat.stdout.read(int(header[2]))
        self._cat.stdout.read(1)
        try:
            # Same newline handling as open() in text mode, so line numbers match the diff.
            return data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            return None

    def close(self):
        if self._cat is not None:
            self._cat.stdin.close()
            self._cat.wait()
            self._cat = None


HEAD = None  # the HeadTree of the commit being reviewed, set by build()


TEST_FILE = re.compile(r"(^test_.*|.*(_test|\.test|\.spec|_spec)\.\w+)$")


def is_test_path(path):
    parts = path.split("/")
    return parts[0] in ("tests", "test", "__tests__") or any(p in ("tests", "__tests__") for p in parts[:-1]) \
        or bool(TEST_FILE.match(parts[-1]))


def tok(s):
    return len(s) // 4


# ---------------------------------------------------------------- IR

class N:
    """One tree-sitter node, materialised once: type, field name in the parent, 1-based line range, children."""
    __slots__ = ("type", "named", "field", "children", "parent", "sl", "el", "sc", "sb", "eb", "src", "tag")

    def __init__(self, ts, field, parent, src, starts):
        # line/column come from byte offsets: tree-sitter 0.26's Point objects corrupt memory when held (SIGBUS in gc)
        self.type, self.named, self.field, self.parent, self.src = ts.type, ts.is_named, field, parent, src
        self.sb, self.eb = ts.start_byte, ts.end_byte
        row = bisect.bisect_right(starts, self.sb) - 1
        self.sl, self.sc = row + 1, self.sb - starts[row]
        erow = bisect.bisect_right(starts, self.eb) - 1
        self.el = max(self.sl, erow + 1 if self.eb > starts[erow] else erow)
        self.children, self.tag = [], None

    @property
    def text(self):
        return self.src[self.sb:self.eb].decode("utf-8", "replace")

    def field_child(self, name):
        return next((c for c in self.children if c.field == name), None)

    def field_children(self, name):
        return [c for c in self.children if c.field == name]

    @property
    def named_children(self):
        return [c for c in self.children if c.named]


def to_ir(tree, src, drop):
    cur = tree.walk()
    starts = [0] + [m.end() for m in re.finditer(b"\n", src)]

    def build(parent):
        n = N(cur.node, cur.field_name, parent, src, starts)
        if cur.goto_first_child():
            while True:
                if cur.node.type not in drop:
                    n.children.append(build(n))
                if not cur.goto_next_sibling():
                    break
            cur.goto_parent()
        return n

    return build(None)


def walk(n):
    stack = [n]
    while stack:
        x = stack.pop()
        yield x
        stack.extend(reversed(x.children))


# ---------------------------------------------------------------- languages

class Lang:
    name = fence = ""
    suffixes = ()
    drop = {"comment"}
    ident = {"identifier"}
    self_names = set()
    attr_types = {}  # type -> (object field, name field)
    call_types = {}  # type -> function field
    string_types = set()
    number_types = set()
    literal_kw = {}
    pattern_types = set()
    transparent = set()
    await_types = set()
    attr_funcs = set()
    init_name = ""
    builtins = set()
    common = set()
    keywords = set()

    _parser = None

    @property
    def parser(self):
        if self._parser is None:
            self._parser = get_parser(self.name)
        return self._parser

    def parse(self, text):
        """(IR root, has syntax errors)"""
        src = text.encode("utf-8")
        tree = self.parser.parse(src)
        return to_ir(tree, src, self.drop), tree.root_node.has_error

    # literals
    def string_value(self, n):
        """Value of a constant string literal, else None (interpolated strings are not constants)."""
        return None

    def literal_token(self, n):
        if n.type in self.string_types:
            return "S"
        if n.type in self.number_types:
            return "N"
        return self.literal_kw.get(n.type)

    def is_constant(self, n):
        return n.type in self.number_types or n.type in self.literal_kw or self.string_value(n) is not None

    # attributes and calls
    def attr_parts(self, n):
        f = self.attr_types.get(n.type)
        return (n.field_child(f[0]), n.field_child(f[1])) if f else (None, None)

    def callee_name(self, call):
        f = call.field_child(self.call_types[call.type])
        if f is None:
            return None, None
        if f.type in self.ident:
            return None, f.text
        obj, nm = self.attr_parts(f)
        if nm is not None:
            if obj is not None and (obj.type in self.ident or obj.type in self.self_types()):
                recv = "self" if obj.text in self.self_names or obj.type in self.self_types() else obj.text
            else:
                recv = "?"
            return recv, nm.text
        return None, None

    def self_types(self):
        return set()


class Python(Lang):
    name, fence, suffixes = "python", "python", (".py",)
    self_names = {"self", "cls"}
    attr_types = {"attribute": ("object", "attribute")}
    call_types = {"call": "function"}
    string_types = {"string", "concatenated_string"}
    number_types = {"integer", "float"}
    literal_kw = {"true": "True", "false": "False", "none": "None", "ellipsis": "Ellipsis"}
    pattern_types = {"call", "boolean_operator", "comparison_operator", "subscript", "conditional_expression"}
    transparent = {"argument_list", "parenthesized_expression", "expression_statement"}
    await_types = {"await", "parenthesized_expression"}
    attr_funcs = {"getattr", "hasattr", "setattr", "delattr"}
    init_name = "__init__"
    builtins = set(dir(__import__("builtins")))
    common = set(dir(dict)) | set(dir(list)) | set(dir(str)) | set(dir(bytes)) | set(dir(set)) | {
        "get", "load", "add", "debug", "info", "warning", "error", "exception", "critical", "update", "close", "read",
        "write", "open", "format", "join", "split", "copy", "run", "main", "setup", "start", "stop", "send", "parse"}
    keywords = set(__import__("keyword").kwlist) | {"self", "cls", "None", "True", "False"}
    PARAM_CONTAINERS = {"parameters", "lambda_parameters"}

    def string_value(self, n):
        if n.type == "concatenated_string":
            parts = [self.string_value(c) for c in n.named_children]
            return None if any(p is None for p in parts) else "".join(parts)
        if n.type != "string" or any(c.type == "interpolation" for c in n.children):
            return None
        return "".join(c.text for c in n.children if c.type in ("string_content", "escape_sequence"))

    def defs(self, root):
        """(node, kind hint, name, start line) per function/class definition, outermost first, in source order."""
        out = []
        for n in walk(root):
            if n.type in ("function_definition", "class_definition"):
                nm = n.field_child("name")
                start = n.parent.sl if n.parent is not None and n.parent.type == "decorated_definition" else n.sl
                out.append((n, "class" if n.type == "class_definition" else "function", nm.text, start))
        return out

    def owner_node(self, n):
        """The node that switches ownership to a def when entered (decorators belong to their def)."""
        if n.type == "decorated_definition":
            return n.field_child("definition")
        return n

    def body(self, d):
        return d.field_child("body")

    def body_line(self, d):
        b = self.body(d)
        kids = b.named_children if b is not None else []
        return kids[0].sl if kids else (b.sl if b is not None else d.el)

    def docstring_end(self, d):
        b = self.body(d)
        kids = b.named_children if b is not None else []
        if not kids:
            return None
        k = kids[0]
        if k.type == "expression_statement" and len(k.named_children) == 1:
            k = k.named_children[0]
        return k.el if k.type in self.string_types and self.string_value(k) is not None else None

    def is_ref(self, n):
        """Identifier in an expression position (ast.Name or ast.Attribute.attr), not a def/param/keyword name."""
        if n.type not in self.ident:
            return False
        p = n.parent
        if p is None:
            return False
        if p.type in ("function_definition", "class_definition") and n.field == "name":
            return False
        if p.type == "keyword_argument" and n.field == "name":
            return False
        if p.type in ("import_statement", "import_from_statement", "aliased_import", "dotted_name",
                      "global_statement", "nonlocal_statement", "relative_import"):
            return False
        x = p
        while x is not None and x.type not in ("function_definition", "class_definition", "module", "block"):
            if x.type in self.PARAM_CONTAINERS:
                return self._under_default(n, x)
            x = x.parent
        return True

    @staticmethod
    def _under_default(n, container):
        x = n
        while x is not None and x is not container:
            if x.field in ("value", "type"):
                return True
            x = x.parent
        return False

    def call_args(self, call):
        """(positional nodes, [(keyword name or None, value)], has_star)"""
        args = call.field_child("arguments")
        pos, kws, star = [], [], False
        if args is None:
            return pos, kws, star
        if args.type != "argument_list":
            return [args], kws, star
        for a in args.named_children:
            if a.type == "keyword_argument":
                kws.append((a.field_child("name").text, a.field_child("value")))
            elif a.type == "dictionary_splat":
                kws.append((None, a.named_children[0] if a.named_children else a))
            elif a.type == "list_splat":
                star = True
                pos.append(a)
            else:
                pos.append(a)
        return pos, kws, star

    def string_refs(self, call):
        _recv, nm = self.callee_name(call)
        if nm in self.attr_funcs and _recv is None:
            pos, _k, _s = self.call_args(call)
            if len(pos) >= 2:
                v = self.string_value(pos[1])
                if v is not None:
                    return [v]
        return []

    def top_statements(self, root, skip_type_checking=False):
        out = []
        for n in root.named_children:
            if n.type == "if_statement":
                cond = n.field_child("condition")
                if skip_type_checking and cond is not None and cond.text.split(".")[-1] == "TYPE_CHECKING":
                    continue
                out.extend(self._blocks(n))
            elif n.type == "try_statement":
                out.extend(self._blocks(n))
            else:
                out.append(n)
        return out

    def _blocks(self, n):
        out = []
        for c in n.named_children:
            if c.type == "block":
                out.extend(c.named_children)
            elif c.type in ("else_clause", "elif_clause", "except_clause", "finally_clause"):
                for b in c.named_children:
                    if b.type == "block":
                        out.extend(b.named_children)
        return out

    def module_bindings(self, root):
        consts, names = {}, set()
        stmts = list(root.named_children) + [s for n in root.named_children
                                             if n.type in ("if_statement", "try_statement") for s in self._blocks(n)]
        for n in stmts:
            inner = self.owner_node(n)
            if inner.type in ("function_definition", "class_definition"):
                names.add(inner.field_child("name").text)
            elif n.type in ("import_statement", "import_from_statement"):
                for nm in n.field_children("name"):
                    if nm.type == "aliased_import":
                        names.add(nm.field_child("alias").text)
                    else:
                        names.add(nm.text.split(".")[0])
            elif n.type == "assignment" or (n.type == "expression_statement" and n.named_children
                                             and n.named_children[0].type == "assignment"):
                a = n if n.type == "assignment" else n.named_children[0]
                left, right = a.field_child("left"), a.field_child("right")
                for x in walk(left):
                    if x.type in self.ident and self.is_ref(x) and not (x.parent.type == "attribute"
                                                                        and x.field == "attribute"):
                        names.add(x.text)
                        if not x.text.startswith("__") and right is not None:
                            consts.setdefault(x.text, (n, right.type == "call"))
        return consts, names

    def module_of(self, rel):
        mod = rel[:-3].replace("/", ".")
        return mod.removesuffix(".__init__")

    def _from_module(self, rel, n):
        pkg = rel.rsplit("/", 1)[0].replace("/", ".") if "/" in rel else ""
        m = n.field_child("module_name")
        if m is None:
            return ""
        if m.type == "relative_import":
            prefix = next((c for c in m.children if c.type == "import_prefix"), None)
            level = prefix.text.count(".") if prefix is not None else 0
            dn = next((c for c in m.named_children if c.type == "dotted_name"), None)
            mod = dn.text if dn is not None else ""
            parts = pkg.split(".") if pkg else []
            parts = parts[:len(parts) - (level - 1)] if level > 1 else parts
            return ".".join(parts + ([mod] if mod else []))
        return m.text

    def _module_file(self, repo, dotted):
        base = dotted.replace(".", "/")
        for cand in (base + ".py", base + "/__init__.py"):
            if cand in repo.trees or HEAD.is_file(cand):
                return cand
        return None

    def import_map(self, repo, rel, root):
        out = {}
        for n in walk(root):
            if n.type == "import_statement":
                for nm in n.field_children("name"):
                    if nm.type == "aliased_import":
                        f = repo.module_file(self, nm.field_child("name").text)
                        if f:
                            out[nm.field_child("alias").text] = ("module", f)
            elif n.type == "import_from_statement":
                mod = self._from_module(rel, n)
                for nm in n.field_children("name"):
                    name = nm.field_child("name").text if nm.type == "aliased_import" else nm.text
                    local = nm.field_child("alias").text if nm.type == "aliased_import" else nm.text
                    sub = repo.module_file(self, f"{mod}.{name}") if mod else None
                    if sub:
                        out[local] = ("module", sub)
                        continue
                    f = repo.module_file(self, mod) if mod else None
                    if f:
                        out[local] = ("symbol", f, name)
        return out

    def import_edges(self, repo, rel, root, top_only):
        stmts = self.top_statements(root, skip_type_checking=True) if top_only else list(walk(root))
        out = []
        for n in stmts:
            if n.type == "import_statement":
                for nm in n.field_children("name"):
                    dotted = nm.field_child("name").text if nm.type == "aliased_import" else nm.text
                    f = repo.module_file(self, dotted)
                    if f:
                        out.append((f, n.sl, n.el))
            elif n.type == "import_from_statement":
                mod = self._from_module(rel, n)
                for nm in n.field_children("name"):
                    name = nm.field_child("name").text if nm.type == "aliased_import" else nm.text
                    f = (repo.module_file(self, f"{mod}.{name}") or repo.module_file(self, mod)) if mod else None
                    if f:
                        out.append((f, n.sl, n.el))
        return out

    def import_lines(self, root):
        return [(n.sl, n.el) for n in root.named_children if n.type in ("import_statement", "import_from_statement")]

    def params(self, d):
        ps = d.field_child("parameters")
        pos, other, kwonly = [], [], False
        for p in (ps.named_children if ps is not None else []):
            if p.type == "identifier":
                nm = p.text
            elif p.type in ("typed_parameter", "list_splat_pattern", "dictionary_splat_pattern"):
                ident = next((c for c in p.named_children if c.type == "identifier"), None)
                nm = ident.text if ident is not None else None
                if p.type == "typed_parameter" and p.named_children and p.named_children[0].type in (
                        "list_splat_pattern", "dictionary_splat_pattern"):
                    inner = p.named_children[0]
                    nm = inner.named_children[0].text if inner.named_children else None
                    other.append(nm)
                    kwonly = kwonly or inner.type == "list_splat_pattern"
                    continue
                if p.type != "typed_parameter":
                    other.append(nm)
                    kwonly = kwonly or p.type == "list_splat_pattern"
                    continue
            elif p.type in ("default_parameter", "typed_default_parameter"):
                nm = p.field_child("name").text
            elif p.type == "keyword_separator":
                kwonly = True
                continue
            else:
                continue
            if nm is None or nm in self.self_names:
                continue
            (other if kwonly else pos).append(nm)
        return pos, pos + other

    def bases(self, cls):
        sc = cls.field_child("superclasses")
        out = []
        for b in (sc.named_children if sc is not None else []):
            if b.type == "identifier":
                out.append(b.text)
            elif b.type == "attribute":
                out.append(b.field_child("attribute").text)
        return out

    STORE_CONTAINERS = {"pattern_list", "tuple_pattern", "list_pattern", "list_splat_pattern", "tuple", "list"}

    def _store_names(self, n, out):
        if n is None:
            return
        if n.type in self.ident:
            out.add(n.text)
        elif n.type in self.STORE_CONTAINERS or n.type == "parenthesized_expression":
            for c in n.named_children:
                self._store_names(c, out)

    def local_bindings(self, d):
        out, glob = set(), set()
        for n in walk(d):
            t = n.type
            if t in ("assignment", "augmented_assignment"):
                self._store_names(n.field_child("left"), out)
            elif t in ("for_statement", "for_in_clause"):
                self._store_names(n.field_child("left"), out)
            elif t == "named_expression":
                self._store_names(n.field_child("name"), out)
            elif t == "as_pattern":
                al = n.field_child("alias")
                if al is not None:
                    for x in walk(al):
                        if x.type in self.ident:
                            out.add(x.text)
            elif t == "delete_statement":
                for c in n.named_children:
                    self._store_names(c, out)
            elif t in self.PARAM_CONTAINERS:
                for p in n.named_children:
                    if p.type == "identifier":
                        out.add(p.text)
                    else:
                        nm = p.field_child("name")
                        if nm is not None and nm.type in self.ident:
                            out.add(nm.text)
                        for c in p.named_children:
                            if c.type in self.ident and c.field is None:
                                out.add(c.text)
                            elif c.type in ("list_splat_pattern", "dictionary_splat_pattern") and c.named_children:
                                out.add(c.named_children[0].text)
            elif t in ("global_statement", "nonlocal_statement"):
                glob.update(c.text for c in n.named_children if c.type in self.ident)
        return out - glob

    def test_child(self, p, node):
        """True when node is the condition tested by p."""
        if p.type in ("if_statement", "elif_clause", "while_statement"):
            return p.field_child("condition") is node
        if p.type == "conditional_expression":
            kids = p.named_children
            return len(kids) >= 2 and kids[1] is node
        if p.type == "assert_statement":
            return bool(p.named_children) and p.named_children[0] is node
        return False

    def is_boolop(self, p):
        return p.type in ("boolean_operator", "not_operator")

    def subscript_value(self, p):
        return p.field_child("value") if p.type == "subscript" else None

    def assigned_names(self, p, node):
        if p.type == "assignment" and p.field_child("right") is node:
            left = p.field_child("left")
            return [left.text] if left is not None and left.type in self.ident else [], False
        if p.type == "named_expression" and p.field_child("value") is node:
            return [p.field_child("name").text], True
        return [], False

    def keyword_name_position(self, n):
        return n.parent is not None and n.parent.type == "keyword_argument" and n.field == "name"

    def module_path_strings(self, rel):
        mod = self.module_of(rel)
        return [f'"{mod}"', f"'{mod}'"]


class JavaScript(Lang):
    name, fence, suffixes = "javascript", "javascript", (".js", ".mjs", ".cjs", ".jsx")
    ident = {"identifier", "property_identifier", "shorthand_property_identifier"}
    self_names = {"this"}
    attr_types = {"member_expression": ("object", "property")}
    call_types = {"call_expression": "function", "new_expression": "constructor"}
    string_types = {"string", "template_string"}
    number_types = {"number"}
    literal_kw = {"true": "true", "false": "false", "null": "null", "undefined": "undefined"}
    pattern_types = {"call_expression", "binary_expression", "subscript_expression", "ternary_expression"}
    transparent = {"arguments", "parenthesized_expression", "expression_statement"}
    await_types = {"await_expression", "parenthesized_expression"}
    init_name = "constructor"
    builtins = {"console", "Object", "Array", "JSON", "Math", "Promise", "process", "require", "module", "exports",
                "window", "document", "setTimeout", "clearTimeout", "setInterval", "clearInterval", "Error",
                "TypeError", "String", "Number", "Boolean", "Date", "RegExp", "Map", "Set", "WeakMap", "Symbol",
                "Buffer", "globalThis", "navigator", "fetch", "URL", "parseInt", "parseFloat", "isNaN", "__dirname",
                "__filename", "undefined", "NaN", "Infinity", "Reflect", "Proxy", "BigInt", "queueMicrotask"}
    common = {"push", "pop", "shift", "unshift", "map", "filter", "forEach", "reduce", "find", "some", "every",
              "includes", "indexOf", "slice", "splice", "concat", "join", "split", "replace", "trim", "toString",
              "then", "catch", "finally", "log", "warn", "error", "info", "debug", "get", "set", "has", "delete",
              "keys", "values", "entries", "assign", "on", "once", "off", "emit", "send", "close", "open", "start",
              "stop", "run", "init", "apply", "call", "bind", "constructor", "length", "add", "remove", "clear",
              "parse", "stringify", "resolve", "reject", "exec", "test", "match", "load", "read", "write",
              "addEventListener", "removeEventListener", "invoke", "handle", "main", "setup", "handler", "callback",
              "listener", "cb", "fn", "done", "next"}
    TEST_CALLS = {"it", "test"}
    keywords = {"break", "case", "catch", "class", "const", "continue", "debugger", "default", "delete", "do",
                "else", "export", "extends", "finally", "for", "function", "if", "import", "in", "instanceof",
                "let", "new", "return", "super", "switch", "this", "throw", "try", "typeof", "var", "void", "while",
                "with", "yield", "async", "await", "null", "true", "false", "undefined", "of"}
    FUNC_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}

    def self_types(self):
        return {"this"}

    def string_value(self, n):
        if n.type == "string":
            return "".join(c.text for c in n.children if c.type in ("string_fragment", "escape_sequence"))
        if n.type == "template_string" and not any(c.type == "template_substitution" for c in n.children):
            return "".join(c.text for c in n.children if c.type in ("string_fragment", "escape_sequence"))
        return None

    def defs(self, root):
        out = []
        for n in walk(root):
            if n.type in ("function_declaration", "generator_function_declaration"):
                out.append((n, "function", n.field_child("name").text, self._start(n)))
            elif n.type == "class_declaration":
                out.append((n, "class", n.field_child("name").text, self._start(n)))
            elif n.type == "method_definition":
                out.append((n, "function", n.field_child("name").text, n.sl))
            elif n.type == "call_expression" and n.field_child("function") is not None \
                    and n.field_child("function").text in self.TEST_CALLS:
                # jest/mocha `it("does x", () => {...})`: the callback is the test function
                args = n.field_child("arguments")
                kids = args.named_children if args is not None else []
                fn = next((a for a in kids if a.type in self.FUNC_VALUES), None)
                title = self.string_value(kids[0]) if kids else None
                if fn is not None:
                    out.append((fn, "function", f"test {title or n.sl}"[:80], n.sl))
            elif n.type == "variable_declarator":
                v, nm = n.field_child("value"), n.field_child("name")
                if v is not None and nm is not None and nm.type == "identifier" and v.type in self.FUNC_VALUES:
                    out.append((n, "function", nm.text, self._start(n)))
                elif v is not None and nm is not None and nm.type == "identifier" and v.type == "class":
                    out.append((n, "class", nm.text, self._start(n)))
        return out

    @staticmethod
    def _start(n):
        x = n
        while x.parent is not None and x.parent.type in ("lexical_declaration", "variable_declaration",
                                                         "export_statement"):
            x = x.parent
        return x.sl

    def owner_node(self, n):
        return n

    def _fn(self, d):
        if d.type == "variable_declarator":
            return d.field_child("value")
        return d

    def body(self, d):
        return self._fn(d).field_child("body")

    def body_line(self, d):
        b = self.body(d)
        if b is None:
            return d.el
        if b.type in ("statement_block", "class_body"):
            kids = b.named_children
            return kids[0].sl if kids else b.sl
        return b.sl

    def docstring_end(self, d):
        return None

    def is_ref(self, n):
        if n.type not in self.ident:
            return False
        p = n.parent
        if p is None:
            return False
        if n.field == "name" and p.type in ("function_declaration", "generator_function_declaration",
                                            "class_declaration", "method_definition", "class"):
            return False
        if n.type == "property_identifier" and not (p.type == "member_expression" and n.field == "property"):
            return False  # object keys, method names
        if p.type in ("import_specifier", "namespace_import", "import_clause", "export_specifier"):
            return False
        if p.type == "variable_declarator" and n.field == "name":
            return False
        x, prev = p, n
        while x is not None and x.type not in ("statement_block", "program", "class_body"):
            if x.type == "formal_parameters":
                return prev.type == "assignment_pattern" and self._under(n, prev, "right")
            prev, x = x, x.parent
        return True

    @staticmethod
    def _under(n, top, field):
        x = n
        while x is not None and x is not top:
            if x.parent is top and x.field == field:
                return True
            x = x.parent
        return False

    def call_args(self, call):
        args = call.field_child("arguments")
        pos, kws, star = [], [], False
        for a in (args.named_children if args is not None and args.type == "arguments" else []):
            if a.type == "spread_element":
                star = True
                pos.append(a)
            elif a.type == "object":  # options object: its keys act as keyword arguments
                pos.append(a)
                for p in a.named_children:
                    if p.type == "pair":
                        k = p.field_child("key")
                        kws.append((k.text.strip("'\""), p.field_child("value")))
                    elif p.type == "shorthand_property_identifier":
                        kws.append((p.text, p))
                    elif p.type == "spread_element":
                        kws.append((None, p))
            else:
                pos.append(a)
        return pos, kws, star

    def string_refs(self, call):
        return []

    def _decls(self, n):
        if n.type == "export_statement":
            d = n.field_child("declaration")
            return [d] if d is not None else []
        return [n]

    def top_statements(self, root, skip_type_checking=False):
        return [d for n in root.named_children for d in self._decls(n)]

    def module_bindings(self, root):
        consts, names = {}, set()
        for n in self.top_statements(root):
            if n.type in ("function_declaration", "generator_function_declaration", "class_declaration"):
                names.add(n.field_child("name").text)
            elif n.type == "import_statement":
                for x in walk(n):
                    if x.type == "identifier":
                        names.add(x.text)
            elif n.type in ("lexical_declaration", "variable_declaration"):
                for dcl in n.named_children:
                    if dcl.type != "variable_declarator":
                        continue
                    nm, v = dcl.field_child("name"), dcl.field_child("value")
                    for x in walk(nm):
                        if x.type in ("identifier", "shorthand_property_identifier_pattern"):
                            names.add(x.text)
                    if nm.type == "identifier" and v is not None and v.type not in self.FUNC_VALUES \
                            and not self._is_require(v) and not nm.text.startswith("__"):
                        consts.setdefault(nm.text, (n, v.type in self.call_types))
        return consts, names

    def _is_require(self, v):
        if v is None or v.type != "call_expression":
            return False
        f = v.field_child("function")
        return f is not None and f.type == "identifier" and f.text == "require"

    def _spec(self, call):
        args = call.field_child("arguments")
        a = args.named_children[0] if args is not None and args.named_children else None
        return self.string_value(a) if a is not None else None

    def module_of(self, rel):
        return os.path.splitext(rel)[0]

    def _module_file(self, repo, spec_and_dir):
        spec, here = spec_and_dir.split("\0")
        if not spec.startswith("."):
            return None
        base = os.path.normpath(os.path.join(here, spec)).replace(os.sep, "/")
        for cand in [base] + [base + s for s in self.suffixes] + [f"{base}/index{s}" for s in self.suffixes]:
            if cand in repo.trees or (os.path.splitext(cand)[1] in self.suffixes
                                      and HEAD.is_file(cand)):
                return cand
        return None

    def _resolve(self, repo, rel, spec):
        return repo.module_file(self, f"{spec}\0{os.path.dirname(rel)}") if spec else None

    def import_map(self, repo, rel, root):
        out = {}
        for n in walk(root):
            if n.type == "import_statement":
                f = self._resolve(repo, rel, self.string_value(n.field_child("source")))
                if not f:
                    continue
                for x in walk(n):
                    if x.type == "import_specifier":
                        nm, al = x.field_child("name"), x.field_child("alias")
                        out[(al or nm).text] = ("symbol", f, nm.text)
                    elif x.type == "namespace_import" or (x.type == "identifier" and x.parent.type == "import_clause"):
                        ident = x if x.type == "identifier" else next(c for c in x.named_children)
                        out[ident.text] = ("module", f)
            elif n.type == "variable_declarator" and self._is_require(n.field_child("value")):
                f = self._resolve(repo, rel, self._spec(n.field_child("value")))
                nm = n.field_child("name")
                if not f or nm is None:
                    continue
                if nm.type == "identifier":
                    out[nm.text] = ("module", f)
                elif nm.type == "object_pattern":
                    for p in nm.named_children:
                        if p.type == "shorthand_property_identifier_pattern":
                            out[p.text] = ("symbol", f, p.text)
                        elif p.type == "pair_pattern":
                            out[p.field_child("value").text] = ("symbol", f, p.field_child("key").text)
        return out

    def import_edges(self, repo, rel, root, top_only):
        out = []
        nodes = self.top_statements(root) if top_only else list(walk(root))
        for n in nodes:
            if n.type == "import_statement":
                f = self._resolve(repo, rel, self.string_value(n.field_child("source")))
                if f:
                    out.append((f, n.sl, n.el))
            elif top_only and n.type in ("lexical_declaration", "variable_declaration"):
                for dcl in n.named_children:
                    if dcl.type == "variable_declarator" and self._is_require(dcl.field_child("value")):
                        f = self._resolve(repo, rel, self._spec(dcl.field_child("value")))
                        if f:
                            out.append((f, n.sl, n.el))
            elif not top_only and n.type == "call_expression" and (self._is_require(n) or (
                    n.field_child("function") is not None and n.field_child("function").type == "import")):
                f = self._resolve(repo, rel, self._spec(n))
                if f:
                    out.append((f, n.sl, n.el))
        return out

    def import_lines(self, root):
        out = []
        for n in root.named_children:
            if n.type == "import_statement" or (n.type in ("lexical_declaration", "variable_declaration") and any(
                    d.type == "variable_declarator" and self._is_require(d.field_child("value"))
                    for d in n.named_children)):
                out.append((n.sl, n.el))
        return out

    def params(self, d):
        fn = self._fn(d)
        ps = fn.field_child("parameters") or fn.field_child("parameter")
        pos, other = [], []
        if ps is not None and ps.type == "identifier":
            return [ps.text], [ps.text]
        for p in (ps.named_children if ps is not None else []):
            if p.type == "identifier":
                pos.append(p.text)
            elif p.type == "assignment_pattern" and p.field_child("left").type == "identifier":
                pos.append(p.field_child("left").text)
            else:
                other.extend(x.text for x in walk(p) if x.type in ("identifier",
                                                                    "shorthand_property_identifier_pattern"))
        return pos, pos + other

    def bases(self, cls):
        h = next((c for c in cls.named_children if c.type == "class_heritage"), None)
        out = []
        for b in (h.named_children if h is not None else []):
            if b.type == "identifier":
                out.append(b.text)
            elif b.type == "member_expression":
                out.append(b.field_child("property").text)
        return out

    def local_bindings(self, d):
        out = set()
        fn = self._fn(d)
        for n in walk(fn):
            if n.type == "variable_declarator":
                for x in walk(n.field_child("name")):
                    if x.type in ("identifier", "shorthand_property_identifier_pattern"):
                        out.add(x.text)
            elif n.type in ("assignment_expression", "augmented_assignment_expression"):
                left = n.field_child("left")
                if left is not None and left.type == "identifier":
                    out.add(left.text)
            elif n.type == "for_in_statement":
                left = n.field_child("left")
                for x in walk(left) if left is not None else []:
                    if x.type == "identifier":
                        out.add(x.text)
            elif n.type == "formal_parameters":
                for x in walk(n):
                    if x.type in ("identifier", "shorthand_property_identifier_pattern") and not self.is_ref(x):
                        out.add(x.text)
            elif n.type == "catch_clause":
                p = n.field_child("parameter")
                for x in walk(p) if p is not None else []:
                    if x.type == "identifier":
                        out.add(x.text)
        return out

    def test_child(self, p, node):
        if p.type in ("if_statement", "while_statement", "do_statement", "ternary_expression"):
            return p.field_child("condition") is node
        return False

    def is_boolop(self, p):
        if p.type == "binary_expression":
            op = p.field_child("operator")
            return op is not None and op.text in ("&&", "||", "??")
        return p.type == "unary_expression" and any(c.text == "!" for c in p.children if not c.named)

    def subscript_value(self, p):
        return p.field_child("object") if p.type == "subscript_expression" else None

    def assigned_names(self, p, node):
        if p.type == "variable_declarator" and p.field_child("value") is node:
            nm = p.field_child("name")
            return ([nm.text] if nm is not None and nm.type == "identifier" else []), False
        if p.type == "assignment_expression" and p.field_child("right") is node:
            left = p.field_child("left")
            return ([left.text] if left is not None and left.type == "identifier" else []), False
        return [], False

    def keyword_name_position(self, n):
        return n.parent is not None and n.parent.type == "pair" and n.field == "key"

    def module_path_strings(self, rel):
        return []


class Tags(Lang):
    """Any grammar that ships a tags query: definitions and calls come from the query, everything else from node-type
    and field-name conventions shared across tree-sitter grammars. Coarser than a hand-written class."""
    CLASS_KINDS = {"class", "interface", "struct", "module", "trait", "enum", "type", "object", "impl", "union",
                   "record", "protocol", "namespace"}
    FUNC_KINDS = {"function", "method", "macro", "constructor"}
    INHERITS = {"typescript": ("javascript",), "tsx": ("javascript",), "cpp": ("c",)}  # upstream composes these
    CALL_CAPS = ("reference.call", "reference.send")
    CALLEE_FIELDS = ("function", "method", "name", "constructor")
    OBJ_FIELDS = ("object", "operand", "value", "receiver", "expression", "argument", "scope", "path")
    NAME_FIELDS = ("property", "attribute", "field", "name", "method")
    NOT_ATTR = ("call", "invocation", "definition", "declaration", "declarator", "parameter", "import", "argument",
                "assignment", "named_expression", "binding", "pair", "keyword", "default")
    self_names = {"self", "this", "cls", "me"}
    literal_kw = {"true": "true", "false": "false", "null": "null", "nil": "null", "none": "null",
                  "null_literal": "null", "nil_literal": "null", "undefined": "undefined"}
    common = {"get", "set", "add", "remove", "delete", "has", "len", "length", "size", "push", "pop", "append",
              "extend", "insert", "map", "filter", "reduce", "forEach", "each", "keys", "values", "items", "entries",
              "join", "split", "replace", "trim", "strip", "format", "toString", "to_string", "String", "equals",
              "hashCode", "clone", "copy", "update", "clear", "close", "open", "read", "write", "flush", "run", "main",
              "start", "stop", "send", "parse", "load", "init", "new", "log", "debug", "info", "warn", "warning",
              "error", "exception", "critical", "print", "println", "printf", "then", "catch", "unwrap", "expect",
              "iter", "collect", "Ok", "Err", "Some", "None", "apply", "call", "bind", "next", "handle", "test"}

    def __init__(self, name, suffixes):
        self.name, self.fence, self.suffixes = name, name, suffixes
        self.init_name = {"python": "__init__", "ruby": "initialize", "php": "__construct"}.get(name, "constructor")
        self.queries = [Query(get_language(name), get_tags_query(name))]
        for extra in self.INHERITS.get(name, ()):
            try:
                self.queries.append(Query(get_language(name), get_tags_query(extra)))
            except Exception:
                pass  # a parent pattern naming a node type this grammar lacks
        self.ident, self.drop = IdentTypes(), CommentTypes()
        self.attr_types, self.call_types = {}, {}
        self.string_types, self.number_types, self.pattern_types = set(), set(), set()
        self.transparent, self.await_types = set(), set()

    def parse(self, text):
        src = text.encode("utf-8")
        tree = self.parser.parse(src)
        root = to_ir(tree, src, self.drop)
        at = {(n.sb, n.eb, n.type): n for n in walk(root)}

        def ir(x):
            return at.get((x.start_byte, x.end_byte, x.type))

        for _pi, caps in (m for q in self.queries for m in QueryCursor(q).matches(tree.root_node)):
            names = caps.get("name", [])
            for cap, nodes in caps.items():
                kind = cap.split(".", 1)[1] if "." in cap else ""
                for x in nodes:
                    n, nm = ir(x), ir(names[0]) if names else None
                    if n is None or nm is None:
                        continue
                    if cap.startswith("definition.") and kind == "constant":
                        nm.tag = nm.tag or "const"
                    elif cap.startswith("definition.") and (kind in self.CLASS_KINDS or kind in self.FUNC_KINDS):
                        n.tag = ("def", "class" if kind in self.CLASS_KINDS else "function", nm)
                        nm.tag = "name"
                    elif cap in self.CALL_CAPS and n.tag is None:
                        n.tag = ("call", nm)
                        fn = next((c for c in n.children if c.sb <= nm.sb and nm.eb <= c.eb), None)
                        self.call_types.setdefault(n.type, fn.field if fn is not None else None)
        for n in walk(root):
            if n.tag is None and n.named and ("call" in n.type or "invocation" in n.type):
                self._tag_call(n)
            self._learn(n)
        return root, tree.root_node.has_error

    def _tag_call(self, n):
        """Grammars whose tags query has no call captures: the callee is the call's function/method/name field."""
        f = next((n.field_child(x) for x in self.CALLEE_FIELDS if n.field_child(x) is not None), None)
        if f is None:
            return
        nm = f if f.type in self.ident else next(
            (f.field_child(x) for x in self.NAME_FIELDS if f.field_child(x) is not None
             and f.field_child(x).type in self.ident), None)
        if nm is not None:
            n.tag = ("call", nm)
            self.call_types.setdefault(n.type, f.field)

    def _learn(self, n):
        t = n.type
        if not n.named:
            return
        fields = {c.field for c in n.children if c.field}
        obj = next((f for f in self.OBJ_FIELDS if f in fields), None)
        nmf = next((f for f in self.NAME_FIELDS if f in fields), None)
        if obj and nmf and obj != nmf and not any(w in t for w in self.NOT_ATTR) and t not in self.call_types:
            self.attr_types.setdefault(t, (obj, nmf))
        if "string" in t and not t.endswith(("content", "fragment", "start", "end", "_char")) and n.parent is not None \
                and "string" not in n.parent.type:
            self.string_types.add(t)
        elif any(w in t for w in ("integer", "float", "number", "int_literal", "decimal", "real_literal")):
            self.number_types.add(t)
        if t in self.call_types or any(w in t for w in ("binary", "boolean_operator", "comparison", "subscript",
                                                         "index_expression", "conditional", "ternary")):
            self.pattern_types.add(t)
        if any(w in t for w in ("argument_list", "arguments", "parenthesized", "expression_statement")):
            self.transparent.add(t)
        if "await" in t or "parenthesized" in t:
            self.await_types.add(t)

    def self_types(self):
        return {"self", "this"}

    def string_value(self, n):
        if n.type not in self.string_types:
            return None
        if any(any(w in c.type for w in ("interpolation", "substitution", "template_")) for c in n.children):
            return None
        parts = [c for c in n.children if any(w in c.type for w in ("content", "fragment", "escape"))]
        if parts:
            return "".join(c.text for c in parts)
        s = n.text
        return s[1:-1] if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`" else None

    def defs(self, root):
        return [(n, n.tag[1], n.tag[2].text, n.sl) for n in walk(root)
                if isinstance(n.tag, tuple) and n.tag[0] == "def"]

    def owner_node(self, n):
        return n

    def body(self, d):
        return d.field_child("body")

    def body_line(self, d):
        b = self.body(d)
        if b is None:
            return d.el
        kids = b.named_children
        return kids[0].sl if kids else b.sl

    def docstring_end(self, d):
        return None

    def _in(self, n, words, stop=None):
        x = n.parent
        while x is not None and x is not stop:
            if any(w in x.type for w in words):
                return x
            x = x.parent
        return None

    def is_ref(self, n):
        if n.type not in self.ident or n.tag == "name" or n.parent is None:
            return False
        if self._in(n, ("import", "use_declaration", "package_clause", "package_declaration", "include", "using")):
            return False
        if self.keyword_name_position(n):
            return False
        p = self._in(n, ("parameter",))
        if p is not None:
            x = n
            while x is not None and x is not p:
                if x.field in ("value", "default", "default_value", "type"):
                    return True
                x = x.parent
            return False
        return True

    def callee_name(self, call):
        if not (isinstance(call.tag, tuple) and call.tag[0] == "call"):
            return None, None
        nm = call.tag[1]
        holder = nm.parent if nm.parent is not None and nm.parent is not call else call
        obj = None
        f = self.attr_types.get(holder.type)
        if f:
            obj = holder.field_child(f[0])
        elif holder is call:
            obj = next((call.field_child(o) for o in self.OBJ_FIELDS if call.field_child(o) is not None
                        and call.field_child(o) is not nm and o != "argument"), None)
        if obj is None:
            return None, nm.text
        if obj.text in self.self_names or obj.type in self.self_types():
            return "self", nm.text
        return (obj.text if obj.type in self.ident else "?"), nm.text

    def _args_node(self, call):
        a = call.field_child("arguments")
        return a if a is not None else next((c for c in call.named_children if "argument" in c.type), None)

    def call_args(self, call):
        args = self._args_node(call)
        pos, kws, star = [], [], False
        for a in (args.named_children if args is not None else []):
            if any(w in a.type for w in ("keyword_argument", "named_argument", "labeled_argument")):
                nm, val = a.field_child("name"), a.field_child("value")
                if nm is not None and val is not None:
                    kws.append((nm.text, val))
                    continue
            if any(w in a.type for w in ("splat", "spread")):
                star = True
            pos.append(a)
        return pos, kws, star

    def string_refs(self, call):
        return []

    def _imports(self, root):
        return [n for n in root.named_children
                if any(w in n.type for w in ("import", "use_declaration", "include", "using_directive"))]

    def top_statements(self, root, skip_type_checking=False):
        return list(root.named_children)

    def _decl_names(self, n):
        out = []
        for c in walk(n):
            if c.field in ("left", "name", "pattern") and c.type in self.ident and c.tag != "name":
                out.append(c)
        return out

    def module_bindings(self, root):
        consts, names, imports = {}, set(), self._imports(root)
        for n in root.named_children:
            if isinstance(n.tag, tuple) and n.tag[0] == "def":
                names.add(n.tag[2].text)
                continue
            if n in imports:
                names.update(c.text for c in walk(n) if c.type in self.ident)
                continue
            if any(w in n.type for w in ("assignment", "declaration", "const", "var", "let", "expression_statement")):
                for x in walk(n):
                    if (x.tag == "const" or x.field in ("left", "name")) and x.type in self.ident:
                        p = x.parent
                        right = next((p.field_child(f) for f in ("right", "value") if p.field_child(f) is not None),
                                     None)
                        names.add(x.text)
                        if right is not None and not x.text.startswith("__"):
                            consts.setdefault(x.text, (n, right.type in self.call_types))
                        break
        return consts, names

    def module_of(self, rel):
        return os.path.splitext(rel)[0].replace("/", ".")

    def _module_file(self, repo, key):
        """key = (importing file, spec): a quoted path or a dotted/scoped name; matched against repo files."""
        rel, spec = key
        spec = (spec or "").strip("\"'<>`")
        if not spec:
            return None
        if spec.startswith("."):
            lead = len(spec) - len(spec.lstrip("."))
            rest = spec[lead:]
            if "/" in rest or not rest or spec.startswith(("./", "../")):
                base = os.path.normpath(os.path.join(os.path.dirname(rel), spec)).replace(os.sep, "/")
            else:
                d = os.path.dirname(rel)
                for _ in range(lead - 1):
                    d = os.path.dirname(d)
                base = "/".join(x for x in (d, rest.replace(".", "/")) if x)
            cands = [base]
        else:
            cands = [re.sub(r"::|\\|\.", "/", spec) if "/" not in spec else spec]
        for base in cands:
            for p in [base + s for s in self.suffixes] + [f"{base}/{i}{s}" for i in ("index", "__init__", "mod")
                                                           for s in self.suffixes]:
                if p in repo.trees:
                    return p
            tail = "/" + base.lstrip("./")
            hits = sorted(p for p in repo.trees if os.path.splitext("/" + p)[0].endswith(tail))
            if len(hits) == 1:
                return hits[0]
            hits = sorted(p for p in repo.trees if ("/" + os.path.dirname(p)).endswith(tail))
            if hits:
                return hits[0]
        return None

    def _specs(self, n):
        """(spec, imported local names) for one import node: quoted paths, else the longest dotted name."""
        strs = [c for c in walk(n) if c.type in self.string_types]
        dotted = [c for c in walk(n) if c.named and c is not n and c.type not in self.string_types
                  and re.fullmatch(r"\.*[\w$]+(?:(?:\.|::|\\)[\w$]+)+|\.+[\w$]*", c.text)]
        idents = [c.text for c in walk(n) if c.type in self.ident]
        if strs:
            return [(self.string_value(s) or s.text, idents) for s in strs]
        if dotted:
            best = max(dotted, key=lambda c: len(c.text))
            rest = [i for i in idents if i not in re.split(r"\.|::|\\", best.text)]
            return [(best.text, rest)]
        return [(i, []) for i in idents[:1]]

    @staticmethod
    def _join(spec, nm):
        return None if "/" in spec else spec + nm if spec.endswith(".") else f"{spec}.{nm}"

    def import_map(self, repo, rel, root):
        out = {}
        for n in self._imports(root):
            for spec, names in self._specs(n):
                f = repo.module_file(self, (rel, spec))
                for nm in names:
                    sub = repo.module_file(self, (rel, self._join(spec, nm)))
                    if sub:
                        out[nm] = ("module", sub)
                    elif f:
                        out[nm] = ("symbol", f, nm)
                if f and not names:
                    out[re.split(r"[./:\\]", spec.strip("\"'<>"))[-1]] = ("module", f)
        return out

    def import_edges(self, repo, rel, root, top_only):
        out = []
        for n in self._imports(root):
            for spec, names in self._specs(n):
                f = repo.module_file(self, (rel, spec))
                for nm in names:
                    sub = repo.module_file(self, (rel, self._join(spec, nm)))
                    if sub:
                        out.append((sub, n.sl, n.el))
                if f:
                    out.append((f, n.sl, n.el))
        return out

    def import_lines(self, root):
        return [(n.sl, n.el) for n in self._imports(root)]

    def _param_nodes(self, d):
        ps = d.field_child("parameters")
        if ps is None:
            ps = next((c for c in d.named_children if "parameter" in c.type), None)
        return ps.named_children if ps is not None else []

    def params(self, d):
        out = []
        for p in self._param_nodes(d):
            if p.type in self.ident:
                nm = p
            else:
                nm = next((p.field_child(f) for f in ("name", "pattern") if p.field_child(f) is not None), None)
                if nm is None or nm.type not in self.ident:
                    nm = next((c for c in walk(p) if c.type in self.ident and c.field != "type"), None)
            if nm is not None and nm.text not in self.self_names:
                out.append(nm.text)
        return out, out

    def bases(self, cls):
        out = []
        for c in cls.children:
            if c.field in ("superclasses", "superclass", "bases", "interfaces") or any(
                    w in c.type for w in ("superclass", "heritage", "base_list", "extends", "implements",
                                          "delegation_specifier", "base_class")):
                for x in walk(c):
                    if x.type in self.ident and not x.named_children:
                        out.append(x.text)
        return out

    def local_bindings(self, d):
        out = set(self.params(d)[1])
        for n in walk(d):
            if n is not d and any(w in n.type for w in ("assignment", "declarator", "declaration", "binding",
                                                        "for_statement", "for_in", "let", "var")):
                for x in self._decl_names(n):
                    out.add(x.text)
        return out

    def test_child(self, p, node):
        if any(w in p.type for w in ("if", "while", "conditional", "ternary", "assert", "guard")):
            c = p.field_child("condition")
            return c is node or (c is None and bool(p.named_children) and p.named_children[0] is node)
        return False

    def is_boolop(self, p):
        if any(w in p.type for w in ("boolean_operator", "not_operator", "logical")):
            return True
        op = p.field_child("operator")
        return op is not None and op.text in ("&&", "||", "and", "or", "!", "not")

    def subscript_value(self, p):
        if "subscript" in p.type or "index_expression" in p.type or "element_access" in p.type:
            v = next((p.field_child(f) for f in ("value", "object", "operand", "expression")
                      if p.field_child(f) is not None), None)
            return v if v is not None else (p.named_children[0] if p.named_children else None)
        return None

    def assigned_names(self, p, node):
        if any(w in p.type for w in ("assignment", "declarator")):
            right = next((p.field_child(f) for f in ("right", "value") if p.field_child(f) is not None), None)
            if right is node:
                left = next((p.field_child(f) for f in ("left", "name", "pattern") if p.field_child(f) is not None),
                            None)
                return ([left.text] if left is not None and left.type in self.ident else []), False
        return [], False

    def keyword_name_position(self, n):
        p = n.parent
        return p is not None and n.field == "name" and any(w in p.type for w in ("keyword_argument", "named_argument",
                                                                                   "labeled_argument"))

    def module_path_strings(self, rel):
        mod = self.module_of(rel)
        return [f'"{mod}"', f"'{mod}'"]


class CommentTypes:
    def __contains__(self, t):
        return "comment" in t


class IdentTypes:
    """Identifier-like node types across grammars (identifier, field_identifier, property_identifier, constant ...)."""
    OTHER = {"name", "constant", "word", "simple_identifier"}

    def __contains__(self, t):
        return t.endswith("identifier") or t in self.OTHER


class GraphifyCalls:
    """Call edges from a Graphify graph (graphify extract --code-only), mapped onto this builder's defs by file and
    start line. Replaces name-based caller search and call resolution for every def Graphify also has."""
    RELS = ("calls", "indirect_call")

    def __init__(self, gdir, repo):
        g = json.load(open(os.path.join(gdir, "graph.json")))
        by_loc = {(d.path, d.start): d for d in repo.defs}
        self.node_def = {}
        for n in g["nodes"]:
            loc = n.get("source_location") or ""
            if n.get("file_type") == "code" and loc.startswith("L") and loc[1:].isdigit():
                d = by_loc.get((n.get("source_file"), int(loc[1:])))
                if d is not None:
                    self.node_def[n["id"]] = d
        self.def_node = {d.key: nid for nid, d in self.node_def.items()}
        self.out, self.inc = {}, {}
        for e in g["links"]:
            if e["relation"] in self.RELS and e["source"] in self.node_def and e["target"] in self.node_def:
                self.out.setdefault(e["source"], set()).add(e["target"])
                self.inc.setdefault(e["target"], set()).add(e["source"])
        print(f"graphify: {len(self.def_node)} of {len(repo.defs)} defs mapped, "
              f"{sum(map(len, self.out.values()))} call edges between them", file=sys.stderr)

    def has(self, d):
        return d.key in self.def_node

    def callees(self, d, name):
        out = sorted(self.out.get(self.def_node[d.key], ()))
        hits = [self.node_def[t] for t in out if self.node_def[t].name == name]
        return hits, ("local" if all(h.path == d.path for h in hits) else "import") if hits else None

    def callers(self, d):
        return [self.node_def[s] for s in sorted(self.inc.get(self.def_node[d.key], ()))]


# extensions probed for a tags-query grammar when no hand-written class covers them
TAGS_EXTS = (".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs", ".go", ".java", ".kt", ".kts", ".scala", ".swift",
             ".rs", ".rb", ".php", ".ts", ".tsx", ".mts", ".cts", ".dart", ".ex", ".exs", ".lua", ".r", ".jl", ".ml",
             ".mli", ".hs", ".elm", ".gleam", ".nix", ".sol", ".proto", ".fs", ".fsi", ".d", ".zig", ".vue", ".svelte",
             ".py", ".pyi", ".js", ".mjs", ".cjs", ".jsx")


def tags_langs(skip):
    by_name = {}
    for ext in TAGS_EXTS:
        if any(ext in lg.suffixes for lg in skip):
            continue
        try:
            name = detect_language_from_extension(ext.lstrip("."))
            if name and (get_tags_query(name) or "").strip():
                by_name.setdefault(name, []).append(ext)
        except Exception:
            continue
    return [Tags(name, tuple(exts)) for name, exts in sorted(by_name.items())]


LANGS = [Python(), JavaScript()]


def lang_for(path):
    ext = os.path.splitext(path)[1]
    return next((lg for lg in LANGS if ext in lg.suffixes), None)


# ---------------------------------------------------------------- diff parsing

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_diff(repo, base, head):
    mb = git(repo, "merge-base", base, head).strip()
    files = {}
    for line in git(repo, "diff", "--no-ext-diff", "--no-textconv", "--name-status", "-M", mb, head).splitlines():
        parts = line.split("\t")
        files[parts[-1]] = {"added": set(), "del_anchors": set(), "status": parts[0][0]}
    cur = None
    for line in git(repo, "diff", "--no-ext-diff", "--no-textconv", "--unified=0", "-M", "--no-color", mb,
                    head).splitlines():
        if line.startswith("+++ "):
            p = line[4:]
            cur = p[2:] if p.startswith("b/") else None
            continue
        m = HUNK_RE.match(line)
        if m and cur in files:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            if count == 0:
                files[cur]["del_anchors"].add(max(start, 1))
            else:
                files[cur]["added"].update(range(start, start + count))
    return mb, files


# ---------------------------------------------------------------- definition index

class Def:
    __slots__ = ("path", "qualname", "name", "kind", "start", "end", "node", "refs", "calls", "parent", "children",
                 "strrefs", "dotrefs", "lang", "body_line")

    def __init__(self, path, qualname, name, kind, start, end, node, parent, lang):
        self.path, self.qualname, self.name, self.kind = path, qualname, name, kind
        self.start, self.end, self.node, self.parent, self.lang = start, end, node, parent, lang
        self.refs, self.calls, self.children = set(), set(), []
        self.strrefs, self.dotrefs = set(), set()
        self.body_line = lang.body_line(node)

    @property
    def key(self):
        return f"{self.path}::{self.qualname}"

    def enclosing_class(self):
        p = self.parent
        while p is not None and p.kind != "class":
            p = p.parent
        return p


def collect_defs(path, root, lang):
    specs = lang.defs(root)
    by_node = {id(n): (kind, name, start) for n, kind, name, start in specs}
    defs, owner_of = [], {}

    def visit(n, prefix, parent):
        for c in n.children:
            target = lang.owner_node(c)
            spec = by_node.get(id(target))
            if spec is None:
                visit(c, prefix, parent)
                continue
            kind, name, start = spec
            if kind == "function" and parent is not None and parent.kind == "class":
                kind = "method"
            q = f"{prefix}.{name}" if prefix else name
            d = Def(path, q, name, kind, start, target.el, target, parent, lang)
            if parent is not None:
                parent.children.append(d)
            defs.append(d)
            owner_of[id(c)] = owner_of[id(target)] = d
            visit(target, q, d)

    visit(root, "", None)

    def assign(n, owner):
        for c in n.children:
            new_owner = owner_of.get(id(c), owner)
            if owner is not None and new_owner is owner:
                if lang.is_ref(c):
                    owner.refs.add(c.text)
                else:
                    v = lang.string_value(c) if c.type in lang.string_types else None
                    if v and "." in v and DOTTED.match(v):
                        owner.dotrefs.add(v.rsplit(".", 1)[1])
                if c.type in lang.call_types:
                    owner.strrefs.update(lang.string_refs(c))
                    recv, name = lang.callee_name(c)
                    if name is not None:
                        owner.calls.add((recv, name))
            assign(c, new_owner)

    assign(root, None)
    return defs


class Repo:
    def __init__(self, root, langs):
        self.root = root
        self.sources, self.trees, self.defs, self.by_name, self.importmap = {}, {}, [], {}, {}
        self.consts, self.modnames, self._modfile, self.langs = {}, {}, {}, {}
        self._edges = {}
        suffixes = tuple(s for lg in langs for s in lg.suffixes)
        for rel in HEAD.blobs:
            dirs = rel.split("/")[:-1]
            if rel.endswith(suffixes) and not any(d in SKIP_DIRS or d.startswith(".") for d in dirs):
                self.trees[rel] = None
        for rel in sorted(self.trees):
            self.load(rel)
        for rel in sorted(self.sources):
            self.importmap[rel] = self.langs[rel].import_map(self, rel, self.trees[rel])
        for d in self.defs:
            self.by_name.setdefault(d.name, []).append(d)
        self.top = {(d.path, d.qualname): d for d in self.defs}

    def load(self, rel):
        lang = lang_for(rel)
        src = HEAD.read_text(rel)
        if src is None:
            return
        try:
            root, broken = lang.parse(src)
            if broken:
                return  # unparsable at head (the stdlib builder skipped what ast.parse refused)
            defs = collect_defs(rel, root, lang)
            bindings = lang.module_bindings(root)
        except RecursionError:
            return  # nested too deeply to walk; leave the file out rather than fail the build
        self.sources[rel] = src.splitlines()
        self.trees[rel] = root
        self.langs[rel] = lang
        self.defs.extend(defs)
        self.consts[rel], self.modnames[rel] = bindings

    def resolve_const(self, path, name, depth=0):
        if name in self.consts.get(path, {}):
            return path, name, self.consts[path][name]
        imp = self.importmap.get(path, {}).get(name)
        if imp and imp[0] == "symbol" and depth < 3:
            return self.resolve_const(imp[1], imp[2], depth + 1)
        return None

    def module_file(self, lang, key):
        k = (lang.name, key)
        if k not in self._modfile:
            self._modfile[k] = lang._module_file(self, key)
        return self._modfile[k]

    def import_edges(self, rel, top_only):
        k = (rel, top_only)
        if k not in self._edges:
            root = self.trees.get(rel)
            self._edges[k] = [] if root is None else self.langs[rel].import_edges(self, rel, root, top_only)
        return self._edges[k]

    def lines(self, path, start, end):
        return self.sources[path][start - 1:end]

    def defs_in(self, path):
        return [d for d in self.defs if d.path == path]

    def imports(self, path):
        root = self.trees.get(path)
        out = []
        for a, b in (self.langs[path].import_lines(root) if root is not None else []):
            out.extend(self.lines(path, a, b))
        return out

    def resolve_call(self, d, recv, name):
        imap = self.importmap.get(d.path, {})
        lang = d.lang
        if recv is None:
            local = self.top.get((d.path, name))
            if local:
                return [local], "local"
            if d.parent is not None:
                nested = self.top.get((d.path, f"{d.qualname}.{name}"))
                if nested:
                    return [nested], "local"
            imp = imap.get(name)
            if imp and imp[0] == "symbol":
                hit = self.top.get((imp[1], imp[2]))
                if hit:
                    return [hit], "import"
                pkg_dir = imp[1].rsplit("/", 1)[0]
                hits = [t for t in self.by_name.get(imp[2], []) if t.path.startswith(pkg_dir + "/")
                        and t.parent is None]
                if len(hits) <= 3:
                    return hits, "import"
            return [], None
        if recv == "self":
            cls = d.enclosing_class()
            seen = set()
            while cls is not None and cls.key not in seen:
                seen.add(cls.key)
                m = self.top.get((cls.path, f"{cls.qualname}.{name}"))
                if m:
                    return [m], "self"
                nxt = None
                for bname in lang.bases(cls.node):
                    cands = [c for c in self.by_name.get(bname, []) if c.kind == "class"]
                    if cands:
                        nxt = cands[0]
                        break
                cls = nxt
        else:
            imp = imap.get(recv)
            if imp and imp[0] == "module":
                hit = self.top.get((imp[1], name))
                return ([hit] if hit else []), "import"
        if name in lang.common or name.startswith("__"):
            return [], None
        hits = [t for t in self.by_name.get(name, []) if (not is_test_path(t.path) or t.path == d.path)
                and t.lang is lang]
        return (hits if len(hits) <= 3 else []), "name"


# ---------------------------------------------------------------- rendering helpers

def code_block(lines, start, max_lines=None, note="", lang="python"):
    shown = lines if max_lines is None else lines[:max_lines]
    trunc = ""
    cmt = "#" if lang == "python" else "//"
    if max_lines is not None and len(lines) > max_lines:
        trunc = f"\n{cmt} ... truncated ({len(lines) - max_lines} more lines)"
    return f"```{lang}\n{cmt} L{start}{note}\n" + "\n".join(shown) + f"{trunc}\n```\n"


def class_skeleton(repo, d):
    out, src, skip_until = [], repo.sources[d.path], 0
    cmt = "#" if d.lang.name == "python" else "//"
    for ln in range(d.start, d.end + 1):
        if ln <= skip_until:
            continue
        child = next((c for c in d.children if c.start == ln), None)
        if child is not None and child.body_line > ln:
            body0 = child.body_line
            out.extend(src[ln - 1:body0 - 1])
            indent = re.match(r"\s*", src[body0 - 1]).group(0)
            out.append(f"{indent}...  {cmt} body elided (L{body0}-{child.end})")
            skip_until = child.end
        else:
            out.append(src[ln - 1])
    return out


def signature_and_doc(repo, d):
    src = repo.sources[d.path]
    end = d.body_line - 1
    doc = d.lang.docstring_end(d.node)
    if doc is not None:
        end = doc
    return src[d.start - 1:max(end, d.start)][:CALLEE_MAX_LINES]


def window(lines, start, changed, ctx=25, cmt="#"):
    keep = {x for c in changed for x in range(c - ctx, c + ctx + 1)}
    out, gap = [], False
    for i, line in enumerate(lines):
        if start + i in keep or i < 3:
            out.append(line)
            gap = False
        elif not gap:
            out.append(f"    {cmt} ... unchanged lines elided ...")
            gap = True
    return out


# ---------------------------------------------------------------- similarity

NAME_SPLIT = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def name_tokens(name):
    return {t.lower() for part in name.split("_") for t in NAME_SPLIT.findall(part) if t}


def canon_tokens(d):
    """Token stream of a def with literals blanked, its name, decorators and docstring dropped."""
    lang = d.lang
    node = d.node
    skip = set()
    nm = node.field_child("name")
    if nm is not None:
        skip.add(id(nm))
    b = lang.body(node)
    if lang.docstring_end(node) is not None and b is not None and b.named_children:
        skip.add(id(b.named_children[0]))
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if id(n) in skip:
            continue
        if lang.is_constant(n) or n.type in lang.string_types:
            out.extend(("constant", "C"))
            continue
        if n.type in lang.ident or n.type in lang.self_types():
            out.extend((n.type, n.text))
            continue
        if not n.named:
            if n.text:
                out.append(n.text)
            continue
        out.append(n.type)
        stack.extend(reversed(n.children))
    return out


def lcs_len(a, alo, ahi, masks, blo, bhi):
    width = bhi - blo
    if width <= 0 or ahi <= alo:
        return 0
    full = (1 << width) - 1
    v = full
    for t in a[alo:ahi]:
        u = v & (masks.get(t, 0) >> blo)
        v = ((v + u) | (v - u)) & full
    return width - v.bit_count()


def bounded_ratio(sm, a, masks, lb, floor):
    total = len(a) + lb
    first = lcs_len(a, 0, len(a), masks, 0, lb)
    if 2.0 * first / total < floor:
        return None
    matched, pending, heap = 0, first, [(-first, 0, len(a), 0, lb)]
    while heap:
        neg, alo, ahi, blo, bhi = heapq.heappop(heap)
        pending += neg
        i, j, k = sm.find_longest_match(alo, ahi, blo, bhi)
        if k:
            matched += k
            for sub in ((alo, i, blo, j), (i + k, ahi, j + k, bhi)):
                if sub[0] < sub[1] and sub[2] < sub[3]:
                    n = lcs_len(a, sub[0], sub[1], masks, sub[2], sub[3])
                    if n:
                        pending += n
                        heapq.heappush(heap, (-n, *sub))
        if 2.0 * (matched + pending) / total < floor:
            return None
    return 2.0 * matched / total


def in_scope(path, scope):
    return any(path.startswith(s) for s in scope)


def find_similar(repo, changed_funcs, exclude_keys, scope):
    cands = [d for d in repo.defs if d.kind in ("function", "method") and in_scope(d.path, scope)
             and d.key not in exclude_keys and (d.end - d.start) >= 2]
    cache = {}

    def toks(d):
        if d.key not in cache:
            cache[d.key] = canon_tokens(d)
        return cache[d.key]

    names = {c.key: name_tokens(c.name) for c in cands}

    def order(x):
        return (-x[0], -x[1], x[3].key)

    result = {}
    for f in changed_funcs:
        ft, fn = toks(f), name_tokens(f.name)
        sm = difflib.SequenceMatcher(None, autojunk=False)
        sm.set_seq2(ft)
        bounded = []
        for c in cands:
            if c.lang is not f.lang:
                continue
            cn = names[c.key]
            jac = len(fn & cn) / len(fn | cn) if (fn | cn) else 0.0
            ub = 0.0
            ct = toks(c)
            if ft and ct and min(len(ft), len(ct)) / max(len(ft), len(ct)) >= 0.6:
                sm.set_seq1(ct)
                if sm.real_quick_ratio() >= 0.6:
                    q = sm.quick_ratio()
                    if q >= 0.6:
                        ub = q
            if ub >= 0.6 or jac >= 0.5:
                bounded.append((max(ub, jac), ub, jac, c))
        bounded.sort(key=lambda x: (-x[0], x[3].key))
        masks = {}
        for j, t in enumerate(ft):
            masks[t] = masks.get(t, 0) | (1 << j)
        scored = []
        for bound, ub, jac, c in bounded:
            kth = float("-inf")
            if len(scored) >= SIMILAR_PER_FUNC:
                scored.sort(key=order)
                del scored[SIMILAR_PER_FUNC:]
                kth = scored[-1][0]
                if bound < kth:
                    break
            ratio = 0.0
            if ub:
                floor = float("-inf") if jac >= 0.5 and jac >= kth else (kth if jac >= 0.5 else max(0.6, kth))
                sm.set_seq1(toks(c))
                ratio = bounded_ratio(sm, toks(c), masks, len(ft), floor)
                if ratio is None:
                    continue
            if ratio >= 0.6 or jac >= 0.5:
                scored.append((max(ratio, jac), ratio, jac, c))
        scored.sort(key=order)
        result[f.key] = scored[:SIMILAR_PER_FUNC]
    return result


# ---------------------------------------------------------------- module-level constants

def referenced_constants(repo, d):
    lang = d.lang
    local = lang.local_bindings(d.node)
    imap = repo.importmap.get(d.path, {})
    found = []
    for n in walk(d.node):
        hit = None
        if n.type in lang.attr_types:
            obj, nm = lang.attr_parts(n)
            if obj is not None and nm is not None and obj.type in lang.ident and obj.text not in local:
                imp = imap.get(obj.text)
                if imp and imp[0] == "module":
                    hit = repo.resolve_const(imp[1], nm.text)
        elif lang.is_ref(n) and not is_attr_name(lang, n) and n.text not in local and not is_store(lang, n):
            hit = repo.resolve_const(d.path, n.text)
        if hit:
            found.append(hit)
    return found


def is_attr_name(lang, n):
    p = n.parent
    if p is None or p.type not in lang.attr_types:
        return False
    return n.field == lang.attr_types[p.type][1]


STORE_PARENTS = {"assignment": "left", "augmented_assignment": "left", "assignment_expression": "left",
                 "augmented_assignment_expression": "left", "named_expression": "name", "for_statement": "left",
                 "for_in_clause": "left", "for_in_statement": "left", "variable_declarator": "name"}


def is_store(lang, n):
    """Assignment / loop target (directly or inside a tuple pattern)."""
    x = n
    while x.parent is not None and x.parent.type in ("pattern_list", "tuple_pattern", "list_pattern"):
        x = x.parent
    p = x.parent
    return p is not None and x.field is not None and STORE_PARENTS.get(p.type) == x.field


# ---------------------------------------------------------------- caller relevance

def _value_inspected(lang, node):
    p = node.parent
    while p is not None and p.type in lang.await_types:
        node, p = p, p.parent
    if p is None:
        return False
    if p.type in lang.attr_types:
        obj, _nm = lang.attr_parts(p)
        if obj is node:
            return True
    if lang.subscript_value(p) is node:
        return True
    if lang.test_child(p, node):
        return True
    return lang.is_boolop(p)


def call_name(lang, n):
    return lang.callee_name(n)[1]


def caller_relevance(c, name):
    lang = c.lang
    bound = set()
    for n in walk(c.node):
        if n.type not in lang.call_types or call_name(lang, n) != name:
            continue
        if _value_inspected(lang, n):
            return 1
        node, p = n, n.parent
        while p is not None and p.type in lang.await_types:
            node, p = p, p.parent
        if p is not None:
            names, walrus = lang.assigned_names(p, node)
            bound.update(names)
            if walrus and _value_inspected(lang, p):
                return 1
    for n in walk(c.node):
        if lang.is_ref(n) and not is_attr_name(lang, n) and n.text in bound and not is_store(lang, n) \
                and _value_inspected(lang, n):
            return 1
    return 0


# ---------------------------------------------------------------- same-pattern retrieval

class PatternIndex:
    """Canonical expression shapes over the scope (non-test). Local identifiers become `_`, string literals `S`,
    numbers `N`; builtins, module-level names (imports, defs, constants), attribute and keyword names are kept."""

    def __init__(self, repo, scope):
        self.repo, self.memo, self.sites, self.kept_override = repo, {}, {}, {}
        for path in sorted(repo.sources):
            if not in_scope(path, scope) or is_test_path(path):
                continue
            lang = repo.langs[path]
            for n in walk(repo.trees[path]):
                if n.type in lang.pattern_types:
                    s, size, anchors = self.canon(path, n, lang)
                    if s and size >= PATTERN_MIN_SIZE and anchors:
                        self.sites.setdefault(s, []).append((path, n.sl))

    def kept(self, path, lang):
        if path in self.kept_override:
            return self.kept_override[path]
        return lang.builtins | self.repo.modnames.get(path, set()) | lang.self_names

    def canon(self, path, node, lang):
        memo = self.memo.setdefault(path, {})
        kept = self.kept(path, lang)
        abort = (None, 0, 0)

        def c(n):
            k = id(n)
            if k in memo:
                return memo[k]
            if n.type in lang.ident or n.type in lang.self_types():
                if is_attr_name(lang, n) or lang.keyword_name_position(n):
                    r = (repr(n.text), 0, 1)
                elif not lang.is_ref(n) and n.type not in lang.self_types():
                    r = ("_", 1, 0)
                elif n.text in kept:
                    r = (n.text, 1, int(n.text not in lang.self_names))
                else:
                    r = ("_", 1, 0)
            elif n.type in lang.string_types or n.type in lang.number_types or n.type in lang.literal_kw:
                r = (lang.literal_token(n), 1, 0)
            elif not n.named:
                t = n.text
                r = ((t, 0, 0) if t not in PUNCT else ("", 0, 0))
            else:
                parts, size, anchors = [], 0 if n.type in lang.transparent else 1, 0
                for x in n.children:
                    s, sz, an = c(x)
                    if s is None:
                        memo[k] = abort
                        return abort
                    if s:
                        parts.append(s)
                    size += sz
                    anchors += an
                    if size > PATTERN_MAX_SIZE:
                        memo[k] = abort
                        return abort
                r = (f"{n.type}({','.join(parts)})", size, anchors)
            memo[k] = r
            return r

        return c(node)

    def display(self, path, node, lang):
        kept = self.kept(path, lang)
        edits = []
        for n in walk(node):
            if n.type in lang.string_types:
                edits.append((n.sb, n.eb, "'S'"))
            elif n.type in lang.number_types:
                edits.append((n.sb, n.eb, "N"))
            elif n.type in lang.ident and lang.is_ref(n) and not is_attr_name(lang, n) and n.text not in kept:
                edits.append((n.sb, n.eb, "_"))
        src, out, pos = node.src, [], node.sb
        for a, b, rep in sorted(edits):
            if a < pos:
                continue
            out.append(src[pos:a].decode("utf-8", "replace"))
            out.append(rep)
            pos = b
        out.append(src[pos:node.eb].decode("utf-8", "replace"))
        text = re.sub(r"\s+", " ", "".join(out)).strip()
        return text if len(text) <= 200 else text[:197] + "..."


def same_pattern(repo, pindex, funcs, added_by_path):
    out, used = [], set()
    for f in funcs:
        lang = f.lang
        added = added_by_path.get(f.path, set())
        cands = []
        for n in walk(f.node):
            if n.type not in lang.pattern_types or n.sl not in added:
                continue
            s, size, anchors = pindex.canon(f.path, n, lang)
            if not s or size < PATTERN_MIN_SIZE or not anchors or s in used:
                continue
            sites = [x for x in dict.fromkeys(pindex.sites.get(s, []))
                     if not (x[0] == f.path and f.start <= x[1] <= f.end)]
            if 0 < len(sites) <= PATTERN_MAX_MATCHES:
                cands.append((size, len(sites), n, s, sites))
        cands.sort(key=lambda x: (-x[0], x[1], x[2].sl, x[2].sc))
        covered, picked = set(), 0
        for size, _cnt, n, s, sites in cands:
            if picked >= PATTERNS_PER_FUNC:
                break
            if id(n) in covered or s in used:
                continue
            used.add(s)
            covered.update(id(x) for x in walk(n))
            picked += 1
            sites.sort(key=lambda x: (distance(f.path, x[0]), x[0], x[1]))
            out.append((f, n, s, size, sites))
    return out


# ---------------------------------------------------------------- main build

def distance(a_path, b_path):
    if a_path == b_path:
        return 0
    if os.path.dirname(a_path) == os.path.dirname(b_path):
        return 1
    if a_path.split("/")[0] == b_path.split("/")[0]:
        return 2
    return 3


def map_changes(repo, code_changed):
    changed, module_level, unparsable, rows = {}, {}, [], []
    for path, info in sorted(code_changed.items()):
        if repo.trees.get(path) is None:
            unparsable.append(path)
            continue
        defs = repo.defs_in(path)
        src = repo.sources[path]
        for ln in sorted(info["added"] | info["del_anchors"]):
            if ln > len(src):
                continue
            blank = not src[ln - 1].strip()
            enclosing_defs = [d for d in defs if d.start <= ln <= d.end]
            d = max(enclosing_defs, key=lambda x: x.start) if enclosing_defs else None
            if blank and (d is None or d.kind == "class"):
                continue
            if d is not None:
                changed.setdefault(d.key, (d, set()))[1].add(ln)
                rows.append((path, ln, d.qualname))
            else:
                module_level.setdefault(path, set()).add(ln)
                rows.append((path, ln, "<module>"))
    return changed, module_level, unparsable, rows


def imports_header(repo, path, seen):
    if path in seen:
        return ""
    seen.add(path)
    imps = repo.imports(path)
    fence_lang = repo.langs[path].fence
    return f"### Imports of `{path}`\n```{fence_lang}\n" + "\n".join(imps) + "\n```\n" if imps else ""


HUNK_FULL = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


def parse_deletions(repo, mb, head):
    out, old, new, in_header = {}, None, None, False
    for line in git(repo, "diff", "--no-ext-diff", "--no-textconv", "--unified=0", "-M", "--no-color", mb,
                    head).splitlines():
        if line.startswith("diff --git "):
            in_header, old, new = True, None, None
            continue
        if in_header and line.startswith("--- "):
            p = line[4:]
            old = p[2:] if p.startswith("a/") else None
            continue
        if in_header and line.startswith("+++ "):
            p = line[4:]
            new = p[2:] if p.startswith("b/") else None
            in_header = False
            if old and new:
                out[new] = (old, set())
            continue
        m = HUNK_FULL.match(line)
        if m and old and new:
            start = int(m.group(1))
            count = int(m.group(2)) if m.group(2) is not None else 1
            out[new][1].update(range(start, start + count))
    return out


class BaseSide:
    def __init__(self, root, mb, deletions):
        self.src, self.tree, self.defs, self.deleted, self.lang = {}, {}, {}, {}, {}
        for head_path, (base_path, lines) in deletions.items():
            try:
                text = git(root, "show", f"{mb}:{base_path}")
            except (subprocess.CalledProcessError, UnicodeDecodeError):
                continue
            self.src[head_path], self.deleted[head_path] = text.splitlines(), lines
            lang = lang_for(head_path)
            if lang is not None:
                tree, _broken = lang.parse(text)
                self.tree[head_path], self.lang[head_path] = tree, lang
                self.defs[head_path] = collect_defs(base_path, tree, lang)

    def deleted_text(self, path, lo=None, hi=None):
        src = self.src.get(path, [])
        return [src[ln - 1] for ln in sorted(self.deleted.get(path, ())) if ln <= len(src)
                and (lo is None or lo <= ln <= hi)]

    def def_named(self, path, qualname):
        return next((d for d in self.defs.get(path, []) if d.qualname == qualname), None)


def diff_view(info):
    return {x for c in info["added"] | info["del_anchors"] for x in range(c - DIFF_BEFORE, c + DIFF_AFTER + 1)}


def enclosing(defs, ln):
    hits = [d for d in defs if d.start <= ln <= d.end]
    return max(hits, key=lambda x: x.start) if hits else None


def snippet(src, anchors, ctx, max_lines, lo=1, hi=None, marks=None):
    hi = len(src) if hi is None else min(hi, len(src))
    keep = sorted({x for a in anchors for x in range(max(lo, a - ctx), min(hi, a + ctx) + 1)})
    out, prev = [], None
    for ln in keep:
        if prev is not None and ln != prev + 1:
            out.append("  ...")
        out.append(f"{ln:>5}| {src[ln - 1]}{(marks or {}).get(ln, '')}")
        prev = ln
    if len(out) > max_lines:
        out = out[:max_lines] + [f"  ... ({len(out) - max_lines} more lines)"]
    return out


def fence(lines, lang="python"):
    return f"```{lang}\n" + "\n".join(lines) + "\n```\n"


def numbered_lines(repo, d):
    src = repo.sources[d.path]
    cmt = "#" if d.lang.name == "python" else "//"
    if d.kind != "class":
        return [(ln, src[ln - 1]) for ln in range(d.start, d.end + 1)]
    out, skip = [], 0
    for ln in range(d.start, d.end + 1):
        if ln <= skip:
            continue
        child = next((c for c in d.children if c.start == ln), None)
        if child is None or child.body_line <= ln:
            out.append((ln, src[ln - 1]))
            continue
        body0 = child.body_line
        out.extend((x, src[x - 1]) for x in range(ln, body0))
        indent = re.match(r"\s*", src[body0 - 1]).group(0)
        out.append((None, f"{indent}...  {cmt} body elided (L{body0}-{child.end})"))
        skip = child.end
    return out


def outside_diff(numbered, body_start, visible, changed, win, indent, cmt="#"):
    near = {x for c in changed for x in range(c - win, c + win + 1)} if win else set()
    out, run = [], None

    def flush():
        if run:
            span = f"L{run[1]}" if run[1] == run[2] else f"L{run[1]}-{run[2]}"
            out.append(f"{indent}{cmt} ... {span} {'shown in the diff' if run[0] == 'diff' else 'elided'}")

    for ln, text in numbered:
        if ln is None or ln < body_start or (ln not in visible and ln in near):
            flush()
            run = None
            out.append(text)
            continue
        kind = "diff" if ln in visible else "far"
        if run and run[0] == kind:
            run[2] = ln
        else:
            flush()
            run = [kind, ln, ln]
    flush()
    return out


def callable_node(d):
    if d.kind != "class":
        return d
    return next((c for c in d.children if c.name == d.lang.init_name), None)


def changed_params(repo, base, d, lines):
    c = callable_node(d)
    if c is None:
        return set(), []
    pos, names = d.lang.params(c.node)
    words = set(re.findall(r"\w+", "\n".join(repo.sources[d.path][ln - 1] for ln in lines if ln <= d.end)))
    bd = base.def_named(d.path, d.qualname)
    if bd is not None:
        bc = callable_node(bd)
        if bc is not None:
            names = names + d.lang.params(bc.node)[1]
        words |= set(re.findall(r"\w+", "\n".join(base.deleted_text(d.path, bd.start, bd.end))))
    return set(names) & words, pos


def arg_relevance(c, name, cparams, positional):
    if not cparams:
        return 0
    lang = c.lang
    idx = [i for i, p in enumerate(positional) if p in cparams]
    for n in walk(c.node):
        if n.type in lang.call_types and call_name(lang, n) == name:
            pos, kws, star = lang.call_args(n)
            if any(k is None or k in cparams for k, _v in kws):
                return 1
            if star or any(i < len(pos) for i in idx):
                return 1
    return 0


def reference_kinds(c, name):
    lang = c.lang
    kinds = set()
    for n in walk(c.node):
        if n.type in lang.call_types:
            if call_name(lang, n) == name:
                kinds.add("calls")
            pos, kws, _star = lang.call_args(n)
            for a in pos + [v for _k, v in kws]:
                if a is None:
                    continue
                if (a.type in lang.ident and a.text == name) or (a.type in lang.attr_types
                                                                 and lang.attr_parts(a)[1] is not None
                                                                 and lang.attr_parts(a)[1].text == name):
                    kinds.add("passes it as an argument")
    if name in c.strrefs:
        kinds.add("getattr string")
    return sorted(kinds) or ["references"]


def reference_lines(c, name):
    lang = c.lang
    out = set()
    for n in walk(c.node):
        if (n.type in lang.ident and n.text == name and (lang.is_ref(n))) or (
                n.type in lang.string_types and lang.string_value(n) == name):
            out.add(n.sl)
    return out


def base_names(cls):
    return {n for n in cls.lang.bases(cls.node) if n not in cls.lang.builtins and n not in GENERIC_BASES}


def find_siblings(repo, d, changed_keys):
    lang = d.lang
    if d.name in lang.common or d.name.startswith("__") or is_test_path(d.path):
        return []
    here, out = os.path.dirname(d.path), []
    if d.kind == "method":
        cls = d.parent
        mine = base_names(cls)
        for m in repo.by_name.get(d.name, []):
            if m.kind != "method" or m.key == d.key or m.key in changed_keys or is_test_path(m.path) \
                    or m.lang is not lang:
                continue
            k = m.parent
            theirs = base_names(k)
            if mine & theirs:
                rel = f"shares base `{min(mine & theirs)}`"
            elif k.name in mine:
                rel = "base class"
            elif cls.name in theirs:
                rel = "subclass"
            elif os.path.dirname(m.path) == here and m.path != d.path:
                rel = "sibling module"
            else:
                continue
            out.append((rel, m))
    elif d.kind == "function" and d.parent is None:
        out = [("sibling module", m) for m in repo.by_name.get(d.name, [])
               if m.kind == "function" and m.parent is None and m.path != d.path and m.key not in changed_keys
               and os.path.dirname(m.path) == here and not is_test_path(m.path) and m.lang is lang]
    out.sort(key=lambda x: (x[0] == "sibling module", x[1].path, x[1].start))
    return out


def line_tokens(text, lang):
    return {t for t in re.findall(r"[A-Za-z_]\w{2,}", text) if t not in lang.keywords}


def shapes_on(pindex, key, node, lines, lang, min_size=3):
    out = set()
    for n in walk(node):
        if n.type in lang.pattern_types and n.sl in lines:
            s, size, _an = pindex.canon(key, n, lang)
            if s and size >= min_size:
                out.add(s)
    return out


def sibling_anchors(repo, pindex, m, added_shapes, removed, token_sets):
    lang = m.lang
    marks = {}
    for n in walk(m.node):
        if n.type in lang.pattern_types:
            s, size, _an = pindex.canon(m.path, n, lang)
            if not s or size < 3:
                continue
            if s in removed:
                marks[n.sl] = f"  {cmt_of(lang)} <<< same shape as a line this PR removed"
            elif s in added_shapes:
                marks.setdefault(n.sl, f"  {cmt_of(lang)} <<< same shape as a line this PR added")
    src = repo.sources[m.path]
    tok_hits = set()
    for ln in range(m.start, m.end + 1):
        if ln in marks:
            continue
        t = line_tokens(src[ln - 1], lang)
        if len(t) >= 2 and any(len(t & u) / len(t | u) >= 0.5 for u in token_sets):
            tok_hits.add(ln)
    return marks, tok_hits


def cmt_of(lang):
    return "#" if lang.name == "python" else "//"


# ---------------------------------------------------------------- non-code siblings

TOML_HDR = re.compile(r"^\s*\[\[?([^\]]+)\]")
YAML_KEY = re.compile(r"^([A-Za-z0-9_][\w.\-]*)\s*:")
MD_HDR = re.compile(r"^(#{1,6})\s+(.+)$")


def file_sections(lines, suffix):
    heads, in_str, in_fence = [], False, False
    for i, line in enumerate(lines, 1):
        if suffix == ".toml":
            if not in_str:
                m = TOML_HDR.match(line)
                if m:
                    heads.append((m.group(1).strip(), i))
            if (line.count('"""') + line.count("'''")) % 2:
                in_str = not in_str
        elif suffix in (".yaml", ".yml"):
            m = YAML_KEY.match(line)
            if m:
                heads.append((m.group(1), i))
        else:
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            m = None if in_fence else MD_HDR.match(line)
            if m:
                heads.append((m.group(2).strip().rstrip("#").strip(), i))
    out = []
    if heads and heads[0][1] > 1:
        out.append(("<top>", 1, heads[0][1] - 1))
    for k, (name, start) in enumerate(heads):
        out.append((name, start, heads[k + 1][1] - 1 if k + 1 < len(heads) else len(lines)))
    return out or [("<top>", 1, len(lines))]


def read_lines(root, rel):
    text = HEAD.read_text(rel)
    return None if text is None else text.splitlines()


def noncode_siblings(root, path, info, base):
    suffix = os.path.splitext(path)[1]
    group = (".yaml", ".yml") if suffix in (".yaml", ".yml") else (suffix,)
    lines = read_lines(root, path)
    if lines is None:
        return []
    secs = file_sections(lines, suffix)
    changed_lines = info["added"] | info["del_anchors"]
    touched = {n for n, a, b in secs if any(a <= x <= b for x in changed_lines)}
    keys = {n for n, _a, _b in secs} - {"<top>"}
    added = [lines[x - 1].strip() for x in sorted(info["added"]) if x <= len(lines)]
    removed = [t.strip() for t in base.deleted_text(path)]
    both = set(added) & set(removed)
    probes = [(t, "removed") for t in removed if len(t) >= 6 and t not in both][:NONCODE_PROBES // 2]
    probes += [(t, "added") for t in added if len(t) >= 6 and t not in both][:NONCODE_PROBES - len(probes)]
    near = sorted({x for c in changed_lines for x in range(c - 3, c + 4)} - info["added"])
    ctx = list(dict.fromkeys(lines[x - 1].strip() for x in near if 1 <= x <= len(lines)))
    freq = {}
    for t in lines:
        freq[t.strip()] = freq.get(t.strip(), 0) + 1
    probes += [(t, "context") for t in ctx if len(t) >= 12 and freq.get(t) == 1 and t not in both][:NONCODE_PROBES // 2]
    folder = os.path.dirname(path)
    names = sorted(f for f in HEAD.listdir(folder) if os.path.splitext(f)[1] in group)
    stem = os.path.splitext(os.path.basename(path))[0]
    ranked = sorted(((difflib.SequenceMatcher(None, stem, os.path.splitext(f)[0]).ratio(), f) for f in names
                     if f != os.path.basename(path)), key=lambda x: (-x[0], x[1]))[:40]
    out = []
    for name_sim, f in ranked:
        rel = f"{folder}/{f}" if folder else f
        sib = read_lines(root, rel)
        if not sib:
            continue
        ssecs = file_sections(sib, os.path.splitext(f)[1])
        skeys = {n for n, _a, _b in ssecs} - {"<top>"}
        overlap = len(keys & skeys) / len(keys) if keys else 0.0
        if name_sim < 0.6 and overlap < 0.5:
            continue
        spans = [(a, b) for n, a, b in ssecs if n != "<top>" and (n in touched or any(
            difflib.SequenceMatcher(None, n, t).ratio() >= 0.75 for t in touched))]
        region = [x for a, b in spans for x in range(a, b + 1)] or list(range(1, len(sib) + 1))
        stripped = {x: sib[x - 1].strip() for x in region}
        marks, counts = {}, {"removed": 0, "added": 0, "context": 0, "similar": 0}
        label = {"removed": "a line this PR removed", "added": "a line this PR added",
                 "context": "unchanged context next to the change"}
        for text, kind in probes:
            exact = [x for x, s in stripped.items() if s == text]
            if len(exact) > 2:
                continue
            if exact:
                for x in exact:
                    marks.setdefault(x, f"   <<< same as {label[kind]}")
                counts[kind] += 1
                continue
            sm, best = difflib.SequenceMatcher(None, autojunk=False), (0.75, None)
            sm.set_seq2(text)
            for x, s in stripped.items():
                sm.set_seq1(s)
                if s and sm.real_quick_ratio() >= best[0] and sm.quick_ratio() >= best[0]:
                    r = sm.ratio()
                    if (best[1] is None and r >= best[0]) or r > best[0]:
                        best = (r, x)
            if best[1] is not None and kind != "context":
                marks.setdefault(best[1], f"   <<< similar to {label[kind]}")
                counts["similar"] += 1
        if not marks and not spans:
            continue
        anchors = sorted(marks) or [a for a, _b in spans][:1]
        body = snippet(sib, anchors, 3, NONCODE_MAX_LINES, marks=marks)
        secnames = sorted({n for n, a, b in ssecs if (a, b) in spans})
        out.append(dict(path=rel, sections=secnames, counts=counts, overlap=overlap,
                        name_sim=name_sim, body=body,
                        score=3 * counts["removed"] + counts["added"] + counts["context"] + counts["similar"]))
    out.sort(key=lambda x: (-x["score"], -x["overlap"], -x["name_sim"], x["path"]))
    return out[:NONCODE_SIBLINGS]


# ---------------------------------------------------------------- tests, importers, cycles

def test_owner(t):
    best, p = t, t
    while p is not None:
        if p.kind != "class" and p.name.startswith("test"):
            best = p
        p = p.parent
    return best


def find_cycle(graph, start, goal):
    prev, queue = {start: None}, [start]
    while queue:
        nxt = []
        for u in queue:
            for v in sorted(graph.get(u, ())):
                if v in prev:
                    continue
                prev[v] = u
                if v == goal:
                    path = [v]
                    while prev[path[-1]] is not None:
                        path.append(prev[path[-1]])
                    return path[::-1]
                nxt.append(v)
        queue = nxt
    return None


def default_scope(code_changed):
    """Top-level directories holding the changed non-test code files (else every non-test code file)."""
    tops = sorted({p.split("/")[0] + "/" if "/" in p else "" for p in code_changed if not is_test_path(p)})
    return tops if tops and "" not in tops else [""]


def build(args):
    global HEAD
    HEAD = HeadTree(args.repo, args.head)
    try:
        return _build(args)
    finally:
        HEAD.close()


def _build(args):
    mb, files = parse_diff(args.repo, args.base, args.head)
    changed_langs = [lg for lg in LANGS if any(p.endswith(lg.suffixes) for p in files)] or LANGS[:1]
    repo = Repo(args.repo, changed_langs)
    gx = GraphifyCalls(args.graphify, repo) if args.graphify else None
    base = BaseSide(args.repo, mb, parse_deletions(args.repo, mb, args.head))
    by_path = {}
    for d in repo.defs:
        by_path.setdefault(d.path, []).append(d)
    code_changed = {p: v for p, v in files.items() if lang_for(p) in changed_langs and v["status"] != "D"}
    other = sorted((p, v["status"]) for p, v in files.items() if p not in code_changed)
    scope = [s if s.endswith("/") or not s else s + "/" for s in args.scope] if args.scope \
        else default_scope(code_changed)
    scope_desc = ", ".join(f"{s}" for s in scope if s) or "the repository"
    changed, module_level, unparsable, rows = map_changes(repo, code_changed)
    changed_keys = set(changed)
    changed_order = sorted(changed.values(), key=lambda v: (is_test_path(v[0].path), v[0].path, v[0].start))
    changed_defs = [v[0] for v in changed_order]
    added_by_path = {p: v["added"] for p, v in code_changed.items()}
    visible = {p: diff_view(v) for p, v in code_changed.items()}
    pindex = PatternIndex(repo, scope)

    sections = []
    seen_imp = set()
    for d, lines in changed_order:
        hdr = imports_header(repo, d.path, seen_imp)
        cl = sorted(lines)
        nonblank = {d.start + i for i, s in enumerate(repo.lines(d.path, d.start, d.end)) if s.strip()}
        whole_new = nonblank <= set(cl)
        body_start = max(d.body_line, d.start + 1)
        indent = re.match(r"\s*", repo.sources[d.path][min(body_start, d.end) - 1]).group(0)
        title = (f"### {d.kind} `{d.path}::{d.qualname}` (L{d.start}-{d.end}; changed lines {compress(cl)}"
                 f"{'; wholly added' if whole_new else ''})\n")
        num = numbered_lines(repo, d)
        texts = {}
        for w in CHANGED_WINDOWS:
            note = " (signature + body lines outside the diff" + (f", within {w} lines of a change)" if w else
                                                                 "; body lines elided)")
            texts[w] = (hdr if w else "") + title + code_block(
                outside_diff(num, body_start, visible[d.path], cl, w, indent, cmt_of(d.lang)), d.start, note=note,
                lang=d.lang.fence)
        sections.append(dict(kind="changed", order=(is_test_path(d.path), d.path, d.start), test=is_test_path(d.path),
                             label=d.key, texts=texts))
    for path, lines in sorted(module_level.items()):
        lang = repo.langs[path]
        hdr = imports_header(repo, path, seen_imp)
        ls = sorted(lines)
        stmts = [n for n in repo.trees[path].named_children if any(n.sl <= x <= n.el for x in ls)]
        title = f"### module-level changes in `{path}` (lines {compress(ls)})\n"
        texts = {}
        for w in CHANGED_WINDOWS:
            body = []
            for n in stmts:
                num = [(x, repo.sources[path][x - 1]) for x in range(n.sl, n.el + 1)]
                body.extend(outside_diff(num, n.sl, visible[path], ls, w, "", cmt_of(lang)))
            texts[w] = (hdr if w else "") + (title + (code_block(body, stmts[0].sl, note=" (statements with "
                                                                 "changes; lines outside the diff)", lang=lang.fence)
                                                      if body else ""))
        sections.append(dict(kind="changed", order=(is_test_path(path), path, min(ls)), test=is_test_path(path),
                             label=f"{path}::<module>", texts=texts))

    const_seen = set()
    for d in changed_defs:
        for path, name, (node, is_call) in referenced_constants(repo, d):
            key = f"{path}::{name}"
            if key in const_seen:
                continue
            const_seen.add(key)
            start, end = node.sl, node.el
            lines = repo.lines(path, start, end)
            in_pr = bool(set(range(start, end + 1)) & added_by_path.get(path, set()))
            title = (f"#### constant `{key}` (L{start}-{end}; read by `{d.qualname}`"
                     f"{'; changed in this PR' if in_pr else ''})\n")
            fl = repo.langs[path].fence
            sections.append(dict(
                kind="const", label=key, text=title + code_block(lines, start, max_lines=CONST_MAX_LINES, lang=fl),
                stub=title + code_block(lines, start, max_lines=5, lang=fl) if len(lines) > 5 else None,
                rank=(is_test_path(d.path), 0, distance(d.path, path), 0, is_call, key)))

    callee_seen = set()
    for d in changed_defs:
        for recv, name in sorted(d.calls, key=lambda x: (x[0] or "", x[1])):
            hits, via = gx.callees(d, name) if gx and gx.has(d) else repo.resolve_call(d, recv, name)
            for t in hits:
                if t.key in changed_keys or t.key in callee_seen:
                    continue
                callee_seen.add(t.key)
                near = via in ("local", "import", "self") or t.path == d.path
                title = (f"#### `{t.path}::{t.qualname}` (called as `{(recv + '.') if recv else ''}{name}` "
                         f"from `{d.qualname}`)\n")
                fl = t.lang.fence
                stub = title + code_block(signature_and_doc(repo, t), t.start, note=" (signature and docstring)",
                                          lang=fl)
                src = class_skeleton(repo, t) if t.kind == "class" else repo.lines(t.path, t.start, t.end)
                full = title + code_block(src, t.start, max_lines=CALLEE_FULL_LINES,
                                          note=" (class; method bodies elided)" if t.kind == "class" else "", lang=fl)
                sections.append(dict(
                    kind="callee", label=t.key, text=full if near else stub, stub=stub if near else None,
                    rank=(is_test_path(d.path), 0 if near else 1, distance(d.path, t.path), 1, False, t.key)))

    caller_seen, caller_total, skipped_names, hub_lines, hubs = set(), 0, set(), [], []
    for d, lines in changed_order:
        lang = d.lang
        name = d.parent.name if (d.name == lang.init_name and d.parent is not None) else d.name
        if name in lang.common or name.startswith("__"):
            skipped_names.add(name)
            continue
        from_test = is_test_path(d.path)
        if gx and gx.has(d):
            callers = [c for c in gx.callers(d) if c.key not in changed_keys and not is_test_path(c.path)]
        else:
            callers = [c for c in repo.defs if c.key not in changed_keys and not is_test_path(c.path)
                       and (name in c.refs or name in c.strrefs)]
        hub = len(callers) > HUB_THRESHOLD
        cparams, positional = changed_params(repo, base, d, lines)
        argrel = {c.key: arg_relevance(c, name, cparams, positional) for c in callers}
        rel = {c.key: caller_relevance(c, name) for c in callers} if hub else {}
        callers.sort(key=lambda c: (-argrel[c.key], -rel.get(c.key, 0), distance(d.path, c.path), c.key))
        if hub:
            per_file = {}
            for c in callers:
                per_file[c.path] = per_file.get(c.path, 0) + 1
            files_sorted = sorted(per_file.items(), key=lambda x: (-x[1], x[0]))
            listed = ", ".join(f"`{p}` ({n})" for p, n in files_sorted[:HUB_FILES_LISTED])
            extra = len(files_sorted) - HUB_FILES_LISTED
            more = f", +{extra} more files" if extra > 0 else ""
            n_rel, n_arg = sum(rel.values()), sum(argrel.values())
            params = f" (`{'`, `'.join(sorted(cparams))}`)" if cparams else ""
            hub_lines.append(f"- Hub `{d.qualname}`: {len(callers)} inbound callers in {len(files_sorted)} files; "
                             f"{n_arg} pass parameters whose handling this PR changes{params}, {n_rel} use the "
                             f"returned value. Up to {HUB_KEEP} shown, ranked by changed-parameter use, return-value "
                             f"use, then proximity. Files: {listed}{more}.\n")
            hubs.append(dict(symbol=d.key, callers=len(callers), files=len(files_sorted), inspecting=n_rel,
                             passing_changed_params=n_arg, changed_params=sorted(cparams)))
            callers = callers[:HUB_KEEP]
        caller_total += len(callers)
        for c in callers:
            if c.key in caller_seen:
                continue
            caller_seen.add(c.key)
            dist = distance(d.path, c.path)
            tags = reference_kinds(c, name)
            if argrel[c.key]:
                tags.append("passes a changed parameter")
            if rel.get(c.key):
                tags.append("uses returned value")
            title = f"#### `{c.path}::{c.qualname}` references `{name}` (distance {dist}; {', '.join(tags)})\n"
            fl = c.lang.fence
            if c.kind == "class":
                src, start, note = class_skeleton(repo, c), c.start, " (class; method bodies elided)"
                text = title + code_block(src, start, max_lines=CALLER_MAX_LINES, note=note, lang=fl)
                stub = None
            else:
                src = repo.lines(c.path, c.start, c.end)
                refs = reference_lines(c, name)
                if len(src) <= CALLER_FULL_LINES:
                    text = title + code_block(src, c.start, lang=fl)
                else:
                    text = title + code_block(window(src, c.start, refs, ctx=CALLER_WINDOW, cmt=cmt_of(c.lang)),
                                              c.start, max_lines=CALLER_MAX_LINES,
                                              note=" (windowed around the references)", lang=fl)
                stub = title + code_block(window(src, c.start, refs, ctx=2, cmt=cmt_of(c.lang)), c.start,
                                          max_lines=20, note=" (windowed around the references)", lang=fl)
                if len(stub) >= len(text):
                    stub = None
            sections.append(dict(kind="caller", label=c.key, text=text, stub=stub,
                                 rank=(from_test, hub, -argrel[c.key], -rel.get(c.key, 0), dist, c.key)))

    added_shapes = {}
    for path, info in code_changed.items():
        if repo.trees.get(path) is not None:
            for s in shapes_on(pindex, path, repo.trees[path], info["added"], repo.langs[path], PATTERN_MIN_SIZE):
                added_shapes.setdefault(s, path)
    removed_cands = []
    for path, tree in sorted(base.tree.items()):
        lang = base.lang[path]
        if lang not in changed_langs:
            continue
        key = "@base/" + path
        pindex.kept_override[key] = lang.builtins | lang.module_bindings(tree)[1] | lang.self_names
        for n in walk(tree):
            if n.type not in lang.pattern_types or n.sl not in base.deleted[path]:
                continue
            s, size, anchors = pindex.canon(key, n, lang)
            if s and size >= PATTERN_MIN_SIZE and anchors and s not in added_shapes:
                removed_cands.append((size, s, path, n))
    removed_shapes = {s for _size, s, _p, _n in removed_cands}

    tests_skipped = []
    sib_found = 0
    for f in [x for x in changed_defs if x.kind in ("function", "method") and not is_test_path(x.path)]:
        sibs = find_siblings(repo, f, changed_keys)
        if not sibs:
            continue
        sib_found += len(sibs)
        lang = f.lang
        flines = changed[f.key][1]
        fshapes = shapes_on(pindex, f.path, f.node, flines, lang)
        fremoved = set()
        bd = base.def_named(f.path, f.qualname)
        if bd is not None and base.tree.get(f.path) is not None:
            fremoved = shapes_on(pindex, "@base/" + f.path, bd.node, base.deleted.get(f.path, set()), lang)
        token_sets = [t for t in (line_tokens(repo.sources[f.path][x - 1], lang) for x in flines) if len(t) >= 2]
        token_sets += [t for t in (line_tokens(x, lang) for x in (base.deleted_text(f.path, bd.start, bd.end)
                                                                  if bd else [])) if len(t) >= 2]
        scored = []
        for relation, m in sibs:
            marks, tok_hits = sibling_anchors(repo, pindex, m, fshapes, fremoved, token_sets)
            n_old = sum(1 for v in marks.values() if "removed" in v)
            scored.append((relation, m, marks, tok_hits, n_old, len(marks) - n_old))
        scored.sort(key=lambda x: (x[0] == "sibling module", -(3 * x[4] + 2 * x[5] + len(x[3])), x[1].path))
        for i, (relation, m, marks, tok_hits, n_old, n_new) in enumerate(scored[:SIBLINGS_PER_SYMBOL]):
            src = repo.sources[m.path]
            anchors = sorted(set(marks) | tok_hits)
            body0 = m.body_line
            sig = [f"{x:>5}| {src[x - 1]}" for x in range(m.start, body0)][:6]
            body = snippet(src, anchors, 4, SIBLING_MAX_LINES, lo=body0, hi=m.end, marks=marks)
            if sig and body and not body[0].startswith(f"{body0:>5}|"):
                sig.append("  ...")
            title = (f"#### `{m.path}::{m.qualname}` ({relation}; same {'method' if m.kind == 'method' else 'function'}"
                     f" as changed `{f.qualname}`; {n_old} lines with a shape this PR removed, {n_new} with a shape "
                     f"it added, {len(tok_hits)} sharing identifiers)\n")
            fl = m.lang.fence
            if not anchors:
                sections.append(dict(kind="sibling", label=m.key, text=title + fence(sig[:3], fl), stub=None,
                                     rank=(False, 2, 1, f.key, i)))
                continue
            stub = title + fence(sig[:3] + body[:8], fl)
            sections.append(dict(kind="sibling", label=m.key, text=title + fence(sig + body, fl), stub=stub,
                                 rank=(False, 0 if relation != "sibling module" or n_old else 1, 1, f.key, i)))

    for path, status in other:
        if status == "D" or not path.endswith(NONCODE_SUFFIXES):
            continue
        for i, sib in enumerate(noncode_siblings(args.repo, path, files[path], base)):
            c = sib["counts"]
            secs = ", ".join(f"`{s}`" for s in sib["sections"]) or "whole file"
            title = (f"#### `{sib['path']}` (sibling of changed `{path}`; sections {secs}; {c['removed']} lines "
                     f"identical to lines the PR removed, {c['added']} identical to lines it added, "
                     f"{c['context']} identical to unchanged context next to the change, {c['similar']} similar)\n")
            lang = {".md": "markdown", ".toml": "toml"}.get(os.path.splitext(path)[1], "yaml")
            sections.append(dict(kind="noncode", label=f"{path}~{sib['path']}", text=title + fence(sib["body"], lang),
                                 stub=title + fence(sib["body"][:8], lang) if len(sib["body"]) > 8 else None,
                                 rank=(False, 0, 2, path, i)))

    removed_cands.sort(key=lambda x: (-x[0], x[2], x[3].sl, x[3].sc))
    used, covered, shown_shapes = set(), set(), 0
    for _size, s, path, n in removed_cands:
        if shown_shapes >= REMOVED_SHAPES:
            break
        if s in used or id(n) in covered:
            continue
        sites = list(dict.fromkeys(pindex.sites.get(s, [])))
        if not 0 < len(sites) <= PATTERN_MAX_MATCHES:
            continue
        used.add(s)
        covered.update(id(x) for x in walk(n))
        shown_shapes += 1
        sites.sort(key=lambda x: (distance(path, x[0]), x[0], x[1]))
        shown = sites[:REMOVED_SITES]
        bdef = enclosing(base.defs.get(path, []), n.sl)
        old_line = base.src[path][n.sl - 1].strip()[:160]
        title = (f"#### shape `{pindex.display('@base/' + path, n, base.lang[path])}` removed from "
                 f"`{path}::{bdef.qualname if bdef else '<module>'}` (base L{n.sl}; {len(sites)} sites in "
                 f"{scope_desc} still use it; {len(shown)} shown)\nRemoved line: `{old_line}`\n")
        body = "".join(site_block(repo, by_path, p, ln) for p, ln in shown)
        stub = title + "".join(f"- `{p}:{ln}` in `{site_owner(by_path, p, ln)}`\n" for p, ln in shown)
        sections.append(dict(kind="removed", label=f"{path}@L{n.sl}", text=title + body, stub=stub,
                             rank=(False, 0, 0, path, n.sl)))

    funcs = [d for d in changed_defs if d.kind in ("function", "method")]
    similar = find_similar(repo, funcs, changed_keys, scope)
    sim_seen = set()
    for f in funcs:
        for _score, ratio, jac, c in similar.get(f.key, []):
            if c.key in sim_seen:
                continue
            sim_seen.add(c.key)
            title = (f"#### `{c.path}::{c.qualname}` similar to `{f.qualname}` "
                     f"(body ratio {ratio:.2f}, name Jaccard {jac:.2f})\n")
            fl = c.lang.fence
            stub = title + code_block(signature_and_doc(repo, c), c.start, note=" (signature only)", lang=fl)
            if ratio >= SIMILAR_FULL_RATIO:
                text = title + code_block(repo.lines(c.path, c.start, c.end), c.start, max_lines=SIMILAR_MAX_LINES,
                                          lang=fl)
            else:
                text, stub = stub, None
            sections.append(dict(kind="similar", label=c.key, text=text, stub=stub,
                                 rank=(is_test_path(f.path), 1 if ratio >= SIMILAR_FULL_RATIO else 2, 3,
                                       f.key, c.key)))
    pattern_sites = 0
    for i, (f, n, _s, _size, sites) in enumerate(same_pattern(repo, pindex, [x for x in funcs
                                                                          if not is_test_path(x.path)],
                                                             added_by_path)):
        shown = sites[:PATTERN_SITES]
        pattern_sites += len(shown)
        line = repo.sources[f.path][n.sl - 1].strip()[:160]
        title = (f"#### `{f.path}::{f.qualname}` L{n.sl} shape `{pindex.display(f.path, n, f.lang)}` "
                 f"({len(sites)} other sites; {len(shown)} shown)\nChanged line: `{line}`\n")
        body = "".join(site_block(repo, by_path, p, ln) for p, ln in shown)
        stub = title + "".join(f"- `{p}:{ln}` in `{site_owner(by_path, p, ln)}`\n" for p, ln in shown)
        sections.append(dict(kind="pattern", label=f"{f.key}@L{n.sl}", text=title + body, stub=stub,
                             rank=(False, 1, 4, f.key, i)))

    joined, importers_of = {}, {}
    for i, path in enumerate(sorted(p for p in code_changed if not is_test_path(p) and repo.trees.get(p) is not None)):
        lang = repo.langs[path]
        mod = lang.module_of(path)
        stem = os.path.basename(mod).rsplit(".", 1)[-1]
        strings = lang.module_path_strings(path)
        importers, loaders = [], []
        for rel in sorted(repo.sources):
            if rel == path:
                continue
            if rel not in joined:
                joined[rel] = "\n".join(repo.sources[rel])
            if stem not in joined[rel]:
                continue
            if any(t == path for t, _a, _b in repo.import_edges(rel, top_only=False)):
                importers.append(rel)
            elif any(s in joined[rel] for s in strings):
                loaders.append(rel)
        importers_of[path] = set(importers) | set(loaders)
        tests_n = sum(1 for r in importers + loaders if is_test_path(r))
        code = [r for r in importers if not is_test_path(r)]
        listed = ", ".join(f"`{r}`" for r in code[:IMPORTERS_LISTED])
        more = f", +{len(code) - IMPORTERS_LISTED} more" if len(code) > IMPORTERS_LISTED else ""
        by_str = [r for r in loaders if not is_test_path(r)]
        strs = f"; loaded by module-path string in {', '.join(f'`{r}`' for r in by_str)}" if by_str else ""
        text = (f"- `{path}` (`{mod}`): imported by {len(code)} non-test files{': ' + listed if listed else ''}{more}"
                f"{strs}; {tests_n} test files\n")
        sections.append(dict(kind="importers", label=path, text=text, stub=None, rank=(1, i)))

    test_defs = [t for t in repo.defs if is_test_path(t.path) and t.kind in ("function", "method")]
    for i, d in enumerate(x for x in changed_defs if not is_test_path(x.path)):
        lang = d.lang
        name = d.parent.name if (d.name == lang.init_name and d.parent is not None) else d.name
        if name in lang.common or name.startswith("__"):
            tests_skipped.append(name)
            continue
        owners = {}
        for t in test_defs:
            if name in t.refs or name in t.strrefs or name in t.dotrefs:
                o = test_owner(t)
                owners[o.key] = o
        near = importers_of.get(d.path, set())
        strong = sorted((o for o in owners.values() if o.path in near), key=lambda o: (o.path, o.start))
        weak = len(owners) - len(strong)
        shown = strong or sorted(owners.values(), key=lambda o: (o.path, o.start))
        listed = ", ".join(f"`{o.path}::{o.qualname}` L{o.start}" for o in shown[:TESTS_PER_SYMBOL])
        more = f", +{len(shown) - TESTS_PER_SYMBOL} more" if len(shown) > TESTS_PER_SYMBOL else ""
        if strong:
            text = (f"{len(strong)} test functions in files importing `{d.path}` reference `{name}`: {listed}{more}"
                    + (f"; {weak} more elsewhere reference the same name" if weak else ""))
        elif owners:
            text = (f"no test file importing `{d.path}` references `{name}`; {len(owners)} test functions elsewhere "
                    f"use the same name (possibly other implementations): {listed}{more}")
        else:
            text = f"no test function references `{name}`"
        sections.append(dict(kind="tests", label=d.key, text=f"- `{d.path}::{d.qualname}`: {text}\n", stub=None,
                             rank=(0, i)))

    graph = {}
    for rel in repo.sources:
        if in_scope(rel, scope) and not is_test_path(rel):
            graph[rel] = {t for t, _a, _b in repo.import_edges(rel, top_only=True) if in_scope(t, scope) and t != rel}
    cycle_lines, checked = [], 0
    for path in sorted(p for p in code_changed if p in graph):
        added = added_by_path[path]
        for t, a, b in repo.import_edges(path, top_only=True):
            if t == path or not in_scope(t, scope) or not any(a <= x <= b for x in added):
                continue
            checked += 1
            cyc = find_cycle(graph, t, path)
            if cyc:
                cycle_lines.append(f"- `{path}` L{a} adds an import of `{t}`, closing the cycle "
                                   + " -> ".join(f"`{x}`" for x in [path] + cyc) + "\n")
    cycles_text = "".join(cycle_lines) or f"none ({checked} added module-level imports within {scope_desc} checked)\n"

    header = (f"# Code-graph context for PR #{args.pr}\n\n"
              f"Untrusted, machine-generated context. Head `{args.head}`, base `{args.base}` (merge-base `{mb}`). "
              "Changed symbols show only the signature and body lines the diff does not show. Other sections: "
              "constants and callees they use, inbound callers, sibling implementations, sibling config/prompt/doc "
              f"files, sites still using a removed pattern, similar code and same-pattern sites in {scope_desc}, "
              "tests that reference the changed symbols, importers and circular imports.\n\n")
    nonpy = ""
    if other or unparsable:
        nonpy = ("## Changed files outside the parsed code\n" + "".join(f"- `{p}` ({s})\n" for p, s in other)
                 + "".join(f"- `{p}` (unparsable at head)\n" for p in unparsable) + "\n")
    if skipped_names:
        header += f"Caller search skipped for generic names: {', '.join(sorted(skipped_names))}.\n\n"

    text, summary, counts, sizes = assemble(header, nonpy, sections, hub_lines, cycles_text, scope_desc)
    os.makedirs(args.out_dir, exist_ok=True)
    name = args.name or str(args.pr)
    out_path = os.path.join(args.out_dir, f"pr{name}.G6.md")
    with open(out_path + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(out_path + ".tmp", out_path)  # a crash or timeout never leaves a partial bundle behind
    found = {k: sum(1 for s in sections if s["kind"] == k) for k in KINDS}
    manifest = dict(
        pr=args.pr, name=name, base=args.base, head=args.head, merge_base=mb, scope=scope,
        languages=[lg.name for lg in changed_langs], changed_code_files=len(code_changed),
        changed_other_files=len(other), chars=len(text), tokens=tok(text), found=found, included=counts,
        section_chars=sizes, callers_found_before_hub_trim=caller_total, hubs=hubs, pattern_sites_shown=pattern_sites,
        sibling_candidates=sib_found, removed_shapes=len(removed_shapes), circular_imports=len(cycle_lines),
        added_imports_checked=checked, caller_search_skipped=sorted(skipped_names),
        tests_search_skipped=sorted(set(tests_skipped)), cut=summary)
    print(json.dumps(manifest))
    return manifest


def site_owner(by_path, path, ln):
    d = enclosing(by_path.get(path, []), ln)
    return d.qualname if d else "<module>"


def site_block(repo, by_path, path, ln):
    return (f"- `{path}:{ln}` in `{site_owner(by_path, path, ln)}`\n"
            + fence(snippet(repo.sources[path], [ln], 1, 3), repo.langs[path].fence))


KINDS = ("changed", "const", "callee", "caller", "sibling", "noncode", "removed", "similar", "pattern", "tests",
         "importers")


def titles(scope_desc):
    return (("const", "## Module-level constants read by changed symbols\n"),
            ("callee", "## 1-hop callees (nearest with full body, others signature and docstring)\n"),
            ("caller", "## 1-hop inbound callers (non-test; direct, passed as argument or getattr string; hubs ranked "
                       "by changed-parameter use, then return-value use)\n"),
            ("sibling", "## Sibling implementations (same method in related classes or sibling modules)\n"),
            ("noncode", "## Sibling config/prompt/doc files (matching lines in sibling files)\n"),
            ("removed", "## Still uses the pattern this PR replaced\n"),
            ("similar", f"## Similar code elsewhere in {scope_desc}\n"),
            ("pattern", f"## Same pattern elsewhere in {scope_desc}\n"),
            ("tests", "## Tests that exercise the changed symbols\n"),
            ("importers", "## Imported by\n"))


def assemble(header, nonpy, sections, hub_lines, cycles_text, scope_desc):
    cut = {"stubbed": [], "windowed": [], "dropped": []}
    changed = [s for s in sections if s["kind"] == "changed"]
    budget = CHAR_CAP - len(header) - len(nonpy) - len(cycles_text) - 1500
    limit = int(budget * CHANGED_SHARE)
    level = {id(s): 0 for s in changed}

    def ctext(s):
        return s["texts"][CHANGED_WINDOWS[level[id(s)]]]

    def csize():
        return sum(len(ctext(s)) for s in changed)

    for group in ([s for s in changed if s["test"]], [s for s in changed if not s["test"]]):
        while csize() > limit:
            cands = [s for s in group if level[id(s)] < len(CHANGED_WINDOWS) - 1]
            if not cands:
                break
            s = max(cands, key=lambda s: len(ctext(s)))
            level[id(s)] += 1
    for s in changed:
        if level[id(s)]:
            cut["windowed"].append(f"changed:{s['label']}@w{CHANGED_WINDOWS[level[id(s)]]}")
    for s in sorted([s for s in changed if s["test"]], key=lambda s: -len(ctext(s))) + \
            sorted([s for s in changed if not s["test"]], key=lambda s: s["order"], reverse=True):
        if csize() <= limit:
            break
        changed.remove(s)
        cut["dropped"].append("changed:" + s["label"])
    changed.sort(key=lambda s: s["order"])
    hub_text = "".join(hub_lines)
    remaining = budget - csize() - len(hub_text)

    chosen = {}

    def pack(items, cap):
        used = 0
        for s in sorted(items, key=lambda s: s["rank"]):
            if used + len(s["text"]) <= cap:
                chosen[id(s)] = "full"
                used += len(s["text"])
            elif s["stub"] and used + len(s["stub"]) <= cap:
                chosen[id(s)] = "stub"
                used += len(s["stub"])
        return used

    def kind(*ks):
        return [s for s in sections if s["kind"] in ks]

    support = kind("const", "callee")
    retrieval = kind("sibling", "noncode", "removed", "similar", "pattern")
    callers, extras = kind("caller"), kind("tests", "importers")
    for items, share in ((support, SUPPORT_SHARE), (retrieval, RETRIEVAL_SHARE), (callers, CALLER_SHARE),
                         (extras, EXTRA_SHARE)):
        remaining -= pack(items, min(remaining, int(budget * share)))
    for items in (support, retrieval, callers, extras):
        for s in sorted(items, key=lambda s: s["rank"]):
            if chosen.get(id(s)) == "full":
                continue
            current = len(s["stub"]) if chosen.get(id(s)) == "stub" else 0
            if len(s["text"]) - current <= remaining:
                chosen[id(s)] = "full"
                remaining -= len(s["text"]) - current
            elif id(s) not in chosen and s["stub"] and len(s["stub"]) <= remaining:
                chosen[id(s)] = "stub"
                remaining -= len(s["stub"])
    for s in support + retrieval + callers + extras:
        state = chosen.get(id(s))
        if state is None:
            cut["dropped"].append(f"{s['kind']}:{s['label']}")
        elif state == "stub":
            cut["stubbed"].append(f"{s['kind']}:{s['label']}")

    def render(k):
        items = sorted((s for s in sections if s["kind"] == k and id(s) in chosen), key=lambda s: s["rank"])
        return "".join(s["text"] if chosen[id(s)] == "full" else s["stub"] for s in items) or "_none_\n"

    sizes = {"changed": csize()}
    out = header + nonpy
    out += ("## Changed symbols (signature + body lines outside the diff; the diff has the rest)\n"
            + "".join(ctext(s) for s in changed) + "\n")
    for k, title in titles(scope_desc):
        body = render(k)
        if k == "caller":
            body = hub_text + body
        sizes[k] = len(body)
        out += title + body + "\n"
    out += f"## Circular imports (module-level imports this PR adds within {scope_desc})\n" + cycles_text
    sizes["cycles"] = len(cycles_text)
    if len(out) > CHAR_CAP:
        out = out[:CHAR_CAP - 60] + "\n\n[bundle hard-truncated at character cap]\n"
        cut["dropped"].append("hard-truncated")
    counts = {"changed": len(changed)}
    for k in KINDS[1:]:
        items = [s for s in sections if s["kind"] == k]
        counts[k] = sum(1 for s in items if id(s) in chosen)
        counts[k + "_stub"] = sum(1 for s in items if chosen.get(id(s)) == "stub")
    counts["hub_summaries"] = len(hub_lines)
    summary = {k: len(v) for k, v in cut.items()}
    summary["items"] = cut
    return out, summary, counts, sizes


def compress(nums):
    if not nums:
        return ""
    out, s, p = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == p + 1:
            p = n
            continue
        out.append(f"{s}" if s == p else f"{s}-{p}")
        s = p = n
    out.append(f"{s}" if s == p else f"{s}-{p}")
    return ",".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--head", required=True)
    ap.add_argument("--pr", required=True, type=int)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--name", default=None, help="output basename suffix (default: the PR number)")
    ap.add_argument("--grammar-cache", default=None,
                    help="directory holding pre-downloaded grammars (tree-sitter-language-pack otherwise fetches "
                         "them from GitHub on first use)")
    ap.add_argument("--scope", nargs="*", default=None,
                    help="directories for similar code, same-pattern sites and cycles (default: top-level dirs of "
                         "the changed non-test code files)")
    ap.add_argument("--graphify", default=None,
                    help="Graphify output dir (holding graph.json): take callers and callees from its call edges")
    ap.add_argument("--generic", action="store_true",
                    help="use the generic tags-query class for every language, Python and JavaScript included")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                    help="wall-clock limit for the build in seconds, after the grammars are loaded")
    a = ap.parse_args()
    if a.grammar_cache:
        configure(PackConfig(cache_dir=a.grammar_cache))
    LANGS[:] = tags_langs([]) if a.generic else LANGS + tags_langs(LANGS)

    def _timeout(_signum, _frame):
        raise TimeoutError(f"build exceeded {a.timeout} s")

    signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(a.timeout)
    try:
        build(a)
    except Exception as e:  # one line and exit 2, no bundle: the caller runs a diff-only review instead
        print(f"review-context build failed: {type(e).__name__}: {str(e)[:300]}", file=sys.stderr)
        sys.exit(2)
