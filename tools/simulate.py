"""Replay the selector's run plan against ground truth, with no tests run.

For each labeled commit this takes the candidates from the analysis, lets the
planner choose what to run, and plays the run out from the labels: tests run
cheapest first, stop at the first one that fails, everything not run is kept.
It reports simulated seconds plus our estimated CPU, so the planner's settings
can be compared in seconds instead of minutes.

Usage: python simulate.py <data dir> [<data dir> ...]
       (each has tests.json, commits/, answers.json; BR_CHECKOUT, BR_MAPS as usual)
"""

from __future__ import annotations

import glob
import itertools
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import selector  # noqa: E402
from br import pydiff  # noqa: E402


MAPS: dict[int, object] = {}  # id(tests) -> the maps they were loaded with


def load(data_dir):
    tests = json.load(open(os.path.join(data_dir, "tests.json")))["tests"]
    answers = json.load(open(os.path.join(data_dir, "answers.json")))["commits"]
    maps = selector.Maps(selector.MAPS, tests)
    seconds = [t["seconds"] for t in tests]
    run_cost = list(seconds)
    for map_number, number in enumerate(maps.translate):
        if number is not None:
            run_cost[number] = maps.index["cpu"][map_number]
    setup = dict(maps.index["class_setup_cpu"])
    MAPS[id(tests)] = maps
    rows = []
    for patch in sorted(glob.glob(os.path.join(data_dir, "commits", "*.patch"))):
        name = os.path.basename(patch)[:-6]
        if name not in answers:
            continue
        text = open(patch, encoding="utf-8", newline="").read()
        decision = selector.analyse(name, text, maps)
        rows.append((decision, set(answers[name]["failing"]), answers[name].get("kind", "")))
    return tests, seconds, run_cost, setup, rows


def play(tests, seconds, run_cost, setup, decision, failing):
    everything = set(range(len(tests)))
    if decision.everything:
        kept = everything
        cpu = 0.0
    else:
        to_run = selector.plan(decision, tests, seconds, run_cost, setup, MAPS.get(id(tests)))
        guard_tests = set().union(*decision.guards.values()) if decision.guards else set()
        kept = set(decision.candidates)
        cpu = selector.JOB_BASE_LOCAL if (to_run or decision.probe) else 0.0
        classes = set()
        passed = set()
        failed = guard_failed = False
        for number in to_run:
            if number in decision.restricted:
                cpu += decision.restricted[number][1]  # an exact answer for the long test
                if number not in failing:
                    kept.discard(number)
                continue
            if not decision.delta_run and (guard_failed if number in guard_tests else failed):
                continue
            key = tests[number]["id"].rsplit("::", 1)[0]
            if key in setup and key not in classes:
                classes.add(key)
                cpu += setup[key]
            if decision.delta_run:
                # every test runs, on the changed items only: a guess at the cost
                cpu += min(run_cost[number], 1.5)
                if number not in failing:
                    kept.discard(number)
                continue
            cpu += run_cost[number]
            if number in failing:
                failed = True
                guard_failed = guard_failed or number in guard_tests
                continue
            kept.discard(number)
            passed.add(number)
        if not failed and not decision.delta_run:
            kept -= decision.drop_when_clean
        for long_test, guard in decision.guards.items():
            if guard <= passed:
                kept.discard(long_test)
    workers = selector.split(sorted(kept), seconds)
    cost = (max(sum(seconds[n] for n in w) for w in workers) + 2 * len(workers)) if workers else 0.0
    miss = 300.0 if failing - kept else 0.0
    return cost, cpu, miss


def total(data, verbose=False):
    out = [0.0, 0.0, 0.0]
    for tests, seconds, run_cost, setup, rows in data:
        for decision, failing, kind in rows:
            cost, cpu, miss = play(tests, seconds, run_cost, setup, decision, failing)
            out[0] += cost
            out[1] += cpu
            out[2] += miss
            if verbose and (cost + cpu > 3 or miss):
                print(f"   {decision.commit} {kind:14s} cand={len(decision.candidates):4d} "
                      f"fail={len(failing):4d} sim={cost:6.2f} cpu={cpu:5.2f} {'MISS' if miss else ''} "
                      f"{sorted(decision.kinds)}")
    return out


def main():
    data = [load(d) for d in sys.argv[1:]]
    for (tests, *_), name in zip(data, sys.argv[1:]):
        print(name, len(data[sys.argv[1:].index(name)][4]), "commits")
    base = total(data, verbose=True)
    print(f"current settings: simulated {base[0]:.1f} + cpu {base[1]:.1f} + misses {base[2]:.0f} "
          f"= {sum(base):.1f}")
    kinds = [pydiff.DEFERRED, pydiff.IMMEDIATE, "data"]
    original = dict(selector.PRIOR)
    grid = [0.2, 0.4, 0.6, 0.8, 0.95]
    for kind in kinds:
        best = None
        for p, q in itertools.product(grid, [0.05, 0.2, 0.5, 0.9]):
            selector.PRIOR = dict(original)
            selector.PRIOR[kind] = (p, q)
            score = sum(total(data))
            if best is None or score < best[0]:
                best = (score, p, q)
        print(f"best for {kind}: p={best[1]} q={best[2]} -> {best[0]:.1f}")
    selector.PRIOR = original


if __name__ == "__main__":
    main()
