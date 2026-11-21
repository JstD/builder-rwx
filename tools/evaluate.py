"""Check the selector's candidate sets against ground truth, with no tests run.

For every labeled practice commit: is each failing test a candidate? Also writes
the labeled commits as a data dir (tests.json, commits/, answers.json) so the
whole image can be run and scored on them like the sample.

Usage: python evaluate.py <tests.json> <patch dir> <label dir> <out data dir>
(BR_CHECKOUT = pinned checkout, BR_MAPS = maps)
"""

import glob
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import selector  # noqa: E402


def main():
    tests_path, patch_dir, label_dir, out_dir = sys.argv[1:5]
    tests = json.load(open(tests_path))["tests"]
    seconds = [t["seconds"] for t in tests]
    maps = selector.Maps(selector.MAPS, tests)
    kinds = {}
    index = os.path.join(patch_dir, "index.txt")
    if os.path.exists(index):
        for line in open(index):
            name, kind, *_ = line.split()
            kinds[name] = kind
    os.makedirs(os.path.join(out_dir, "commits"), exist_ok=True)
    shutil.copy(tests_path, os.path.join(out_dir, "tests.json"))
    answers = {}
    missed = 0
    spent = 0.0
    rows = []
    for label in sorted(glob.glob(os.path.join(label_dir, "*.json"))):
        name = os.path.basename(label)[:-5]
        patch = os.path.join(patch_dir, name + ".patch")
        truth = json.load(open(label))
        failing = set(truth["failing"])
        answers[name] = {"failing": sorted(failing), "kind": kinds.get(name, "")}
        shutil.copy(patch, os.path.join(out_dir, "commits", name + ".patch"))
        text = open(patch, encoding="utf-8", newline="").read()
        started = time.process_time()
        decision = selector.analyse(name, text, maps)
        spent += time.process_time() - started
        candidates = set(range(len(tests))) if decision.everything else decision.candidates
        lost = failing - candidates
        rows.append((name, kinds.get(name, ""), len(failing), len(candidates),
                     sum(seconds[n] for n in candidates), lost, decision))
        if lost:
            missed += 1
    for name, kind, fail, cands, cost, lost, decision in rows:
        flag = "MISSED " + " ".join(tests[n]["id"].split("::", 1)[1] for n in sorted(lost)[:4]) if lost else ""
        print(f"{name} {kind:16s} fail={fail:4d} cand={cands:4d} sec={cost:7.2f} {flag}")
        if lost:
            print("      ", "; ".join(decision.notes))
    json.dump({"commits": answers}, open(os.path.join(out_dir, "answers.json"), "w"))
    print(f"{len(rows)} commits, {missed} with a failing test outside the candidates, "
          f"analysis {spent:.1f}s cpu")


if __name__ == "__main__":
    main()
