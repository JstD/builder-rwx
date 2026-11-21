"""A diagnostic answer, for a practice submission that is never meant to score well.

The board shows three numbers per run: simulated seconds, CPU seconds and the
count of missed commits. This turns one run into a measurement of where the
normal answer's simulated seconds sit. Each run picks one page at random:

  page 0  Commits that keep two or more tests although no failure was seen get an
          empty answer. The missed count is how many of them really had a failing test.
  page 1  Commits where a failure was seen lose the long tests they kept unrun.
          The missed count is how many of those long tests really fail.

  page 2  Commits where no failure was seen lose the long tests they keep. The
          missed count is how many of those long tests really fail.
  page 3  Commits that keep two or more tests with no failure seen, and change no
          data file, get an empty answer. The missed count is how many really fail.

What the selector itself knows about the page is written into the last five
digits of the simulated milliseconds, by padding answers that cannot be affected
with extra tests until the total ends in those digits:

  page 0  0 CC N I  CC = seconds the emptied commits cost, N = how many there
                    are, I = how many have a module- or class-level change
  page 1  1 LL B D  LL = seconds saved by removing the long tests, B = commits
                    it was done on, D = commits where the drop rule fired
  page 2  2 LL B F  as page 1, F = how many of those commits change a data file
  page 3  3 CC NN   CC = seconds the emptied commits cost, NN = how many there are

Pages 4 to 6 change no answer. They describe the commits that still keep a long
test: "quiet" ones where no failure was seen (delta runs aside), "loud" ones where
one was. Counts and CPU seconds are given as buckets (see the code).

  page 4  4 K C D N  first quiet commit: K = kinds of code change (1 function, 2
                     module or class level, 4 whole file, added), C = candidates,
                     D = candidates that read a changed data file, N = quiet commits
  page 5  5 Q T F L  first quiet commit: Q = quick code candidates, T = their CPU,
                     F = 1 drop rule set + 2 something ran + 4 guard set, L = which
                     long test (1 TPC-DS, 2 max_nodes, 3 both)
  page 7  7 L K E G  first loud commit: L = which long test it keeps, K = kinds,
                     E = quick executor candidates, G as on page 6
  page 8  8 C Q T E  first quiet commit: C = candidates, Q = quick code candidates,
                     T = their CPU, E = quick executor candidates
  page 9  9 C Q T R  first loud commit: C, Q, T as on page 8, R = tests planned
  page 6  6 N K E G  N = loud commits; the first one: K = kinds as above, E = quick
                     executor candidates, G = 1 guard set + 2 a guard test failed
                     + 4 a data file changed
"""

from __future__ import annotations

import math
import os

WORKER_MS = 2000
MODULUS = 100_000


def ms(seconds: float) -> int:
    return math.floor(seconds * 1000 + 0.5)


def cost_ms(workers: list[list[int]], duration: list[int]) -> int:
    loads = [sum(duration[n] for n in worker) for worker in workers if worker]
    return (max(loads) + WORKER_MS * len(loads)) if loads else 0


def subset(values: list[tuple[int, int]], target: int) -> list[int] | None:
    """Test numbers whose durations add up to exactly ``target`` ms."""
    if target == 0:
        return []
    reach = [1]
    mask = (1 << (target + 1)) - 1
    for _, value in values:
        reach.append((reach[-1] | (reach[-1] << value)) & mask)
    if not (reach[-1] >> target) & 1:
        return None
    chosen = []
    left = target
    for index in range(len(values) - 1, -1, -1):
        if (reach[index] >> left) & 1:
            continue  # reachable without this one
        number, value = values[index]
        chosen.append(number)
        left -= value
    return chosen if left == 0 else None


def apply(patches, tests, answers, split, info: dict, page: int | None = None):
    """The workers per commit for a diagnostic run, and a note for the log.
    ``info`` is what the run saw: commits with a failure ("failed"), the kinds of
    change per commit ("kinds"), drop-rule firings ("drops"), the long tests ("long")."""
    INFO = info
    long_tests = set(info.get("long", ()))
    duration = [ms(test["seconds"]) for test in tests]
    seconds = [test["seconds"] for test in tests]
    commits = [path.stem for path in patches]
    if page is None:
        page = 7 + os.urandom(1)[0] % 3  # the three pages that matter now
    workers = {commit: split(sorted(answers[commit]), seconds) for commit in commits}
    before = {commit: cost_ms(workers[commit], duration) for commit in commits}
    touched: set[str] = set()

    facts = INFO.get("facts", {})

    def count_bucket(n):
        return sum(n >= edge for edge in (1, 5, 10, 20, 50, 100, 200, 500, 1000))

    def cpu_bucket(x):
        return sum(x >= edge for edge in (0.5, 1, 2, 4, 6, 10, 20, 40))

    def kind_bits(commit):
        kinds = INFO["kinds"].get(commit, ())
        return ("deferred" in kinds) + 2 * ("immediate" in kinds) + 4 * ("file" in kinds)

    # pages 4 to 6 change no answer: they only describe the commits that keep a long test
    quiet = [c for c in commits if c not in INFO["failed"] and answers[c] & long_tests
             and not facts.get(c, {}).get("delta")]
    loud = [c for c in commits if c in INFO["failed"] and answers[c] & long_tests
            and not facts.get(c, {}).get("delta")]
    if page == 4:
        first = facts.get(quiet[0], {}) if quiet else {}
        digits = ((kind_bits(quiet[0]) if quiet else 0) * 1000 + count_bucket(first.get("cands", 0)) * 100
                  + count_bucket(first.get("data", 0)) * 10 + min(9, len(quiet)))
    elif page == 5:
        first = facts.get(quiet[0], {}) if quiet else {}
        flags = (1 if first.get("drop") else 0) + (2 if first.get("ran") else 0) + (4 if first.get("guard") else 0)
        kept_long = answers[quiet[0]] & long_tests if quiet else set()
        which = sum(1 if seconds[n] < 25 else 2 for n in kept_long)
        digits = (count_bucket(first.get("quick", 0)) * 1000 + cpu_bucket(first.get("quick_cpu", 0.0)) * 100
                  + flags * 10 + min(9, which))
    elif page == 6:
        first = facts.get(loud[0], {}) if loud else {}
        flags = ((1 if first.get("guard") else 0) + (2 if loud and loud[0] in INFO.get("guard_failed", ()) else 0)
                 + (4 if loud and "data" in INFO["kinds"].get(loud[0], ()) else 0))
        digits = (min(9, len(loud)) * 1000 + (kind_bits(loud[0]) if loud else 0) * 100
                  + count_bucket(first.get("exec_quick", 0)) * 10 + flags)
    elif page == 7:
        first = facts.get(loud[0], {}) if loud else {}
        kept_long = answers[loud[0]] & long_tests if loud else set()
        which = sum(1 if seconds[n] < 25 else 2 for n in kept_long)
        flags = ((1 if first.get("guard") else 0)
                 + (2 if loud and loud[0] in INFO.get("guard_failed", ()) else 0)
                 + (4 if loud and "data" in INFO["kinds"].get(loud[0], ()) else 0))
        digits = (which * 1000 + (kind_bits(loud[0]) if loud else 0) * 100
                  + count_bucket(first.get("exec_quick", 0)) * 10 + flags)
    elif page in (8, 9):
        group = quiet if page == 8 else loud
        first = facts.get(group[0], {}) if group else {}
        last = count_bucket(first.get("exec_quick", 0)) if page == 8 else count_bucket(first.get("ran", 0))
        digits = (count_bucket(first.get("cands", 0)) * 1000 + count_bucket(first.get("quick", 0)) * 100
                  + cpu_bucket(first.get("quick_cpu", 0.0)) * 10 + last)
    elif page == 0:
        total = immediate = 0
        for commit in commits:
            # a single kept test is nearly always the one that fails (an edited fixture)
            if len(answers[commit]) >= 2 and commit not in INFO["failed"]:
                touched.add(commit)
                total += before[commit]
                if "immediate" in INFO["kinds"].get(commit, ()):
                    immediate += 1
                workers[commit] = []
        digits = min(99, round(total / 1000)) * 100 + min(9, len(touched)) * 10 + min(9, immediate)
    elif page == 2:
        saved = count = data = 0
        for commit in commits:
            if commit not in INFO["failed"] and answers[commit] & long_tests:
                slim = split(sorted(answers[commit] - long_tests), seconds)
                saved += before[commit] - cost_ms(slim, duration)
                count += 1
                data += "data" in INFO["kinds"].get(commit, ())
                touched.add(commit)
                workers[commit] = slim
        digits = min(99, round(saved / 1000)) * 100 + min(9, count) * 10 + min(9, data)
    elif page == 3:
        total = 0
        for commit in commits:
            if (len(answers[commit]) >= 2 and commit not in INFO["failed"]
                    and "data" not in INFO["kinds"].get(commit, ())):
                touched.add(commit)
                total += before[commit]
                workers[commit] = []
        digits = min(99, round(total / 1000)) * 100 + min(99, len(touched))
    else:
        saved = count = 0
        for commit in commits:
            if commit in INFO["failed"] and answers[commit] & long_tests:
                slim = split(sorted(answers[commit] - long_tests), seconds)
                saved += before[commit] - cost_ms(slim, duration)
                count += 1
                touched.add(commit)
                workers[commit] = slim
        digits = min(99, round(saved / 1000)) * 100 + min(9, count) * 10 + min(9, INFO["drops"])
    wanted = page * 10_000 + digits

    base = sum(cost_ms(workers[commit], duration) for commit in commits)
    extra = (wanted - base) % MODULUS
    while extra < WORKER_MS + 200:
        extra += MODULUS
    # pad commits whose answer is empty and untouched first: nothing can fail there
    order = sorted((c for c in commits if c not in touched), key=lambda c: (bool(workers[c]), c))
    for position, commit in enumerate(order):
        if extra == 0:
            break
        used = {n for worker in workers[commit] for n in worker}
        if len([w for w in workers[commit] if w]) >= 8:
            continue
        pool = [(n, duration[n]) for n in range(len(tests)) if n not in used and (not used or n not in long_tests)]
        pool = [(n, d) for n, d in pool if d > 0]
        top = max((sum(duration[n] for n in worker) for worker in workers[commit]), default=0)
        full = sum(d for _, d in pool)
        if full <= top:
            continue
        whole = full - top + WORKER_MS  # what a worker holding the whole pool adds
        if extra >= whole + WORKER_MS + 200 and position + 1 < len(order):
            workers[commit] = workers[commit] + [[n for n, _ in pool]]
            extra -= whole
            continue
        part = extra
        if extra - WORKER_MS + top > full:
            part = extra // 2  # too much for one worker here, too little to leave a full one
        need = part - WORKER_MS + top  # the new worker's load
        if need <= top or need > full:
            continue
        picked = subset(pool, need)
        if picked is None:
            continue
        workers[commit] = workers[commit] + [picked]
        extra -= part
    final = sum(cost_ms(workers[commit], duration) for commit in commits)
    note = (f"diagnostic page {page}: digits {wanted:05d}, {len(touched)} commits changed, "
            f"total {final} ms, padding left {extra}")
    return workers, note
