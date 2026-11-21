"""Compile per-item traces into maps/items.json.

Some long tests are one loop over independent items handed to a process pool (the
executor's TPC-DS test: 99 queries). tools/trace_one.py with BR_ITEMS=1 records,
for each item, the lines that ran while that item was being handled. This turns
those traces into a map from source line to the items that run it, so that for a
change inside a function the selector knows which items can be affected.

Per test:
  weights   traced seconds per item (only their proportions are used)
  base      traced seconds outside any item
  imports   {file: [lines the test runs while modules are being imported]}
  files     {file: {"sets": [[item, ...], ...], "lines": {line: set index}}}

Usage: python compile_items.py <out file> <trace json> [<trace json> ...]
"""

from __future__ import annotations

import json
import sys


def main():
    out_path, traces = sys.argv[1], sys.argv[2:]
    result = {}
    for path in traces:
        trace = json.load(open(path))
        items = {}
        for key, files in trace["items"].items():
            kind, index = key.split(":")[0], int(key.rsplit(":", 1)[1])
            if kind != "P":
                raise SystemExit(f"{path}: only pool items are supported, found {key}")
            items[index] = files
        count = max(items) + 1
        weights = [0.0] * count
        for key, value in trace["item_time"].items():
            weights[int(key.rsplit(":", 1)[1])] = round(value, 4)
        by_line: dict[str, dict[int, set[int]]] = {}
        for index, files in items.items():
            for file, lines in files.items():
                table = by_line.setdefault(file, {})
                for line in lines:
                    table.setdefault(line, set()).add(index)
        files_out = {}
        for file, table in sorted(by_line.items()):
            sets: list[list[int]] = []
            seen: dict[tuple, int] = {}
            lines_out = {}
            for line in sorted(table):
                key = tuple(sorted(table[line]))
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
            "weights": weights,
            "base": round(trace["base_time"], 4),
            "imports": imports,
            "files": files_out,
        }
        print(f"{trace['id']}: {count} items, {sum(len(v['lines']) for v in files_out.values())} lines",
              file=sys.stderr)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, separators=(",", ":"), sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    main()
