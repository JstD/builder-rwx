"""Ground truth for practice commits: run every test on each patched tree.

Uses the same harness as the selector, so it checks the candidate maps and the
decision logic, not the harness itself (the 30 labeled samples check that).

Usage: python label.py <tests.json> <patch dir> <out dir> [jobs]
       python label.py --one <tests.json> <patch file> <out file>
(cwd anywhere; BR_CHECKOUT points at the pinned checkout)
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

CHECKOUT = os.environ.get("BR_CHECKOUT", "/app/sqlglot")


def one(tests_path, patch_path, out_path):
    from br import harness, runner
    from br import patch as patchlib

    tests = json.load(open(tests_path))["tests"]
    cpu = None
    timing = os.environ.get("BR_TIMING")
    if timing:
        cpu = json.load(open(timing))["cpu"]
    text = open(patch_path, encoding="utf-8", newline="").read()
    files = {}
    for file_patch in patchlib.parse(text):
        old = None
        if file_patch.old_path:
            with open(os.path.join(CHECKOUT, file_patch.old_path), encoding="utf-8", newline="") as handle:
                old = handle.read()
        new, _ = patchlib.apply(old, file_patch)
        if file_patch.new_path:
            files[file_patch.new_path] = new
        if file_patch.old_path and file_patch.old_path != file_patch.new_path:
            files[file_patch.old_path] = None
    # The three slowest tests are run only when they execute a patched file at all;
    # that halves the time and cannot hide a failure the maps would need to explain.
    skip = set()
    if os.environ.get("BR_SKIP_BIG"):
        import selector

        maps = selector.Maps(selector.MAPS, tests)
        touched = set()
        for path in files:
            touched |= maps.whole_file(path) | maps.readers(path)
        skip = {t["number"] for t in tests if t["seconds"] > 5 and t["number"] not in touched}
    root = tempfile.mkdtemp(prefix="label-") + "/tree"
    runner.build_overlay(CHECKOUT, root, files)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.chdir(root)
    sys.path.insert(0, root)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    harness.install_alarm()
    session = harness.Session()
    failing = []
    started = time.time()
    for test in tests:
        if test["number"] in skip:
            continue
        limit = 60.0
        if cpu is not None:
            limit = max(20.0, 8 * cpu[str(test["number"])] + 20.0)
        if not session.run(test["id"], limit):
            failing.append(test["number"])
    with open(out_path + ".tmp", "w") as handle:
        json.dump({"failing": failing, "skipped": sorted(skip),
                   "wall": round(time.time() - started, 1)}, handle)
    os.replace(out_path + ".tmp", out_path)
    os._exit(0)


def main():
    if sys.argv[1] == "--one":
        one(*sys.argv[2:5])
        return
    tests_path, patch_dir, out_dir = sys.argv[1:4]
    jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 8
    os.makedirs(out_dir, exist_ok=True)
    todo = []
    for patch in sorted(glob.glob(os.path.join(patch_dir, "*.patch"))):
        name = os.path.basename(patch)[:-6]
        out = os.path.join(out_dir, name + ".json")
        if not os.path.exists(out):
            todo.append((patch, out))

    def run(item):
        patch, out = item
        proc = subprocess.run([sys.executable, __file__, "--one", tests_path, patch, out],
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if not os.path.exists(out):
            with open(out + ".err", "w") as handle:
                handle.write(proc.stderr[-4000:])

    started = time.time()
    with ThreadPoolExecutor(jobs) as pool:
        list(pool.map(run, todo))
    print(f"labeled {len(todo)} patches in {time.time() - started:.0f}s")


if __name__ == "__main__":
    main()
