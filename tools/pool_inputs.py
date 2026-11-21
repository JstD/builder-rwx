"""Record what the pool-driven tests hand to their pool on the pinned commit.

Two executor tests send each query of a fixture to a process pool and compare
what comes back. For a commit that only edits fixtures, a query whose pickled
input is what it was on the pinned commit gives what it gave there, so it need
not be run. This writes those inputs' digests to maps/pool_inputs.json. Nothing
is executed while recording: the pool notes each task and returns no result.

Usage: python pool_inputs.py <out file> <test id> [<test id> ...]
(cwd = the pinned checkout, with the repo on sys.path as for the other map tools)
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from br import harness  # noqa: E402


def main():
    out_path, test_ids = sys.argv[1], sys.argv[2:]
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path.insert(0, os.getcwd())
    harness.install_alarm()
    session = harness.Session()
    result = {}
    for test_id in test_ids:
        harness.Items.record = []
        ok = session.run(test_id, 600.0)
        notes = harness.Items.record
        harness.Items.record = None
        if not ok or not notes or len({init for _, _, init, _ in notes}) != 1:
            print(f"{test_id}: not recorded (ok={ok}, {len(notes)} tasks)", file=sys.stderr)
            continue
        if [index for _, index, _, _ in notes] != list(range(len(notes))):
            print(f"{test_id}: tasks out of order", file=sys.stderr)
            continue
        result[test_id] = {"init": notes[0][2], "tasks": [task for _, _, _, task in notes]}
        print(f"{test_id}: {len(notes)} tasks", file=sys.stderr)
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, indent=0, sort_keys=True)
        handle.write("\n")
    session.finish()


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    os._exit(0)
