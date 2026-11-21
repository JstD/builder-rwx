"""Trace every test in its own process, several at a time.

Usage: python trace_all.py <tests.json> <out dir> [jobs]   (cwd = sqlglot checkout)
"""

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    tests_path, out_dir = sys.argv[1], sys.argv[2]
    jobs = int(sys.argv[3]) if len(sys.argv) > 3 else os.cpu_count()
    tests = json.load(open(tests_path))["tests"]
    os.makedirs(out_dir, exist_ok=True)
    todo = [t for t in tests if not os.path.exists(os.path.join(out_dir, f"{t['number']}.json"))]
    floor = float(os.environ.get("BR_MIN_SECONDS", "0"))
    todo = [t for t in todo if t["seconds"] >= floor]
    todo.sort(key=lambda t: -t["seconds"])
    started = time.time()
    failed = []

    def one(test):
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "trace_one.py"), tests_path, str(test["number"]), out_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        if proc.returncode != 0:
            failed.append((test["id"], proc.stderr[-2000:]))

    with ThreadPoolExecutor(jobs) as pool:
        list(pool.map(one, todo))
    print(f"traced {len(todo)} tests in {time.time() - started:.0f}s, {len(failed)} crashed")
    for test_id, err in failed[:10]:
        print(test_id, err)


if __name__ == "__main__":
    main()
