"""Make practice commits: small random edits to the pinned checkout, as unified diffs.

These are for checking the selector against ground truth (tools/label.py) on far
more changes than the 30 samples. Nothing here ships in the image.

Usage: python mutate.py <checkout> <out dir> <count> [seed]
"""

from __future__ import annotations

import ast
import difflib
import glob
import os
import random
import sys

COMMENTS = [
    "# Cheap check first", "# Kept for backwards compatibility", "# Intentionally not cached",
    "# Keep in sync with the generator", "# Mirrors the behavior of the reference implementation",
]
SWAP = {
    ast.Eq: "!=", ast.NotEq: "==", ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">",
    ast.Is: "==", ast.IsNot: "is", ast.In: "not in", ast.NotIn: "in",
}
TOKEN = {
    ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=",
    ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in",
}


def read(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def unified(rel, old, new):
    lines = list(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                      f"a/{rel}", f"b/{rel}", n=3))
    out = [f"diff --git a/{rel} b/{rel}\n"]
    for line in lines:
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def line_starts(text):
    starts = [0]
    for line in text.splitlines(True):
        starts.append(starts[-1] + len(line.encode("utf-8")))
    return starts


def code_sites(text):
    """(start byte, end byte, replacement, where) candidates for one-token edits."""
    tree = ast.parse(text)
    data = text.encode("utf-8")
    starts = line_starts(text)

    def pos(line, col):
        return starts[line - 1] + col

    sites = []
    scope = {}
    for parent in ast.walk(tree):
        inside = isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
        for child in ast.iter_child_nodes(parent):
            scope[child] = "func" if inside or scope.get(parent) == "func" else "body"

    for node in ast.walk(tree):
        where = scope.get(node, "body")
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left_end = pos(node.left.end_lineno, node.left.end_col_offset)
            right_start = pos(node.comparators[0].lineno, node.comparators[0].col_offset)
            gap = data[left_end:right_start].decode("utf-8")
            token = TOKEN.get(type(node.ops[0]))
            if token and gap.strip() == token and type(node.ops[0]) in SWAP:
                at = left_end + len(gap) - len(gap.lstrip())
                sites.append((at, at + len(token), SWAP[type(node.ops[0])], where, "compare"))
        elif isinstance(node, ast.BoolOp):
            token = "and" if isinstance(node.op, ast.And) else "or"
            other = "or" if token == "and" else "and"
            for a, b in zip(node.values, node.values[1:]):
                a_end = pos(a.end_lineno, a.end_col_offset)
                b_start = pos(b.lineno, b.col_offset)
                gap = data[a_end:b_start].decode("utf-8")
                index = gap.find(token)
                if index >= 0 and gap.replace(token, "", 1).strip(" \n\t()") == "":
                    sites.append((a_end + index, a_end + index + len(token), other, where, "boolop"))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            start = pos(node.lineno, node.col_offset)
            operand = pos(node.operand.lineno, node.operand.col_offset)
            if data[start:operand].decode("utf-8").strip() == "not":
                sites.append((start, operand, "", where, "not"))
        elif isinstance(node, ast.Constant) and node.end_lineno == node.lineno:
            start = pos(node.lineno, node.col_offset)
            end = pos(node.end_lineno, node.end_col_offset)
            raw = data[start:end].decode("utf-8")
            if node.value is True and raw == "True":
                sites.append((start, end, "False", where, "bool"))
            elif node.value is False and raw == "False":
                sites.append((start, end, "True", where, "bool"))
            elif isinstance(node.value, int) and not isinstance(node.value, bool) and raw.isdigit():
                sites.append((start, end, str(node.value + 1), where, "int"))
            elif isinstance(node.value, str) and len(raw) > 4 and raw[0] in "\"'" and raw[:3] not in ('"""', "'''") and "\\" not in raw:
                sites.append((start, end, raw[:-1] + "_x" + raw[-1], where, "str"))
        elif isinstance(node, (ast.Return, ast.Expr, ast.Assign)) and scope.get(node) == "func":
            if node.lineno == node.end_lineno and not (
                isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
            ):
                start = pos(node.lineno, node.col_offset)
                end = pos(node.end_lineno, node.end_col_offset)
                sites.append((start, end, "pass", "func", "delete"))
    return data, sites


def mutate_code(rel, text, rng, want=None):
    data, sites = code_sites(text)
    if want:
        sites = [s for s in sites if s[3] == want]
    # strings and ints are everywhere; keep them from drowning the rest
    weights = [0.25 if s[4] in ("str", "int") else 0.5 if s[4] == "delete" else 1.0 for s in sites]
    if not sites:
        return None
    start, end, replacement, where, kind = rng.choices(sites, weights)[0]
    new = (data[:start] + replacement.encode("utf-8") + data[end:]).decode("utf-8")
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return unified(rel, text, new), f"{kind}/{where}"


def mutate_comment(rel, text, rng):
    lines = text.splitlines(True)
    candidates = [i for i, line in enumerate(lines) if line.strip() and not line.strip().startswith(("#", '"""', "'''", ")", "]", "}"))]
    index = rng.choice(candidates)
    indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
    new_lines = lines[:index] + [f"{indent}{rng.choice(COMMENTS)}\n"] + lines[index:]
    new = "".join(new_lines)
    try:
        if ast.dump(ast.parse(new)) != ast.dump(ast.parse(text)):
            return None
    except SyntaxError:
        return None
    return unified(rel, text, new), "comment"


def mutate_fixture(rel, text, rng):
    lines = text.splitlines(True)
    candidates = [i for i, line in enumerate(lines) if line.strip() and not line.startswith(("--", "#"))]
    index = rng.choice(candidates)
    words = lines[index].rstrip("\n").split(" ")
    spot = rng.randrange(len(words) + 1)
    action = rng.choice(["insert", "insert", "delete", "comment"])
    if action == "delete" and len(words) > 1:
        del words[min(spot, len(words) - 1)]
    elif action == "comment":
        lines.insert(index, "-- " + rng.choice(COMMENTS)[2:] + "\n")
        return unified(rel, text, "".join(lines)), "fixture/comment"
    else:
        words.insert(spot, rng.choice(["42", "x", "0", "NULL", "1"]))
    lines[index] = " ".join(words) + "\n"
    return unified(rel, text, "".join(lines)), f"fixture/{action}"


def main():
    checkout, out_dir, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 1)
    os.makedirs(out_dir, exist_ok=True)
    source = sorted(p for p in glob.glob(os.path.join(checkout, "sqlglot", "**", "*.py"), recursive=True))
    test_files = sorted(glob.glob(os.path.join(checkout, "tests", "**", "test_*.py"), recursive=True))
    fixtures = sorted(glob.glob(os.path.join(checkout, "tests", "fixtures", "**", "*.sql"), recursive=True))
    sizes = [os.path.getsize(p) for p in source]
    made = 0
    index = []
    while made < count:
        roll = rng.random()
        try:
            if roll < 0.40:  # code, file chosen uniformly (as the samples look)
                path = rng.choice(source)
                result = mutate_code(os.path.relpath(path, checkout).replace(os.sep, "/"), read(path), rng)
            elif roll < 0.55:  # code, weighted to the big core files
                path = rng.choices(source, sizes)[0]
                result = mutate_code(os.path.relpath(path, checkout).replace(os.sep, "/"), read(path), rng)
            elif roll < 0.70:  # module and class level only
                path = rng.choice(source)
                result = mutate_code(os.path.relpath(path, checkout).replace(os.sep, "/"), read(path), rng, "body")
            elif roll < 0.82:
                path = rng.choice(fixtures)
                result = mutate_fixture(os.path.relpath(path, checkout).replace(os.sep, "/"), read(path), rng)
            elif roll < 0.90:
                path = rng.choice(source)
                result = mutate_comment(os.path.relpath(path, checkout).replace(os.sep, "/"), read(path), rng)
            else:  # an edit inside a test file
                path = rng.choice(test_files)
                result = mutate_code(os.path.relpath(path, checkout).replace(os.sep, "/"), read(path), rng)
        except (SyntaxError, IndexError, ValueError):
            continue
        if result is None:
            continue
        diff, kind = result
        name = f"m{made:04d}"
        with open(os.path.join(out_dir, f"{name}.patch"), "w", encoding="utf-8", newline="") as handle:
            handle.write(diff)
        index.append(f"{name} {kind} {os.path.relpath(path, checkout)}")
        made += 1
    with open(os.path.join(out_dir, "index.txt"), "w") as handle:
        handle.write("\n".join(index) + "\n")
    print(f"wrote {made} patches")


if __name__ == "__main__":
    main()
