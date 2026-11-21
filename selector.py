"""Blast Radius selector.

For each patch against the pinned sqlglot commit:

1. Work out which old source lines must execute for the change to matter
   (``br.pydiff``), and look up which tests execute them in maps traced from the
   pinned commit (``maps/``). Data files map to the tests that open them. That is
   the candidate set: a test outside it cannot see the change.
2. Run the cheap candidates on the patched tree (``br.runner``), cheapest first,
   and stop at the first failure. A candidate is dropped only when it actually ran
   and passed; everything not run is kept.
3. Split what is kept across workers to minimise busiest worker + 2 s per worker.

Anything unexpected falls back to keeping more tests, never fewer.
"""

from __future__ import annotations

import json
import os
import select
import sys
import time
import traceback
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from br import patch as patchlib  # noqa: E402
from br import pydiff  # noqa: E402
from br import readers as readerlib  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
CHECKOUT = os.environ.get("BR_CHECKOUT", "/app/sqlglot")
MAPS = Path(os.environ.get("BR_MAPS", os.path.join(HERE, "maps")))
WORK = os.environ.get("BR_WORK", "/tmp/br")

MAX_WORKERS = 8
WORKER_COST = 2.0
SLOTS = 2
SLICED = os.environ.get("BR_SLICED", "1") == "1"  # parse only the changed statements, via maps/outline.json
RUN = os.environ.get("BR_RUN", "1") == "1"  # 0: never run a test, answer from the maps alone
WALL_BUDGET = float(os.environ.get("BR_WALL", "140"))  # seconds for the whole batch
JOB_BASE_LOCAL = 0.15  # CPU seconds to fork a worker and import what a test needs, as timed for the maps
SPEED = float(os.environ.get("BR_SPEED", "1.0"))  # the scorer's CPU seconds per CPU second timed for the maps
JOB_BASE = JOB_BASE_LOCAL * SPEED

# How likely a change of each kind is to break at least one candidate (p), and,
# when it does, how likely each candidate is to be one of the broken (q). They
# only steer how much is run before deciding; they never drop a test.
PRIOR = {
    "data": (0.9, 0.9),
    pydiff.DEFERRED: (0.6, 0.4),
    pydiff.IMMEDIATE: (0.35, 0.2),
    pydiff.FILE: (0.9, 0.8),
}

# The one place a test may be dropped without having been run (BR_DROP=0 turns it off).
# A few tests are so long that keeping one costs more than everything else on the
# commit. When a code change has long tests among its candidates, the quick
# candidates are run as evidence: at least DROP_MIN_CHEAP of them, each costing at
# most EVIDENCE_CPU. Then, while the job stays within DROP_CAP CPU seconds, the
# remaining ones under BIG_SECONDS are run too. If nothing fails, the long tests are
# let go; anything else not run is kept. In the labeled practice commits a long test
# never failed without a quick candidate failing as well.
DROP = os.environ.get("BR_DROP", "1") == "1"
BIG_SECONDS = float(os.environ.get("BR_BIG", "3.0"))
EVIDENCE_CPU = float(os.environ.get("BR_EVIDENCE", "0.3"))
LONG_CPU = float(os.environ.get("BR_LONG_CPU", "10.0"))  # a long test is one that also costs more than this to run; the rest are just run
DROP_CAP = float(os.environ.get("BR_DROP_CAP", "6.0"))
DROP_MIN_CHEAP = int(os.environ.get("BR_DROP_MIN", "5"))

# A commit that edits SQL fixtures and no code is run in delta mode (see
# br/harness.py): each test that reads a changed fixture runs only on the items that
# differ from the pinned file, which takes a fraction of the whole test, and every
# test gets its own verdict. Worth it once keeping the readers would cost DELTA_MIN
# seconds. BR_DELTA=0 turns it off.
DELTA = os.environ.get("BR_DELTA", "1") == "1"
DELTA_MIN = float(os.environ.get("BR_DELTA_MIN", "6.0"))
DELTA_LONG_LIMIT = 15.0  # wall seconds for a test too long to run whole; past it, it is kept
DELTA_ITEM_COST = 1.5  # a guess at the CPU a long test takes in delta mode

WARM = os.environ.get("BR_WARM", "1") == "1"  # start workers from a pre-imported sqlglot core
HEAVY_FILES = ("tests/test_optimizer.py", "tests/test_executor.py")  # these import duckdb and pandas
MAX_SINGLE_RUN = 10.0  # CPU seconds; two workers share 4 GB and the giants use over 1 GB

CONFIG_FILES = {"conftest.py", "pytest.ini", "tox.ini"}
CONFIG_HINTS = ("pytest", "addopts", "testpaths", "filterwarnings")


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def expand(runs) -> set[int]:
    out: set[int] = set()
    for i in range(0, len(runs), 2):
        out.update(range(runs[i], runs[i] + runs[i + 1]))
    return out


class Maps:
    def __init__(self, directory: Path, tests: list[dict]):
        self.directory = directory
        self.index = json.loads((directory / "index.json").read_text())
        self.cov: dict[str, dict | None] = {}
        self.importers = None
        self.outlines = None
        self.tree = None  # ClassTree and readers.Finder, made on first need
        self.finder = None
        # the maps number tests as the pinned tests.json did; translate by id
        by_id = {test["id"]: test["number"] for test in tests}
        self.translate = [by_id.get(test_id) for test_id in self.index["ids"]]
        known = set(self.index["ids"])
        self.unknown = {test["number"] for test in tests if test["id"] not in known}
        self.identity = all(i == n for i, n in enumerate(self.translate)) and not self.unknown

    def tests(self, runs) -> set[int]:
        numbers = expand(runs)
        if self.identity:
            return numbers
        return {self.translate[n] for n in numbers if self.translate[n] is not None}

    def file(self, path: str) -> dict | None:
        if path not in self.cov:
            target = self.directory / "cov" / (path.replace("/", "__") + ".json")
            self.cov[path] = json.loads(target.read_text()) if target.exists() else None
        return self.cov[path]

    def lines(self, path: str, kind: str, start: int, end: int) -> set[int]:
        data = self.file(path)
        if data is None:
            return set()
        table = data[kind]  # "deferred", "immediate" or "runtime"
        ids = {table[str(line)] for line in range(start, end + 1) if str(line) in table}
        out: set[int] = set()
        for set_id in ids:
            out |= self.tests(data["sets"][set_id])
        return out

    def whole_file(self, path: str) -> set[int]:
        data = self.file(path)
        out: set[int] = set()
        if data is not None:
            for runs in data["sets"]:
                out |= self.tests(runs)
        return out

    def outline(self, path: str) -> dict | None:
        """Where the module- and class-level statements of a pinned file are."""
        if self.outlines is None:
            target = self.directory / "outline.json"
            self.outlines = json.loads(target.read_text()) if SLICED and target.exists() else {}
        return self.outlines.get(path)

    def probe_modules(self, paths: list[str]) -> list[str]:
        # The patched modules and what imports them, as importable names.
        if self.importers is None:
            data = json.loads((self.directory / "classes.json").read_text())
            self.importers = data.get("importers", {})
        names: list[str] = []
        for path in paths:
            if not path.startswith(("sqlglot/", "tests/")):
                continue
            module = path[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[: -len(".__init__")]
            seen = [module]
            for current in seen:
                for importer in self.importers.get(current, ()):
                    if importer not in seen and not importer.startswith("tests."):
                        seen.append(importer)
                if len(seen) > 40:
                    break
            # a module nearly everything imports: the package itself stands for the rest
            names += seen if len(seen) <= 40 else [module, "sqlglot"]
        return list(dict.fromkeys(names))

    def readers(self, path: str) -> set[int]:
        runs = self.index["opens"].get(path)
        return self.tests(runs) if runs else set()

    def listers(self, directory: str) -> set[int]:
        runs = self.index["dirs"].get(directory)
        return self.tests(runs) if runs else set()


CLASS_ROOTS = {
    "sqlglot.expressions.core:Expr",
    "sqlglot.dialects.dialect:Dialect",
    "sqlglot.tokens:Tokenizer",
    "sqlglot.parser:Parser",
    "sqlglot.generator:Generator",
}


class ClassTree:
    """Which tests work with a class, for changes to a class body.

    A class-level statement runs once at import, so line coverage only says who
    imported the module. What narrows it is who then used the class: the traces
    record the class of every object whose methods a test called. This is applied
    only to the families whose state is reached through instances (expressions,
    dialects and their tokenizers, parsers and generators).
    """

    def __init__(self, maps: "Maps"):
        data = json.loads((maps.directory / "classes.json").read_text())
        self.maps = maps
        self.mro = data["mro"]
        self.parts = data["parts"]
        self.children: dict[str, set[str]] = {}
        for cls, bases in self.mro.items():
            for base in bases:
                self.children.setdefault(base, set()).add(cls)
        self.owners: dict[str, set[str]] = {}
        for dialect, parts in self.parts.items():
            for part in parts:
                self.owners.setdefault(part, set()).add(dialect)

    def descendants(self, cls: str) -> set[str]:
        seen: set[str] = set()
        stack = [cls]
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(self.children.get(current, ()))
        return seen

    def users(self, path: str, qualname: str) -> set[int] | None:
        """Tests that used the class or anything built on it; None when unknown."""
        module = path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        cls = f"{module}:{qualname}"
        if cls not in self.mro:
            return None
        if not (set(self.mro[cls]) | {cls}) & CLASS_ROOTS:
            return None
        related = self.descendants(cls)
        for member in list(related):
            # a tokenizer, parser or generator is also reached through its dialect
            for dialect in self.owners.get(member, ()):
                related |= self.descendants(dialect)
            # and a dialect's settings are copied onto the parts made for it
            for part in self.parts.get(member, ()):
                if part not in CLASS_ROOTS and not part.startswith("sqlglot.dialects.dialect:"):
                    related |= self.descendants(part)
        types = self.maps.index["types"]
        out: set[int] = set()
        for member in related:
            runs = types.get(member)
            if runs:
                out |= self.maps.tests(runs)
        return out


class Decision:
    """What one commit needs: candidates, the files of its patched tree, a prior."""

    def __init__(self, commit: str):
        self.commit = commit
        self.candidates: set[int] = set()
        self.broad: set[int] = set()  # the candidates before any import-time narrowing
        self.probe: list[str] = []  # modules that must still import for the narrowing to hold
        self.everything = False  # could not analyse: keep every test
        self.files: dict[str, str | None] = {}
        self.runnable = True  # False when the patched tree cannot be built
        self.kinds: set[str] = set()
        self.notes: list[str] = []
        self.drop_when_clean: set[int] = set()  # see DROP: long tests to let go, set by plan()
        self.delta_files: list[str] = []  # SQL fixtures edited in place
        self.delta = False  # see DELTA: nothing but those changed
        self.delta_run = False  # set by plan(): run every candidate in delta mode


def read_pinned(path: str) -> str | None:
    try:
        with open(os.path.join(CHECKOUT, path), encoding="utf-8", newline="") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def analyse(commit: str, text: str, maps: Maps) -> Decision:
    decision = Decision(commit)
    import_reads: set[str] = set()
    other_changes = False
    for file_patch in patchlib.parse(text):
        old_path, new_path = file_patch.old_path, file_patch.new_path
        if old_path is None and new_path is None:
            decision.everything = True
            continue
        name = os.path.basename(new_path or old_path)
        if name in CONFIG_FILES or (
            name in ("pyproject.toml", "setup.cfg")
            and any(hint in text for hint in CONFIG_HINTS)
        ):
            decision.everything = True
            decision.notes.append(f"test configuration changed: {name}")
            continue

        if old_path != new_path:
            # a file appearing or disappearing is visible to whoever lists its directory
            for path in (old_path, new_path):
                if path is not None:
                    listers = maps.listers(os.path.dirname(path))
                    if listers:
                        decision.candidates |= listers
                        decision.kinds.add("data")

        old_text = read_pinned(old_path) if old_path is not None else None
        try:
            if old_path is not None and old_text is None:
                raise patchlib.PatchError("pinned file missing or not text")
            new_text, blocks = patchlib.apply(old_text, file_patch)
        except patchlib.PatchError as error:
            decision.notes.append(f"{old_path or new_path}: {error}")
            decision.runnable = False
            if old_path is not None and maps.readers(old_path):
                decision.candidates |= maps.readers(old_path)
                decision.kinds.add("data")
            if (old_path or "").endswith(".py") or (new_path or "").endswith(".py"):
                decision.everything = True
            continue

        if new_path is not None:
            decision.files[new_path] = new_text
        if old_path == new_path and new_text == old_text:
            continue  # nothing in the file changed (a mode change, say)
        if old_path == new_path and old_path.startswith("tests/fixtures/") and old_path.endswith(".sql"):
            decision.delta_files.append(old_path)
        else:
            other_changes = True
        if old_path is not None:
            # tests that read the old file as data see any change to it
            readers = maps.readers(old_path)
            if readers:
                decision.candidates |= readers
                decision.kinds.add("data")
        if old_path is not None and old_path != new_path:
            decision.files[old_path] = None

        if old_path is not None and old_path.endswith(".py"):
            if maps.file(old_path) is None:
                continue  # no test executes this file
            renamed = new_text if old_path == new_path else None
            for anchor in pydiff.diff(old_text, renamed, blocks, maps.outline(old_path)):
                decision.kinds.add(anchor.kind)
                if anchor.kind == pydiff.FILE:
                    found = maps.whole_file(old_path)
                else:
                    found = maps.lines(old_path, anchor.kind, anchor.start, anchor.end)
                decision.broad |= found
                if anchor.probe:
                    decision.notes.append(f"{old_path}: {anchor!r}")
                    continue
                if anchor.kind == pydiff.IMMEDIATE and found and (anchor.cls or anchor.names):
                    # ran at import: narrow from "imported it" to "can read what it set"
                    if maps.tree is None:
                        maps.tree = ClassTree(maps)
                        maps.finder = readerlib.Finder(CHECKOUT, maps, maps.tree)
                    tree, finder = maps.tree, maps.finder
                    reading = finder.find(old_path, anchor.cls, anchor.names)
                    if reading is not None:
                        found &= reading
                        import_reads |= finder.import_reads
                    elif anchor.doc:
                        found = set()  # a docstring is reachable only as __doc__, which nothing reads
                    elif anchor.cls:  # no read found by name: fall back to who used the class
                        users = tree.users(old_path, anchor.cls)
                        if users is not None:
                            found &= users
                decision.candidates |= found
                decision.notes.append(f"{old_path}: {anchor!r} -> {len(found)}")
    decision.broad |= decision.candidates
    if decision.broad != decision.candidates:
        decision.probe = maps.probe_modules(
            [path for path, new in decision.files.items() if new is not None and path.endswith(".py")]
        )
        # and every module that reads a changed name while it loads: when that read
        # fails, everything importing the module fails with it
        for path in sorted(import_reads):
            if decision.files.get(path, "") is None or not path.endswith(".py"):
                continue
            module = path[:-3].replace("/", ".")
            if module.endswith(".__init__"):
                module = module[: -len(".__init__")]
            if module not in decision.probe:
                decision.probe.append(module)
    decision.delta = (bool(decision.delta_files) and not other_changes and decision.runnable
                      and not decision.everything and decision.kinds == {"data"})
    if decision.candidates:
        decision.candidates |= maps.unknown
    if decision.broad:
        decision.broad |= maps.unknown
    return decision


def approx_split(total: float, biggest: float) -> float:
    if total <= 0:
        return 0.0
    return min(max(biggest, total / k) + WORKER_COST * k for k in range(1, MAX_WORKERS + 1))


def split(tests: list[int], seconds: list[float]) -> list[list[int]]:
    """Longest first onto the lightest worker, for the worker count that scores best."""
    if not tests:
        return []
    ordered = sorted(tests, key=lambda n: -seconds[n])
    best = None
    for count in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        loads = [0.0] * count
        workers: list[list[int]] = [[] for _ in range(count)]
        for number in ordered:
            lightest = loads.index(min(loads))
            workers[lightest].append(number)
            loads[lightest] += seconds[number]
        cost = max(loads) + WORKER_COST * count
        if best is None or cost < best[0] - 1e-9:
            best = (cost, workers)
    return best[1]


def plan(decision: Decision, tests: list[dict], seconds: list[float], run_cost: list[float],
         setup_cost: dict[str, float]) -> list[int]:
    """The candidates worth running, in order. Empty means keep them all unrun."""
    candidates = sorted(decision.candidates)
    decision.drop_when_clean = set()
    decision.delta_run = False
    if not candidates or not decision.runnable or decision.everything:
        return []
    if DELTA and decision.delta:
        keep_all = approx_split(sum(seconds[n] for n in candidates), max(seconds[n] for n in candidates))
        if keep_all >= DELTA_MIN:
            decision.delta_run = True
            # Longest first: in delta mode a long test costs little to run, and once
            # one is known to fail the shorter ones may fit beside it for free.
            return sorted(candidates, key=lambda n: (-seconds[n], n))
    if DROP and "data" not in decision.kinds:
        long_tests = {n for n in candidates if seconds[n] >= BIG_SECONDS and run_cost[n] > LONG_CPU}
        if long_tests:
            def by_cost(n):
                # classes with a costly setUpClass go last: a failure found before
                # them saves paying for it
                return (setup_cost.get(tests[n]["id"].rsplit("::", 1)[0], 0.0) > 0.5, run_cost[n], n)

            quick = sorted((n for n in candidates if n not in long_tests and run_cost[n] <= EVIDENCE_CPU), key=by_cost)
            rest = sorted((n for n in candidates if n not in long_tests and run_cost[n] > EVIDENCE_CPU), key=by_cost)
            seen_classes: set[str] = set()

            def price(n):
                key = tests[n]["id"].rsplit("::", 1)[0]
                extra = 0.0 if key in seen_classes else setup_cost.get(key, 0.0)
                seen_classes.add(key)
                return run_cost[n] + extra

            total = JOB_BASE + sum(price(n) for n in quick)
            if len(quick) >= DROP_MIN_CHEAP and total <= DROP_CAP:
                to_run = list(quick)
                for n in rest:
                    before = set(seen_classes)
                    step = price(n)
                    if total + step > DROP_CAP:
                        seen_classes.clear()
                        seen_classes.update(before)
                        break
                    total += step
                    to_run.append(n)
                decision.drop_when_clean = long_tests
                return to_run
    p, q = 0.0, 0.0
    for kind in decision.kinds:
        kind_p, kind_q = PRIOR[kind]
        p = 1 - (1 - p) * (1 - kind_p)
        q = max(q, kind_q)
    if len(candidates) == 1:
        q = 1.0

    # the first test run from a class also pays for its setUpClass
    cost = {n: run_cost[n] for n in candidates}
    cheapest_in_class: dict[str, int] = {}
    for n in candidates:
        key = tests[n]["id"].rsplit("::", 1)[0]
        if key in setup_cost and (key not in cheapest_in_class or cost[n] < cost[cheapest_in_class[key]]):
            cheapest_in_class[key] = n
    for key, n in cheapest_in_class.items():
        cost[n] += setup_cost[key]
    order = sorted(candidates, key=lambda n: (cost[n], n))

    count = len(order)
    suffix_total = [0.0] * (count + 1)
    suffix_big = [0.0] * (count + 1)
    for i in range(count - 1, -1, -1):
        suffix_total[i] = suffix_total[i + 1] + seconds[order[i]]
        suffix_big[i] = max(suffix_big[i + 1], seconds[order[i]])
    keep_all = approx_split(suffix_total[0], suffix_big[0])

    best_k, best_cost = 0, keep_all
    spent = 0.0  # CPU to run the first k
    until_failure = 0.0  # expected CPU before the first failure, if the change breaks tests
    survive = 1.0
    for k in range(1, count + 1):
        step = cost[order[k - 1]]
        if step > MAX_SINGLE_RUN:
            break  # the longest tests also need the most memory: they are kept, never run
        spent += step
        until_failure += step * survive
        survive *= 1 - q
        harmless = JOB_BASE + spent + approx_split(suffix_total[k], suffix_big[k])
        harmful = JOB_BASE + until_failure + keep_all
        expected = (1 - p) * harmless + p * harmful
        if expected < best_cost - 1e-9:
            best_k, best_cost = k, expected
    return order[:best_k]


def solve(patches: list[Path], tests: list[dict], answers: dict[str, set[int]], started: float):
    seconds = [test["seconds"] for test in tests]
    everything = set(range(len(tests)))
    maps = Maps(MAPS, tests)
    traced_cpu = maps.index["cpu"]
    run_cost = list(seconds)
    for map_number, number in enumerate(maps.translate):
        if number is not None:
            run_cost[number] = traced_cpu[map_number] * SPEED
    setup_cost = {k: v * SPEED for k, v in maps.index["class_setup_cpu"].items()}

    jobs = []
    for path in patches:
        commit = path.stem
        try:
            text = path.read_bytes().decode("utf-8", "surrogateescape")
            decision = analyse(commit, text, maps)
        except Exception:
            log(f"{commit}: analysis failed\n{traceback.format_exc()}")
            continue
        if decision.everything:
            log(f"{commit}: keeping every test ({'; '.join(decision.notes)})")
            continue
        answers[commit] = set(decision.candidates)
        to_run = plan(decision, tests, seconds, run_cost, setup_cost)
        log(f"{commit}: {len(decision.candidates)} candidates, running {len(to_run)} "
            f"[{' '.join(sorted(decision.kinds))}] {'; '.join(decision.notes)}")
        if to_run or decision.probe:
            jobs.append((decision, to_run))

    log(f"analysed {len(patches)} commits, {len(jobs)} to run: {sum(os.times()[:4]):.2f}s cpu so far")
    if not RUN:
        # Answer from the maps alone. Without the import check the narrowing of
        # import-time changes is not verified, so those fall back to the wider set.
        for decision, _ in jobs:
            if decision.probe:
                answers[decision.commit] = set(decision.broad)
        return
    if not jobs:
        return

    from br import runner

    # duckdb and pandas are worth loading once only if more than one job needs them
    heavy = sum(1 for _, to_run in jobs if any(tests[n]["file"] in HEAVY_FILES for n in to_run))
    runner.preload(heavy=heavy >= 2)
    log(f"preloaded: {sum(os.times()[:4]):.2f}s cpu so far")
    os.makedirs(WORK, exist_ok=True)

    # A warm slot has sqlglot's core imported already, from the pinned checkout. A
    # job may start there when it patches none of the files that import covered,
    # which is true of most dialect, parser and generator changes.
    def patched_source(decision):
        return [path for path in decision.files if path.startswith("sqlglot/")]

    def could_be_warm(decision):
        return all(path.endswith(".py") and decision.files[path] is not None
                   for path in patched_source(decision))

    hopeful = sum(1 for decision, _ in jobs if could_be_warm(decision))
    warm_slots = []
    if WARM and hopeful >= 2:
        warm_slots = [runner.Slot(warm=CHECKOUT) for _ in range(2 if hopeful >= 8 else 1)]
        waiting = list(warm_slots)
        limit = time.monotonic() + 30.0
        while waiting and time.monotonic() < limit:
            ready, _, _ = select.select([s.out_r for s in waiting], [], [], 1.0)
            for slot in list(waiting):
                if slot.out_r in ready:
                    events = slot.read()
                    if slot.core is not None or ("E",) in events:
                        waiting.remove(slot)
        for slot in list(warm_slots):
            if not slot.core:
                slot.close()
                warm_slots.remove(slot)
    core = set.intersection(*(slot.core for slot in warm_slots)) if warm_slots else set()
    warm_queue, cold_queue = [], []
    for job in jobs:
        decision = job[0]
        if warm_slots and could_be_warm(decision) and not core.intersection(patched_source(decision)):
            warm_queue.append(job)
        else:
            cold_queue.append(job)
    warm_queue.reverse()
    cold_queue.reverse()
    slots = warm_slots + [runner.Slot() for _ in range(min(SLOTS, len(cold_queue)))]
    log(f"{len(warm_queue)} jobs start warm, {len(cold_queue)} cold: {sum(os.times()[:4]):.2f}s cpu so far")
    running = 0
    deadline = started + WALL_BUDGET

    def start(slot):
        nonlocal running
        queue = warm_queue if slot.warm else cold_queue
        while queue:
            decision, to_run = queue.pop()
            root = os.path.join(WORK, decision.commit)
            try:
                runner.remove_overlay(root)
                runner.build_overlay(CHECKOUT, root, decision.files)
            except Exception:
                log(f"{decision.commit}: overlay failed\n{traceback.format_exc()}")
                continue
            # A test's limit is generous (a timeout only ever keeps tests) and must
            # cover what the first test of a class also pays for: importing the test
            # module and its setUpClass, which for the executor loads every table.
            def limit_for(n):
                setup = setup_cost.get(tests[n]["id"].rsplit("::", 1)[0], 0.0)
                if decision.delta_run and run_cost[n] > MAX_SINGLE_RUN:
                    return round(DELTA_LONG_LIMIT * max(1.0, SPEED) + 6 * setup, 2)
                return round(6 * (run_cost[n] + setup) + 5.0 * SPEED, 2)

            limits = [(n, tests[n]["id"], limit_for(n)) for n in to_run]
            wall = min(sum(limit for _, _, limit in limits) + 10.0, max(5.0, deadline - time.monotonic()))
            job = {"root": root, "tests": limits, "stop": not decision.delta_run, "wall": wall + 20.0,
                   "probe": decision.probe, "warm": slot.warm}
            if decision.delta_run:
                job["delta"] = {"pinned": CHECKOUT, "files": decision.delta_files,
                                "pool": str(MAPS / "pool_inputs.json"),
                                "seconds": [seconds[n] for n in to_run],
                                "cost": [min(run_cost[n], DELTA_ITEM_COST) for n in to_run]}
            slot.submit(job,
                        {"decision": decision, "root": root, "failed": False,
                         "probed": not decision.probe,
                         "since": time.monotonic(), "ran": 0, "planned": len(to_run),
                         "expected": sum(run_cost[n] for n in to_run)})
            running += 1
            return

    def widen(handle):
        # a patched module no longer imports: every test that imports it is in play
        decision = handle["decision"]
        log(f"{decision.commit}: a patched module fails to import, keeping all {len(decision.broad)}")
        answers[decision.commit] = set(decision.broad)
        handle["failed"] = True
        handle["probed"] = True

    def pump():
        for slot in slots:
            if slot.job is None and running < SLOTS:
                start(slot)

    try:
        pump()
        while running:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                log("out of time: keeping every candidate not yet cleared")
                break
            ready, _, _ = select.select([s.out_r for s in slots if s.job is not None], [], [], timeout)
            for slot in slots:
                if slot.job is None or slot.out_r not in ready:
                    continue
                for event in slot.read():
                    handle = slot.job
                    if handle is None:
                        break
                    if event[0] == "R":
                        _, number, ok = event
                        handle["ran"] += 1
                        if not ok:
                            log(f"{handle['decision'].commit}: FAILED {tests[number]['id']}")
                        if ok and (not handle["failed"] or handle["decision"].delta_run):
                            # in delta mode each test stands for itself
                            answers[handle["decision"].commit].discard(number)
                        else:
                            handle["failed"] = True
                    elif event[0] == "P":
                        handle["probed"] = True
                        if not event[1]:
                            widen(handle)
                    else:
                        if not handle["probed"]:
                            widen(handle)  # the worker died before the imports finished
                        decision = handle["decision"]
                        if (decision.drop_when_clean and not handle["failed"]
                                and handle["ran"] == handle["planned"]):
                            log(f"{decision.commit}: the evidence passed, letting go of "
                                f"{len(decision.drop_when_clean)} long tests")
                            answers[decision.commit] -= decision.drop_when_clean
                        log(f"{handle['decision'].commit}: ran {handle['ran']}/{handle['planned']} in "
                            f"{time.monotonic() - handle['since']:.2f}s (expected {handle['expected']:.2f}s)")
                        runner.remove_overlay(handle["root"])
                        slot.job = None
                        running -= 1
                        pump()
    finally:
        for slot in slots:
            slot.close()
        try:
            import shutil

            shutil.rmtree(WORK, ignore_errors=True)
        except Exception:
            pass


def emit(patches, tests, answers) -> None:
    seconds = [test["seconds"] for test in tests]
    lines = []
    for path in patches:
        kept = sorted(answers[path.stem])
        lines.append(json.dumps({"commit": path.stem, "workers": split(kept, seconds)},
                                separators=(",", ":")))
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    sys.stdout.flush()


def main() -> None:
    started = time.monotonic()
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    patches = sorted((DATA / "commits").glob("*.patch"))
    everything = set(range(len(tests)))
    # every commit starts as "run everything"; analysis only ever narrows it
    answers: dict[str, set[int]] = {path.stem: set(everything) for path in patches}

    def last_resort(signum, frame):
        # Something is stuck well past the budget. What has been cleared so far is
        # still right, so answer with it and leave; the container ends with us.
        log("hard timeout: answering with what is cleared so far")
        emit(patches, tests, answers)
        os._exit(0)

    import signal

    timed = hasattr(signal, "SIGALRM")  # absent only where this is run for development
    if timed:
        signal.signal(signal.SIGALRM, last_resort)
        signal.setitimer(signal.ITIMER_REAL, WALL_BUDGET + 12.0)
    try:
        solve(patches, tests, answers, started)
    except Exception:
        log(f"selector failed, keeping what is safe\n{traceback.format_exc()}")
    if timed:
        signal.setitimer(signal.ITIMER_REAL, 0)
    emit(patches, tests, answers)
    log(f"answered {len(patches)} commits in {time.monotonic() - started:.1f}s wall, "
        f"{sum(os.times()[:4]):.1f}s cpu")


if __name__ == "__main__":
    main()
