"""Per-commit cost of an output file next to the best possible cost.

Usage: python breakdown.py <data dir> <answers.json> <output.jsonl>
"""

import json
import sys


def best_split(seconds):
    if not seconds:
        return 0.0
    ordered = sorted(seconds, reverse=True)
    best = None
    for count in range(1, min(8, len(ordered)) + 1):
        loads = [0.0] * count
        for s in ordered:
            loads[loads.index(min(loads))] += s
        cost = max(loads) + 2 * count
        best = cost if best is None else min(best, cost)
    return best


def main():
    data, answers_path, output = sys.argv[1:4]
    tests = json.load(open(f"{data}/tests.json"))["tests"]
    sec = [t["seconds"] for t in tests]
    answers = json.load(open(answers_path))["commits"]
    total = oracle_total = 0.0
    for line in open(output):
        row = json.loads(line)
        commit = row["commit"]
        workers = [w for w in row["workers"] if w]
        kept = {n for w in workers for n in w}
        cost = (max(sum(sec[n] for n in w) for w in workers) + 2 * len(workers)) if workers else 0.0
        failing = set(answers[commit]["failing"])
        miss = bool(failing - kept)
        oracle = best_split([sec[n] for n in failing])
        total += cost + 300 * miss
        oracle_total += oracle
        flag = "MISS" if miss else ""
        if cost - oracle > 0.3 or miss:
            print(f"{commit} {answers[commit].get('kind', ''):8s} kept={len(kept):4d} fail={len(failing):3d} "
                  f"cost={cost:7.2f} oracle={oracle:7.2f} extra={cost - oracle:7.2f} {flag}")
    print(f"total {total:.1f}  oracle {oracle_total:.1f}  extra {total - oracle_total:.1f}")


if __name__ == "__main__":
    main()
