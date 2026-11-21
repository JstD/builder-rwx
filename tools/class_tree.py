"""Record how sqlglot's classes relate, on the pinned commit.

  mro       class -> the sqlglot classes it inherits from (itself excluded)
  parts     dialect class -> its tokenizer, parser and generator classes

Classes are named "module:qualname". Usage: python class_tree.py <out.json>
(cwd = sqlglot checkout)
"""

import importlib
import json
import os
import pkgutil
import sys

sys.path.insert(0, os.getcwd())

import sqlglot  # noqa: E402
from sqlglot.dialects.dialect import Dialect  # noqa: E402


def name(cls):
    return f"{cls.__module__}:{cls.__qualname__}"


def main():
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name != "sqlglot.__main__":
            importlib.import_module(info.name)
    Dialect.classes  # loads every dialect

    seen = {}
    stack = [object]
    while stack:
        cls = stack.pop()
        try:
            subclasses = type.__subclasses__(cls)
        except TypeError:
            continue
        for sub in subclasses:
            if id(sub) not in seen:
                seen[id(sub)] = sub
                stack.append(sub)
    ours = [c for c in seen.values() if getattr(c, "__module__", "").split(".")[0] == "sqlglot"]

    mro = {}
    for cls in ours:
        bases = [name(b) for b in cls.__mro__[1:] if getattr(b, "__module__", "").split(".")[0] == "sqlglot"]
        mro.setdefault(name(cls), set()).update(bases)
        meta = type(cls)
        if getattr(meta, "__module__", "").split(".")[0] == "sqlglot":
            mro.setdefault(name(cls), set())

    parts = {}
    for cls in ours:
        if isinstance(cls, type) and issubclass(cls, Dialect):
            found = set()
            for attr in ("tokenizer_class", "jsonpath_tokenizer_class", "parser_class",
                         "generator_class", "Tokenizer", "JSONPathTokenizer", "Parser", "Generator"):
                part = getattr(cls, attr, None)
                if isinstance(part, type) and getattr(part, "__module__", "").split(".")[0] == "sqlglot":
                    found.add(name(part))
            parts.setdefault(name(cls), set()).update(found)

    # who imports whom, from the import statements of sqlglot/ and tests/
    import ast
    import glob

    importers = {}
    paths = glob.glob("sqlglot/**/*.py", recursive=True) + glob.glob("tests/**/*.py", recursive=True)
    for path in sorted(paths):
        path = path.replace(os.sep, "/")
        module = path[:-3].replace("/", ".")
        package = module.rsplit(".", 1)[0]
        if module.endswith(".__init__"):
            module = package
        try:
            tree = ast.parse(open(path, encoding="utf-8").read())
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Import):
                targets = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parent = package.split(".")
                    parent = parent[: len(parent) - (node.level - 1)]
                    base = ".".join(parent + ([node.module] if node.module else []))
                targets = [base] + [f"{base}.{alias.name}" for alias in node.names]
            for target in targets:
                if target.split(".")[0] in ("sqlglot", "tests") and target != module:
                    importers.setdefault(target, set()).add(module)

    out = {
        "mro": {k: sorted(v) for k, v in sorted(mro.items())},
        "parts": {k: sorted(v) for k, v in sorted(parts.items())},
        "importers": {k: sorted(v) for k, v in sorted(importers.items())},
    }
    with open(sys.argv[1], "w", newline="\n") as handle:
        json.dump(out, handle, separators=(",", ":"))
    print(len(mro), "classes,", len(parts), "dialects")


if __name__ == "__main__":
    main()
