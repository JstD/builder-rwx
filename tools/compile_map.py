"""Fold the per-test traces into the maps the selector reads.

Every map is derived from running sqlglot's own tests on the pinned commit:

  maps/cov/<file>.json  per source file: which tests execute each line
      "deferred"  line -> set id: tests that ran the line from function code
      "immediate" line -> set id: tests in whose process the line ran at all
      "runtime"   line -> set id: tests that ran the line outside any module import
      "sets"      the distinct test sets, as flat [start, length, ...] runs
  maps/index.json       test timings, files opened and dirs listed by each test,
                        classes whose methods each test called, class ancestry

Usage: python compile_map.py <tests.json> <trace dir> <timing.json> <maps dir>
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict


def runs(numbers) -> list[int]:
    """Sorted ints as flat [start, length, start, length, ...]."""
    out: list[int] = []
    previous = None
    for n in sorted(numbers):
        if previous is not None and n == previous + 1:
            out[-1] += 1
        else:
            out += [n, 1]
        previous = n
    return out


def safe(path: str) -> str:
    return path.replace("/", "__")


def main():
    tests_path, trace_dir, timing_path, out_dir = sys.argv[1:5]
    tests = json.load(open(tests_path))["tests"]
    timing = json.load(open(timing_path))

    deferred = defaultdict(lambda: defaultdict(set))  # file -> line -> tests
    immediate = defaultdict(lambda: defaultdict(set))
    runtime = defaultdict(lambda: defaultdict(set))
    opens = defaultdict(set)
    dirs = defaultdict(set)
    types = defaultdict(set)
    not_ok = []
    wall = {}

    for test in tests:
        number = test["number"]
        trace = json.load(open(os.path.join(trace_dir, f"{number}.json")))
        assert trace["id"] == test["id"]
        if not trace["ok"]:
            not_ok.append(test["id"])
        wall[number] = trace["wall"]
        for key in ("run", "imp_call"):
            for file, lines in trace[key].items():
                target = deferred[file]
                for line in lines:
                    target[line].add(number)
        for file, lines in trace["run"].items():
            target = runtime[file]
            for line in lines:
                target[line].add(number)
        for key in ("run", "imp_call", "imp_body"):
            for file, lines in trace[key].items():
                target = immediate[file]
                for line in lines:
                    target[line].add(number)
        for path in trace["opens"]:
            opens[path].add(number)
        for path in trace["dirs"]:
            dirs[path].add(number)
        for name in trace["types"]:
            types[name].add(number)
        for name in trace.get("import_types", ()):
            types[name].add(number)

    os.makedirs(os.path.join(out_dir, "cov"), exist_ok=True)
    total = 0
    for file in sorted(immediate):
        sets: dict[frozenset, int] = {}
        encoded: list[list[int]] = []

        def set_id(numbers):
            key = frozenset(numbers)
            if key not in sets:
                sets[key] = len(encoded)
                encoded.append(runs(key))
            return sets[key]

        payload = {
            "deferred": {str(line): set_id(t) for line, t in sorted(deferred[file].items())},
            "immediate": {str(line): set_id(t) for line, t in sorted(immediate[file].items())},
            "runtime": {str(line): set_id(t) for line, t in sorted(runtime[file].items())},
            "sets": encoded,
        }
        path = os.path.join(out_dir, "cov", safe(file) + ".json")
        with open(path, "w", newline="\n") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        total += os.path.getsize(path)

    index = {
        "ids": [t["id"] for t in tests],
        "cpu": [timing["cpu"][str(t["number"])] for t in tests],
        "class_setup_cpu": {k: round(v, 4) for k, v in timing["setup"].items() if v >= 0.01},
        "opens": {path: runs(t) for path, t in sorted(opens.items())},
        "dirs": {path: runs(t) for path, t in sorted(dirs.items())},
        "types": {name: runs(t) for name, t in sorted(types.items())},
        "files": sorted(immediate),
    }
    path = os.path.join(out_dir, "index.json")
    with open(path, "w", newline="\n") as handle:
        json.dump(index, handle, separators=(",", ":"))
    total += os.path.getsize(path)
    print(f"{len(tests)} tests, {len(immediate)} files, {total / 1e6:.1f} MB of maps")
    print(f"tests that failed while traced: {not_ok}")


if __name__ == "__main__":
    main()
