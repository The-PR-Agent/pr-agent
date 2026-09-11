"""Generate labelled single-site defects from real source files.

Eleven hand-written items cannot tell a 10-point recall change from noise - one item flipping is
a 9-point swing. This module turns the repository's own code into as many labelled defects as a
run can afford, each with an exact line label derived from the edit rather than written by hand.

Every operator rewrites one AST node that sits on a single line, using the node's column offsets
so the edit touches nothing else. The mutant must still parse, or it is discarded: a syntax
error is not a review-finding, it is a CI failure, and seeding one would inflate recall.

Mutants keep the real file path so the model sees real surrounding code. That is exactly the
case the runner's enrichment guard exists for - run it from outside the checkout.
"""

import ast
import difflib
import glob
import os
import random
from dataclasses import dataclass
from typing import Callable, Iterator

from tests.eval.corpus import SeededDefect


@dataclass(frozen=True)
class Mutation:
    operator: str
    defect_class: str
    summary: str
    signals: tuple[str, ...]
    start_line: int
    end_line: int
    source: str  # the whole mutated file


_FLIP_BOUNDARY = {ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">"}
_FLIP_INVERT = {ast.Eq: "!=", ast.NotEq: "==", ast.Is: "is not", ast.IsNot: "is",
                ast.Lt: ">=", ast.LtE: ">", ast.Gt: "<=", ast.GtE: "<"}
_OP_TEXT = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
            ast.Is: "is", ast.IsNot: "is not"}


def _one_line(node: ast.AST) -> bool:
    return getattr(node, "end_lineno", None) == getattr(node, "lineno", None)


def _replace_span(lines: list[str], lineno: int, start_col: int, end_col: int, text: str) -> list[str]:
    out = list(lines)
    line = out[lineno - 1]
    out[lineno - 1] = line[:start_col] + text + line[end_col:]
    return out


def _replace_between(lines: list[str], lineno: int, start_col: int, end_col: int,
                     old: str, new: str) -> list[str] | None:
    """Replace ``old`` inside one column window of one line; None when it is not there once."""
    segment = lines[lineno - 1][start_col:end_col]
    if segment.count(old) != 1:
        return None
    return _replace_span(lines, lineno, start_col, end_col, segment.replace(old, new))


def _compare_mutations(tree: ast.AST, lines: list[str], table: dict, operator: str,
                       defect_class: str, summary: str, signals: tuple[str, ...]) -> Iterator[Mutation]:
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1 and _one_line(node)):
            continue
        op = type(node.ops[0])
        if op not in table:
            continue
        left, right = node.left, node.comparators[0]
        mutated = _replace_between(lines, node.lineno, left.end_col_offset, right.col_offset,
                                   _OP_TEXT[op], table[op])
        if mutated is None:
            continue
        yield Mutation(operator, defect_class, summary.format(old=_OP_TEXT[op], new=table[op]),
                       signals, node.lineno, node.lineno, "".join(mutated))


def op_boundary(tree, lines):
    yield from _compare_mutations(
        tree, lines, _FLIP_BOUNDARY, "boundary", "boundary",
        "Comparison '{old}' changed to '{new}': the boundary value is now handled the wrong way.",
        ("off-by-one", "off by one", "boundary", "inclusive", "exclusive", "<=", ">=", "edge"))


def op_invert_comparison(tree, lines):
    yield from _compare_mutations(
        tree, lines, _FLIP_INVERT, "invert-comparison", "inverted-condition",
        "Comparison '{old}' negated to '{new}': the condition is now the opposite of intended.",
        ("inverted", "flipped", "reversed", "opposite", "negat", "wrong condition", "always", "never"))


def op_drop_none_guard(tree, lines):
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or node.orelse or len(node.body) != 1:
            continue
        test = node.test
        if not (isinstance(test, ast.Compare) and len(test.ops) == 1
                # `is not None` / `!= None` guards do the opposite job; dropping one is not a
                # null dereference and would carry a wrong class label
                and isinstance(test.ops[0], (ast.Is, ast.Eq))
                and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value is None):
            continue
        body = node.body[0]
        if not isinstance(body, (ast.Return, ast.Raise, ast.Continue, ast.Break)):
            continue
        start, end = node.lineno, body.end_lineno
        mutated = lines[:start - 1] + lines[end:]
        yield Mutation("drop-none-guard", "null-dereference",
                       "The None guard is removed, so a missing value reaches code that dereferences it.",
                       ("none", "null", "attributeerror", "typeerror", "guard", "missing", "unchecked",
                        "not exist", "may be"),
                       start, start, "".join(mutated))


def op_drop_await(tree, lines):
    for node in ast.walk(tree):
        value = getattr(node, "value", None)
        if not (isinstance(node, (ast.Expr, ast.Assign)) and isinstance(value, ast.Await) and _one_line(value)):
            continue
        inner = value.value
        mutated = _replace_between(lines, value.lineno, value.col_offset, inner.col_offset, "await ", "")
        if mutated is None:
            continue
        yield Mutation("drop-await", "missing-await",
                       "The coroutine is no longer awaited, so it never runs and any result is a coroutine object.",
                       ("await", "coroutine", "not awaited", "never executed", "never runs", "async"),
                       value.lineno, value.lineno, "".join(mutated))


def op_widen_except(tree, lines):
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ExceptHandler) and node.type is not None and _one_line(node.type)):
            continue
        if isinstance(node.type, ast.Name) and node.type.id in ("Exception", "BaseException"):
            continue
        if len(node.body) == 1 and isinstance(node.body[0], ast.Raise) and node.body[0].exc is None:
            continue  # `except X: raise` widened still re-raises; not a defect
        mutated = _replace_span(lines, node.type.lineno, node.type.col_offset, node.type.end_col_offset, "Exception")
        yield Mutation("widen-except", "silent-failure",
                       "The except clause now catches every exception, so unrelated failures are "
                       "handled as if expected.",
                       ("broad except", "bare except", "catches all", "swallow", "silent", "hides", "masks",
                        "too broad", "exception"),
                       node.type.lineno, node.type.lineno, "".join(mutated))


def op_drop_not(tree, lines):
    for node in ast.walk(tree):
        if not isinstance(node, (ast.If, ast.While)):
            continue
        test = node.test
        if not (isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and _one_line(test)):
            continue
        mutated = _replace_between(lines, test.lineno, test.col_offset, test.operand.col_offset, "not ", "")
        if mutated is None:
            continue
        yield Mutation("drop-not", "inverted-condition",
                       "The 'not' is removed, so the branch runs in exactly the cases it was guarding against.",
                       ("inverted", "flipped", "reversed", "opposite", "negat", "wrong condition", "not"),
                       test.lineno, test.lineno, "".join(mutated))


def op_swap_args(tree, lines):
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and len(node.args) >= 2 and _one_line(node)):
            continue
        a, b = node.args[0], node.args[1]
        if not (isinstance(a, ast.Name) and isinstance(b, ast.Name) and a.id != b.id):
            continue
        line = lines[node.lineno - 1]
        swapped = line[:a.col_offset] + b.id + line[a.end_col_offset:b.col_offset] + a.id + line[b.end_col_offset:]
        mutated = list(lines)
        mutated[node.lineno - 1] = swapped
        yield Mutation("swap-args", "swapped-arguments",
                       f"Arguments '{a.id}' and '{b.id}' are passed in the wrong order.",
                       ("swap", "order", "transposed", "reversed", "wrong argument", a.id.lower(), b.id.lower()),
                       node.lineno, node.lineno, "".join(mutated))


OPERATORS: dict[str, Callable] = {
    "boundary": op_boundary,
    "invert-comparison": op_invert_comparison,
    "drop-none-guard": op_drop_none_guard,
    "drop-await": op_drop_await,
    "widen-except": op_widen_except,
    "drop-not": op_drop_not,
    "swap-args": op_swap_args,
}


def mutations_for_source(source: str, operators=None) -> list[Mutation]:
    """Every valid mutation of one file, in a deterministic order."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines(keepends=True)
    found = []
    for name in sorted(operators or OPERATORS):
        for mutation in OPERATORS[name](tree, lines):
            if mutation.source == source:
                continue
            try:
                ast.parse(mutation.source)
            except SyntaxError:
                continue
            found.append(mutation)
    found.sort(key=lambda m: (m.start_line, m.operator))
    return found


def mutation_diff(path: str, original: str, mutated: str) -> str:
    body = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), mutated.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}", n=3))
    return f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n{body}"


def to_defect(path: str, original: str, mutation: Mutation) -> SeededDefect:
    return SeededDefect(
        id=f"mut-{mutation.operator}-{os.path.basename(path)}-L{mutation.start_line}",
        defect_class=mutation.defect_class,
        summary=mutation.summary,
        files=(path,),
        signals=mutation.signals,
        source=f"mutant:{mutation.operator}",
        diff_text=mutation_diff(path, original, mutation.source),
    )


DEFAULT_GLOBS = ("pr_agent/algo/*.py", "pr_agent/tools/*.py", "pr_agent/git_providers/*.py")


def generate_mutants(repo_root: str, count: int, seed: int, globs=DEFAULT_GLOBS,
                     operators=None) -> list[SeededDefect]:
    """``count`` mutants, balanced across operators, reproducible for a given seed.

    Balanced rather than uniform over candidates: comparisons vastly outnumber awaits, and an
    unbalanced sample would report "recall" that is mostly recall on flipped comparisons.
    """
    by_operator: dict[str, list[tuple[str, str, Mutation]]] = {}
    for pattern in globs:
        for path in sorted(glob.glob(os.path.join(repo_root, pattern))):
            if os.path.basename(path).startswith("__"):
                continue
            with open(path, encoding="utf-8") as fh:
                source = fh.read()
            rel = os.path.relpath(path, repo_root)
            for mutation in mutations_for_source(source, operators):
                by_operator.setdefault(mutation.operator, []).append((rel, source, mutation))

    rng = random.Random(seed)
    queues = {op: rng.sample(items, len(items)) for op, items in sorted(by_operator.items())}
    chosen: list[SeededDefect] = []
    while len(chosen) < count and any(queues.values()):
        for op in sorted(queues):
            if queues[op] and len(chosen) < count:
                rel, source, mutation = queues[op].pop()
                chosen.append(to_defect(rel, source, mutation))
    return chosen
