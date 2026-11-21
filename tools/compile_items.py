"""Compile per-item traces into maps/items.json.

Many slow tests are one loop over independent items: the pairs of a .sql fixture
(most optimizer tests), or the queries handed to a process pool (the executor's
TPC-DS test). tools/trace_one.py with BR_ITEMS=1 records, for each item, the lines
that ran while that item was being handled. This turns those traces into a map
from source line to the items that run it, so that for a change inside a function
the selector knows which items of a test can be affected.

Per test:
  ids       the items, in a fixed order: "P:<n>" is the n-th pool task, and
            "L:<fixture>:<n>" the n-th item a fixture loader yields
  weights   traced seconds per item (only their proportions are used)
  base      traced seconds outside any item
  imports   {file: [lines the test runs while modules are being imported]}
  files     {file: {"sets": [[start, length, ...], ...], "lines": {line: set index}}}
            where a set is run-length coded positions into ``ids``

Usage: python compile_items.py <out file> <trace json> [<trace json> ...]
"""

from __future__ import annotations

import json
import sys


def runs(positions):
    out = []
    for value in sorted(positions):
        if out and out[-2] + out[-1] == value:
            out[-1] += 1
        else:
            out += [value, 1]
    return out


def order(key):
    kind, rest = key.split(":", 1)
    if kind == "P":
        return (0, "", int(rest))
    fixture, index = rest.rsplit(":", 1)
    return (1, fixture, int(index))


def main():
    out_path, traces = sys.argv[1], sys.argv[2:]
    result = {}
    for path in traces:
        trace = json.load(open(path))
        ids = sorted(trace["items"], key=order)
        position = {key: i for i, key in enumerate(ids)}
        weights = [round(trace["item_time"].get(key, 0.0), 4) for key in ids]
        by_line: dict[str, dict[int, set[int]]] = {}
        for key, files in trace["items"].items():
            for file, lines in files.items():
                table = by_line.setdefault(file, {})
                for line in lines:
                    table.setdefault(line, set()).add(position[key])
        files_out = {}
        for file, table in sorted(by_line.items()):
            sets: list[list[int]] = []
            seen: dict[tuple, int] = {}
            lines_out = {}
            for line in sorted(table):
                key = tuple(runs(table[line]))
                if key not in seen:
                    seen[key] = len(sets)
                    sets.append(list(key))
                lines_out[str(line)] = seen[key]
            files_out[file] = {"sets": sets, "lines": lines_out}
        imports: dict[str, list[int]] = {}
        for part in ("imp_call", "imp_body"):
            for file, lines in trace.get(part, {}).items():
                imports[file] = sorted(set(imports.get(file, [])) | set(lines))
        result[trace["id"]] = {
            "ids": ids,
            "weights": weights,
            "base": round(trace["base_time"], 4),
            "imports": imports,
            "files": files_out,
        }
        print(f"{trace['id']}: {len(ids)} items, {sum(len(v['lines']) for v in files_out.values())} lines",
              file=sys.stderr)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    main()
