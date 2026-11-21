"""Run every test with the harness (no tracing): check it agrees with pytest on the
pinned commit and measure the CPU seconds each test costs us to run in-process.

Usage: python timing.py <tests.json> <out.json>   (cwd = sqlglot checkout)
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.getcwd())

from br import harness  # noqa: E402


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    session = harness.Session()
    harness.install_alarm()
    cpu = {}
    setup = {}
    failed = []
    start = time.time()
    for test in tests:
        file, cls, _ = harness.split_id(test["id"])
        t0 = harness.cpu_now()
        if session.load(file) is not None:
            session.setup_class(file, cls)
        t1 = harness.cpu_now()
        if t1 - t0 > 0.0005:
            setup[f"{file}::{cls}"] = setup.get(f"{file}::{cls}", 0.0) + (t1 - t0)
        ok = session.run(test["id"])
        cpu[test["number"]] = round(harness.cpu_now() - t1, 4)
        if not ok:
            failed.append(test["id"])
    session.finish()
    print(f"ran {len(tests)} tests in {time.time() - start:.1f}s wall, "
          f"{sum(cpu.values()):.1f}s test cpu, failed: {failed}", file=sys.stderr)
    print("teardown_failed", session.teardown_failed, file=sys.stderr)
    json.dump({"cpu": cpu, "setup": setup, "failed": failed}, open(sys.argv[2], "w"))


if __name__ == "__main__":
    main()
