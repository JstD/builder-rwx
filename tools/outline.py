"""Record where the module- and class-level statements of each source file are.

Parsing an 11,000-line file to look at one statement is most of the selector's own
CPU. With this outline it can cut the statement's lines out of the file and parse
only those. Functions are not opened up: a function is one unit, however long.

  maps/outline.json   file -> {"star": bool, "units": [[start, end, indent, kind, cls, first, last], ...]}

  kind "stmt"   one statement (decorators included), lines start..end
  kind "head"   the header of a class (decorators, bases), up to the line before its body
  cls           dotted names of the classes the unit sits in ("" at module level);
                for a head, the classes around it plus the class itself after "|"
  first         1 when the statement is the first of its block (where a docstring lives)
  last          the last line of the statement; a head also carries the line of its ``class``

Usage: python outline.py <checkout> <maps dir>
"""

from __future__ import annotations

import ast
import json
import os
import sys


def first_line(node) -> int:
    decorators = getattr(node, "decorator_list", None) or []
    return min([node.lineno] + [d.lineno for d in decorators])


def units_of(body, indent_of, chain: str, out: list):
    for position, statement in enumerate(body):
        start = first_line(statement)
        indent = indent_of(start)
        first = 1 if position == 0 else 0
        if isinstance(statement, ast.ClassDef) and statement.body and first_line(statement.body[0]) > statement.lineno:
            inner = f"{chain}.{statement.name}" if chain else statement.name
            head_end = first_line(statement.body[0]) - 1
            out.append([start, head_end, indent, "head", f"{chain}|{inner}", first,
                        statement.end_lineno, statement.lineno])
            units_of(statement.body, indent_of, inner, out)
        else:
            out.append([start, statement.end_lineno, indent, "stmt", chain, first, statement.end_lineno])


def outline(text: str) -> dict:
    tree = ast.parse(text)
    lines = text.split("\n")

    def indent_of(line_number: int) -> int:
        line = lines[line_number - 1]
        return len(line) - len(line.lstrip(" "))

    units: list = []
    units_of(tree.body, indent_of, "", units)
    star = any(
        isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
        for node in tree.body
    )
    return {"star": star, "units": units}


def main():
    checkout, maps_dir = sys.argv[1], sys.argv[2]
    index = json.load(open(os.path.join(maps_dir, "index.json")))
    out = {}
    for path in index["files"]:
        try:
            with open(os.path.join(checkout, path), encoding="utf-8", newline="") as handle:
                out[path] = outline(handle.read())
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
    target = os.path.join(maps_dir, "outline.json")
    with open(target, "w", newline="\n") as handle:
        json.dump(out, handle, separators=(",", ":"))
    print(len(out), "files,", sum(len(v["units"]) for v in out.values()), "units,",
          os.path.getsize(target) // 1024, "KB")


if __name__ == "__main__":
    main()
