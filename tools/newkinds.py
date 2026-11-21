"""Practice commits of kinds mutate.py does not make: statements removed or added,
imports, signatures, decorators, whole functions, test helpers, non-code files,
files added, removed or renamed, and several files at once.

Usage: python tools/newkinds.py <full LF checkout> <out dir> [seed]
"""

from __future__ import annotations

import ast
import glob
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mutate  # noqa: E402

ROOT = sys.argv[1]
OUT = sys.argv[2]
rng = random.Random(int(sys.argv[3]) if len(sys.argv) > 3 else 7)
os.makedirs(OUT, exist_ok=True)

SOURCE = sorted(glob.glob(os.path.join(ROOT, "sqlglot", "**", "*.py"), recursive=True))
TESTS = sorted(glob.glob(os.path.join(ROOT, "tests", "**", "*.py"), recursive=True))
FIXTURES = sorted(glob.glob(os.path.join(ROOT, "tests", "fixtures", "**", "*.sql"), recursive=True))
SIZES = [os.path.getsize(p) for p in SOURCE]


def rel(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def pick_source(big=0.4):
    return rng.choices(SOURCE, SIZES)[0] if rng.random() < big else rng.choice(SOURCE)


def parses(text):
    try:
        ast.parse(text)
        return True
    except SyntaxError:
        return False


def replace_lines(text, first, last, new_lines):
    """Replace lines first..last (1-based, inclusive) with new_lines (each ends in \\n)."""
    lines = text.splitlines(True)
    return "".join(lines[: first - 1] + new_lines + lines[last:])


def functions(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def sig(path, text):
    """Flip the default of a parameter."""
    tree = ast.parse(text)
    sites = []
    for fn in functions(tree):
        for default in fn.args.defaults + [d for d in fn.args.kw_defaults if d is not None]:
            if isinstance(default, ast.Constant) and default.lineno == default.end_lineno:
                value = default.value
                if value is True:
                    sites.append((default, "False"))
                elif value is False:
                    sites.append((default, "True"))
                elif value is None:
                    sites.append((default, rng.choice(["0", "''", "True"])))
                elif isinstance(value, int):
                    sites.append((default, str(value + 1)))
    if not sites:
        return None
    node, new = rng.choice(sites)
    lines = text.splitlines(True)
    line = lines[node.lineno - 1]
    raw = line.encode("utf-8")
    line = (raw[: node.col_offset] + new.encode() + raw[node.end_col_offset:]).decode("utf-8")
    return replace_lines(text, node.lineno, node.lineno, [line])


def simple_statements(tree, in_function=True, min_block=2):
    found = []
    for owner in ast.walk(tree):
        inside = isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef))
        for field in ("body", "orelse", "finalbody"):
            block = getattr(owner, field, None)
            if not isinstance(block, list) or len(block) < min_block:
                continue
            for statement in block:
                if not isinstance(statement, (ast.Assign, ast.Expr, ast.Return, ast.AugAssign, ast.AnnAssign)):
                    continue
                if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
                    continue
                found.append((owner, statement, inside))
    return found


def within_function(tree):
    inside = set()
    for fn in functions(tree):
        for node in ast.walk(fn):
            inside.add(node)
    return inside


def dellines(path, text):
    """Delete one whole statement from a function body."""
    tree = ast.parse(text)
    inside = within_function(tree)
    sites = [s for _, s, _ in simple_statements(tree) if s in inside]
    if not sites:
        return None
    statement = rng.choice(sites)
    return replace_lines(text, statement.lineno, statement.end_lineno, [])


def clsdel(path, text):
    """Delete a class-level assignment, or one line of a class-level literal."""
    tree = ast.parse(text)
    sites = []
    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for statement in cls.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)) and getattr(statement, "value", None) is not None:
                if statement.lineno == statement.end_lineno and len(cls.body) > 1:
                    sites.append((statement.lineno, statement.lineno))
                elif statement.end_lineno - statement.lineno >= 3:
                    for _ in range(2):
                        line = rng.randrange(statement.lineno + 1, statement.end_lineno)
                        sites.append((line, line))
    rng.shuffle(sites)
    for first, last in sites[:20]:
        new = replace_lines(text, first, last, [])
        if parses(new) and ast.dump(ast.parse(new)) != ast.dump(tree):
            return new
    return None


def insret(path, text):
    """Insert an early return in a function."""
    tree = ast.parse(text)
    sites = [fn for fn in functions(tree) if len(fn.body) >= 3]
    if not sites:
        return None
    fn = rng.choice(sites)
    at = rng.choice(fn.body[1:])
    lines = text.splitlines(True)
    line = lines[at.lineno - 1]
    indent = line[: len(line) - len(line.lstrip())]
    new = replace_lines(text, at.lineno, at.lineno - 1, [f"{indent}return None\n"])
    return new if parses(new) else None


def imports(path, text):
    """Delete a module-level import line, or one imported name."""
    tree = ast.parse(text)
    sites = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    sites = [n for n in sites if not (isinstance(n, ast.ImportFrom) and n.module == "__future__")]
    if not sites:
        return None
    node = rng.choice(sites)
    if node.lineno == node.end_lineno:
        new = replace_lines(text, node.lineno, node.lineno, [])
    else:
        line = rng.randrange(node.lineno + 1, node.end_lineno)
        new = replace_lines(text, line, line, [])
    return new if parses(new) else None


def syntax(path, text):
    """Break the file's syntax on one line."""
    lines = text.splitlines(True)
    sites = [i for i, line in enumerate(lines) if line.rstrip().endswith(":") and line.lstrip().startswith(("if ", "for ", "def ", "while "))]
    if not sites:
        return None
    index = rng.choice(sites)
    lines[index] = lines[index].rstrip()[:-1] + "\n"
    new = "".join(lines)
    return None if parses(new) else new


def decorator(path, text):
    tree = ast.parse(text)
    sites = [d for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef)) for d in n.decorator_list
             if d.lineno == d.end_lineno]
    if not sites:
        return None
    node = rng.choice(sites)
    return replace_lines(text, node.lineno, node.lineno, [])


def bigfunc(path, text):
    """Replace a function's body with a raise, or remove a method outright."""
    tree = ast.parse(text)
    sites = [fn for fn in functions(tree) if fn.end_lineno - fn.lineno >= 3]
    if not sites:
        return None
    fn = rng.choice(sites)
    lines = text.splitlines(True)
    if rng.random() < 0.4:
        first = min([fn.lineno] + [d.lineno for d in fn.decorator_list])
        new = replace_lines(text, first, fn.end_lineno, [])
    else:
        start = fn.body[0].lineno
        if isinstance(fn.body[0], ast.Expr) and isinstance(fn.body[0].value, ast.Constant) and len(fn.body) > 1:
            start = fn.body[1].lineno
        line = lines[start - 1]
        indent = line[: len(line) - len(line.lstrip())]
        new = replace_lines(text, start, fn.end_lineno, [f"{indent}raise NotImplementedError()\n"])
    return new if parses(new) else None


def docstring(path, text):
    tree = ast.parse(text)
    sites = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Module)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                sites.append(first)
    if not sites:
        return None
    node = rng.choice(sites)
    lines = text.splitlines(True)
    line = rng.randrange(node.lineno, node.end_lineno + 1)
    stripped = lines[line - 1].rstrip("\n")
    if stripped.rstrip().endswith(('"""', "'''")) and node.lineno != node.end_lineno and line == node.end_lineno:
        line = node.lineno
        stripped = lines[line - 1].rstrip("\n")
    if stripped.rstrip().endswith(('"""', "'''")) and len(stripped.strip()) > 6:
        quote = stripped.rstrip()[-3:]
        new_line = stripped.rstrip()[:-3] + " (see notes)" + quote + "\n"
    elif stripped.rstrip().endswith(('"""', "'''")):
        return None
    else:
        new_line = stripped + " Updated.\n"
    new = replace_lines(text, line, line, [new_line])
    return new if parses(new) else None


def fmt(path, text):
    """Formatting only: a blank line, a trailing comment, or trailing spaces."""
    lines = text.splitlines(True)
    sites = [i for i, line in enumerate(lines) if line.strip() and not line.strip().startswith(("#", '"', "'"))]
    index = rng.choice(sites)
    roll = rng.random()
    if roll < 0.4:
        lines.insert(index, "\n")
    elif roll < 0.8:
        lines[index] = lines[index].rstrip("\n") + "  # noqa\n"
    else:
        lines[index] = lines[index].rstrip("\n") + "  \n"
    new = "".join(lines)
    try:
        if ast.dump(ast.parse(new)) != ast.dump(ast.parse(text)):
            return None
    except SyntaxError:
        return None
    return new


def operator(path, text):
    """Swap an arithmetic operator or a min/max, any/all call."""
    tree = ast.parse(text)
    lines = text.splitlines(True)
    sites = []
    swap = {"min": "max", "max": "min", "any": "all", "all": "any"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in swap:
            sites.append((node.func, swap[node.func.id]))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)) and node.left.end_lineno == node.right.lineno:
            raw = lines[node.left.end_lineno - 1].encode("utf-8")
            gap = raw[node.left.end_col_offset: node.right.col_offset].decode("utf-8", "replace")
            token = "+" if isinstance(node.op, ast.Add) else "-"
            if gap.strip() == token:
                sites.append(((node.left.end_lineno, node.left.end_col_offset + gap.index(token), 1), "-" if token == "+" else "+"))
    if not sites:
        return None
    node, new = rng.choice(sites)
    if isinstance(node, tuple):
        lineno, col, size = node
    else:
        lineno, col, size = node.lineno, node.col_offset, node.end_col_offset - node.col_offset
    raw = lines[lineno - 1].encode("utf-8")
    line = (raw[:col] + new.encode() + raw[col + size:]).decode("utf-8")
    out = replace_lines(text, lineno, lineno, [line])
    return out if parses(out) else None


def testhelper(path, text):
    """An edit in a test file outside the test methods: helpers, class attributes."""
    tree = ast.parse(text)
    data, sites = mutate.code_sites(text)
    starts = mutate.line_starts(text)
    test_spans = [(fn.lineno, fn.end_lineno) for fn in functions(tree) if fn.name.startswith("test")]
    import bisect

    keep = []
    for site in sites:
        line = bisect.bisect_right(starts, site[0])
        if not any(a <= line <= b for a, b in test_spans):
            keep.append(site)
    if not keep:
        return None
    start, end, replacement, where, kind = rng.choice(keep)
    new = (data[:start] + replacement.encode("utf-8") + data[end:]).decode("utf-8")
    return new if parses(new) else None


def text_edit(path, text):
    """One line of a non-code file changed."""
    lines = text.splitlines(True)
    sites = [i for i, line in enumerate(lines) if len(line.strip()) > 8]
    if not sites:
        return None
    index = rng.choice(sites)
    lines[index] = lines[index].rstrip("\n") + " (updated)\n"
    return "".join(lines)


def diff_for(path, maker):
    text = mutate.read(path)
    try:
        new = maker(path, text)
    except (SyntaxError, IndexError, ValueError):
        return None
    if new is None or new == text:
        return None
    return mutate.unified(rel(path), text, new)


def deleted(path):
    text = mutate.read(path)
    lines = text.splitlines(True)
    name = rel(path)
    body = "".join("-" + (line if line.endswith("\n") else line + "\n\\ No newline at end of file\n") for line in lines)
    return (f"diff --git a/{name} b/{name}\ndeleted file mode 100644\n--- a/{name}\n+++ /dev/null\n"
            f"@@ -1,{len(lines)} +0,0 @@\n{body}")


def added(name, text):
    lines = text.splitlines(True)
    body = "".join("+" + line for line in lines)
    return (f"diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n+++ b/{name}\n"
            f"@@ -0,0 +1,{len(lines)} @@\n{body}")


def renamed(path, new_name):
    name = rel(path)
    return (f"diff --git a/{name} b/{new_name}\nsimilarity index 100%\nrename from {name}\nrename to {new_name}\n")


made: list[str] = []


def save(kind, diff, note):
    name = f"n{len(made):04d}"
    with open(os.path.join(OUT, name + ".patch"), "w", encoding="utf-8", newline="") as handle:
        handle.write(diff)
    made.append(f"{name} {kind} {note}")


def many(kind, count, maker, chooser):
    done = tries = 0
    while done < count and tries < count * 80:
        tries += 1
        path = chooser()
        diff = diff_for(path, maker)
        if diff:
            save(kind, diff, rel(path))
            done += 1


many("sig", 14, sig, pick_source)
many("dellines", 14, dellines, pick_source)
many("clsdel", 12, clsdel, lambda: pick_source(0.1))
many("insret", 8, insret, pick_source)
many("imports", 10, imports, lambda: pick_source(0.1))
many("syntax", 6, syntax, lambda: pick_source(0.1))
many("decorator", 5, decorator, pick_source)
many("bigfunc", 10, bigfunc, pick_source)
many("docstring", 5, docstring, pick_source)
many("format", 6, fmt, pick_source)
many("operator", 8, operator, pick_source)
many("testhelper", 10, testhelper, lambda: rng.choice(TESTS))

# test-file edits made with the first generator's one-token changes
done = 0
while done < 8:
    path = rng.choice([p for p in TESTS if os.path.basename(p).startswith("test_")])
    try:
        result = mutate.mutate_code(rel(path), mutate.read(path), rng)
    except (SyntaxError, IndexError, ValueError):
        continue
    if result:
        save("testedit", result[0], rel(path))
        done += 1

# non-code files
for name in ("README.md", "CHANGELOG.md", "Makefile", "setup.cfg", "MANIFEST.in", "pyproject.toml",
             "CONTRIBUTING.md", "sqlglotc/setup.py", "sqlglotc/pyproject.toml", "tests/fixtures/jsonpath/cts.json",
             "tests/fixtures/jsonpath/LICENSE", "setup.py", "tests/gen_fixtures.py", "benchmarks/parse.py"):
    path = os.path.join(ROOT, name)
    if os.path.exists(path):
        diff = diff_for(path, text_edit if not name.endswith(".py") else fmt)
        if diff:
            save("textfile", diff, name)

# files appearing, disappearing, moving
save("delfile", deleted(os.path.join(ROOT, "sqlglot/dialects/dune.py")), "sqlglot/dialects/dune.py")
save("delfile", deleted(os.path.join(ROOT, "sqlglot/optimizer/eliminate_ctes.py")), "sqlglot/optimizer/eliminate_ctes.py")
save("delfile", deleted(os.path.join(ROOT, "tests/fixtures/optimizer/eliminate_ctes.sql")), "tests/fixtures/optimizer/eliminate_ctes.sql")
save("delfile", deleted(os.path.join(ROOT, "tests/test_time.py")), "tests/test_time.py")
save("newfile", added("sqlglot/dialects/newdialect.py", "from sqlglot.dialects.dialect import Dialect\n\n\nclass NewDialect(Dialect):\n    pass\n"), "sqlglot/dialects/newdialect.py")
save("newfile", added("tests/test_new_thing.py", "import unittest\n\n\nclass TestNewThing(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(1, 1)\n"), "tests/test_new_thing.py")
save("newfile", added("tests/fixtures/optimizer/extra.sql", "SELECT 1;\nSELECT 1;\n"), "tests/fixtures/optimizer/extra.sql")
save("newfile", added("NOTES.md", "# Notes\n\nNothing yet.\n"), "NOTES.md")
save("rename", renamed(os.path.join(ROOT, "sqlglot/dialects/tableau.py"), "sqlglot/dialects/tableau2.py"), "sqlglot/dialects/tableau.py")
save("rename", renamed(os.path.join(ROOT, "tests/fixtures/optimizer/simplify.sql"), "tests/fixtures/optimizer/simplify_all.sql"), "tests/fixtures/optimizer/simplify.sql")
save("rename", renamed(os.path.join(ROOT, "tests/test_serde.py"), "tests/test_serde2.py"), "tests/test_serde.py")

# several files in one commit
MAKERS = [(sig, pick_source), (dellines, pick_source), (operator, pick_source), (fmt, pick_source),
          (docstring, pick_source), (clsdel, lambda: pick_source(0.1)), (testhelper, lambda: rng.choice(TESTS))]


def one_part():
    roll = rng.random()
    if roll < 0.45:
        path = pick_source()
        result = mutate.mutate_code(rel(path), mutate.read(path), rng)
        return (path, result[0]) if result else None
    if roll < 0.6:
        path = rng.choice(FIXTURES)
        result = mutate.mutate_fixture(rel(path), mutate.read(path), rng)
        return (path, result[0]) if result else None
    if roll < 0.7:
        path = rng.choice([p for p in TESTS if os.path.basename(p).startswith("test_")])
        result = mutate.mutate_code(rel(path), mutate.read(path), rng)
        return (path, result[0]) if result else None
    maker, chooser = rng.choice(MAKERS)
    path = chooser()
    diff = diff_for(path, maker)
    return (path, diff) if diff else None


done = 0
while done < 24:
    parts = {}
    want = rng.choice([2, 2, 3, 4])
    tries = 0
    while len(parts) < want and tries < 50:
        tries += 1
        try:
            part = one_part()
        except (SyntaxError, IndexError, ValueError):
            continue
        if part and part[0] not in parts:
            parts[part[0]] = part[1]
    if len(parts) == want:
        save("multi", "".join(parts[p] for p in sorted(parts)), ",".join(rel(p) for p in sorted(parts)))
        done += 1

with open(os.path.join(OUT, "index.txt"), "w") as handle:
    handle.write("\n".join(made) + "\n")
import collections

print(collections.Counter(line.split()[1] for line in made))
