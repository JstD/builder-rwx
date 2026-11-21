"""Runs sqlglot's unittest-style tests one at a time, giving the verdict pytest would.

Every test in the suite is a ``unittest.TestCase`` method, so pytest's verdict for
``file::Class::method`` is: the file imports, ``setUpClass`` works, and the method
runs with no failure, error or failed subtest. This module reproduces that without
pytest's startup cost, and is shared by the map builder (build time, with tracing)
and the selector (run time, on patched trees).

sqlglot's optimizer and executor tests push work through a spawned
``ProcessPoolExecutor``. ``InlinePool`` stands in for it: the same pickling
boundary, no extra processes, so the work is traceable and costs one CPU.

Many of the slow tests are one loop over independent items: the pairs of a ``.sql``
fixture, or the queries handed to the pool. ``Items`` follows which item is running
(for tracing) and can restrict a test to chosen items (for running): an item whose
code never reaches a changed line behaves as it did on the pinned commit, so only
the others need to run to know the test's verdict.
"""

from __future__ import annotations

import hashlib
import importlib
import os
import pickle
import signal
import sys
import time
import unittest
from concurrent.futures import Future

LOADERS = ("load_sql_fixture_pairs", "load_sql_fixtures")


class Abort(BaseException):
    """Raised by the alarm when a test overruns its time limit."""


class Items:
    """Which item of the running test is executing, and which are allowed to.

    Keys are ("L", fixture, index) for the index-th item a fixture loader yields
    and ("P", index) for the index-th task given to the pool outside any such item.
    """

    enabled = False  # off unless a tracer or an item filter asks for it
    active = False  # enabled, and a test method is running
    current = None  # key of the item whose code is running, or None
    submitted = 0  # pool tasks seen in this test that belong to no loader item
    only_pool = None  # indices allowed to run, or None for all
    only_loader = None  # fixture -> indices allowed, or None for all
    only_checked = False  # an item outside only_pool is skipped only with its pinned input
    on_switch = None  # the tracer's callback(key)

    # Delta mode, for a commit that changes fixture files and no code. Whatever a
    # test does with an item it was also given on the pinned commit, it does as it
    # did there. So a loader yields only the items of a changed file that differ
    # from the pinned file, and a pool task whose pickled input equals the pinned
    # one is not run.
    delta_pinned = None  # the pinned checkout, when delta mode is on
    delta_files = frozenset()  # changed files, relative to the checkout
    pool_inputs = {}  # test id -> {"init": digest, "tasks": [digest, ...]} on the pinned commit
    test_id = None
    record = None  # a list: pool inputs are recorded here instead of being run

    @classmethod
    def switch(cls, key):
        if key != cls.current:
            cls.current = key
            if cls.on_switch is not None:
                cls.on_switch(key)

    @classmethod
    def begin(cls, only=None, test_id=None):
        cls.test_id = test_id
        cls.active = cls.enabled or only is not None or cls.delta_pinned is not None or cls.record is not None
        if not cls.active:
            return
        cls.submitted = 0
        cls.only_pool = cls.only_loader = None
        if only is not None:
            cls.only_pool = set(only.get("P", ()))
            cls.only_loader = {name: set(indices) for name, indices in only.get("L", {}).items()}
        cls.switch(None)

    @classmethod
    def end(cls):
        if not cls.active:
            return
        cls.switch(None)
        cls.active = False
        cls.only_pool = cls.only_loader = None


def wrap_loader(loader):
    """A fixture loader that reports each item it yields and skips filtered ones."""

    def wrapped(filename, *args, **kwargs):
        allowed = None
        if Items.active and Items.only_loader is not None:
            allowed = Items.only_loader.get(filename, ())
        elif Items.active and Items.delta_pinned is not None:
            allowed = _changed_items(loader, filename, args, kwargs)
        for index, item in enumerate(loader(filename, *args, **kwargs)):
            if allowed is not None and index not in allowed:
                continue
            if Items.active:
                Items.switch(("L", filename, index))
            yield item
        if Items.active:
            Items.switch(None)

    wrapped.__wrapped__ = loader
    return wrapped


def _changed_items(loader, filename, args, kwargs):
    """Indices of the items the loader gives now that it did not give, in the same
    place, from the pinned file. None when that cannot be worked out, and then
    every item counts."""
    scope = getattr(loader, "__globals__", {})
    directory = scope.get("FIXTURES_DIR")
    if not isinstance(directory, str):
        return None
    relative = os.path.relpath(os.path.join(directory, filename), os.getcwd()).replace(os.sep, "/")
    if relative not in Items.delta_files:
        return None
    try:
        new = list(loader(filename, *args, **kwargs))
        scope["FIXTURES_DIR"] = os.path.join(Items.delta_pinned, os.path.relpath(directory, os.getcwd()))
        try:
            old = list(loader(filename, *args, **kwargs))
        finally:
            scope["FIXTURES_DIR"] = directory
    except Abort:
        raise
    except BaseException:
        return None
    if len(old) != len(new):
        return None
    return {index for index, (a, b) in enumerate(zip(old, new)) if a != b}


def _digest(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class InlinePool:
    """A drop-in for ProcessPoolExecutor that runs each task at submit time.

    Arguments and results still cross a pickle boundary, as they do with real
    worker processes, because sqlglot's own pickling code is under test.
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        self._initializer = initializer
        self._initargs = initargs
        self._ready = False
        self._init_digest = None

    def _start(self):
        if not self._ready:
            self._ready = True
            if self._initializer is not None:
                self._initializer(*pickle.loads(pickle.dumps(self._initargs)))

    def submit(self, fn, /, *args, **kwargs):
        future = _Future()
        outer = Items.current
        if Items.active:
            if outer is not None and outer[0] == "L":
                future.item = outer
            else:
                future.item = ("P", Items.submitted)
                Items.submitted += 1
                if Items.only_pool is not None and future.item[1] not in Items.only_pool:
                    # Not selected: it cannot reach the change, so it would give what
                    # it gave on the pinned commit. The tests take None as "no result".
                    # When the code has changed that also needs the item's input to be
                    # what it was: the change may have run while the input was made.
                    if not Items.only_checked or self._same_as_pinned(future.item[1], fn, args, kwargs):
                        future.set_result(None)
                        return future
                if Items.record is not None or Items.delta_pinned is not None:
                    if self._same_as_pinned(future.item[1], fn, args, kwargs):
                        future.set_result(None)
                        return future
                Items.switch(future.item)
        try:
            self._start()
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            result = pickle.loads(pickle.dumps(fn(*args, **kwargs)))
        except Exception as error:
            future.set_exception(error)
        else:
            future.set_result(result)
        finally:
            if Items.active:
                Items.switch(outer)
        return future

    def _same_as_pinned(self, index, fn, args, kwargs) -> bool:
        """Whether this task has the input it had on the pinned commit (and so the
        result). When recording, notes the input and says yes, so nothing runs."""
        try:
            task = _digest(pickle.dumps((fn, args, kwargs)))
            if self._init_digest is None:
                self._init_digest = _digest(pickle.dumps(self._initargs))
        except Abort:
            raise
        except BaseException:
            return False
        if Items.record is not None:
            Items.record.append((Items.test_id, index, self._init_digest, task))
            return True
        known = Items.pool_inputs.get(Items.test_id)
        if not known or known.get("init") != self._init_digest:
            return False
        tasks = known.get("tasks", ())
        return index < len(tasks) and tasks[index] == task

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        futures = [self.submit(fn, *args) for args in zip(*iterables)]
        return (future.result() for future in futures)

    def shutdown(self, wait=True, *, cancel_futures=False):
        if Items.active and Items.current is not None and Items.current[0] == "P":
            Items.switch(None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown()
        return False


class _Future(Future):
    item = None

    def result(self, timeout=None):
        # what the test does with a result belongs to the item that produced it
        if Items.active and self.item is not None:
            Items.switch(self.item)
        return super().result(timeout)


class _Verdict(unittest.TestResult):
    """Collects whether anything went wrong for one test, subtests included."""

    def __init__(self):
        super().__init__()
        self.bad = False
        self.notes = []

    def addError(self, test, err):
        self.bad = True
        self.notes.append(err)

    def addFailure(self, test, err):
        self.bad = True
        self.notes.append(err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.bad = True
            self.notes.append(err)

    def addUnexpectedSuccess(self, test):
        self.bad = True


class SharedTables:
    """The executor test class loads 32 CSV tables in its setUpClass, in pure Python,
    which costs about a second of CPU in every job that runs one of its tests. The
    rows depend only on the CSV files and the schema in tests/helpers.py, so they
    are parsed once, before the workers fork (``parse``), and a job whose patch
    touches nothing under tests/ builds the class from them (``setup``). Everything
    else setUpClass does is done as written there, with the patched code."""

    FILE = "tests/test_executor.py"
    CLASS = "TestExecutor"
    # sha1 of the pinned setUpClass source: the shortcut is only for exactly that code
    SOURCE = "120988b74899ec199c4f9f167fd14db3924750a2"
    rows = None  # {"tpch": {table: [row tuples]}, "tpcds": {...}}
    enabled = False  # per job

    @classmethod
    def parse(cls, checkout: str) -> None:
        import ast
        import csv
        import gzip
        import runpy

        helpers = runpy.run_path(os.path.join(checkout, "tests", "helpers.py"))
        fixtures = os.path.join(checkout, "tests", "fixtures")

        def open_file(file_name):
            with open(file_name, "rb") as f:
                gzipped = f.read(2) == b"\x1f\x8b"
            if gzipped:
                return gzip.open(file_name, "rt", newline="")
            return open(file_name, encoding="utf-8", newline="")

        def load(directory, schema):
            tables = {}
            for table in schema:
                # the loop of the test's own setup(), unchanged
                reader = csv.reader(open_file(f"{directory}{table}.csv.gz"), delimiter="|")
                rows = []
                ctypes = []
                next(reader)
                for row in reader:
                    if not ctypes:
                        for i, v in enumerate(row):
                            try:
                                ctypes.append(type(ast.literal_eval(v)))
                            except (ValueError, SyntaxError):
                                ctypes.append(str)
                    rows.append(
                        tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row))
                    )
                tables[table] = rows
            return tables

        cls.rows = {
            "tpch": load(fixtures + "/optimizer/tpc-h/", helpers["TPCH_SCHEMA"]),
            "tpcds": load(fixtures + "/optimizer/tpc-ds/", helpers["TPCDS_SCHEMA"]),
        }

    @classmethod
    def usable(cls, file: str, cls_name: str, module) -> bool:
        if cls.rows is None or not cls.enabled or (file, cls_name) != (cls.FILE, cls.CLASS):
            return False
        try:
            with open(module.__file__, encoding="utf-8") as handle:
                source = handle.read()
            start = source.index("    @classmethod\n    def setUpClass(cls):")
            end = source.index("    @classmethod\n    def tearDownClass(cls):")
            return hashlib.sha1(source[start:end].encode("utf-8")).hexdigest() == cls.SOURCE
        except (OSError, ValueError):
            return False

    @classmethod
    def setup(cls, test_class, module) -> None:
        """What the pinned setUpClass does, with the rows already parsed."""
        test_class.tpch_conn = module.duckdb.connect()
        test_class.tpcds_conn = module.duckdb.connect()
        test_class.tpch_tables = {}
        test_class.tpcds_tables = {}

        def setup(conn, directory, table, columns, tables, parsed):
            file_name = f"{directory}{table}.csv.gz"

            conn.execute(
                f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})
                """
            )
            tables[table] = module.Table(columns=columns, rows=list(parsed[table]))

        for table, columns in module.TPCH_SCHEMA.items():
            setup(test_class.tpch_conn, module.DIR_TPCH, table, columns, test_class.tpch_tables, cls.rows["tpch"])

        for table, columns in module.TPCDS_SCHEMA.items():
            setup(test_class.tpcds_conn, module.DIR_TPCDS, table, columns, test_class.tpcds_tables, cls.rows["tpcds"])

        test_class.cache = {}
        test_class.tpch_sqls = list(module.load_sql_fixture_pairs("optimizer/tpc-h/tpc-h.sql"))
        test_class.tpcds_sqls = list(module.load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))


def module_name(file: str) -> str:
    return file[:-3].replace("/", ".")


def split_id(test_id: str):
    file, cls, method = test_id.split("::")
    return file, cls, method


class Session:
    """Loads test modules and runs tests in any order, handling class fixtures once."""

    def __init__(self, on_module=None):
        self.modules = {}  # file -> module or None when it cannot be imported
        self.classes = {}  # (file, cls) -> True/False once setUpClass has run
        self.class_objects = []
        self.on_module = on_module
        self.teardown_failed = []

    def load(self, file: str):
        if file not in self.modules:
            try:
                module = importlib.import_module(module_name(file))
                if hasattr(module, "ProcessPoolExecutor"):
                    module.ProcessPoolExecutor = InlinePool
                for name in LOADERS:
                    loader = getattr(module, name, None)
                    if loader is not None and not hasattr(loader, "__wrapped__"):
                        setattr(module, name, wrap_loader(loader))
                self.modules[file] = module
            except Abort:
                raise
            except BaseException as error:  # pytest reports a collection error
                self.modules[file] = None
                print(f"import of {file} failed: {error!r}", file=sys.stderr)
            if self.on_module is not None:
                self.on_module(file, self.modules[file])
        return self.modules[file]

    def setup_class(self, file: str, cls_name: str) -> bool:
        key = (file, cls_name)
        if key not in self.classes:
            cls = getattr(self.modules[file], cls_name)
            ok = True
            if not getattr(cls, "__unittest_skip__", False):
                try:
                    if SharedTables.usable(file, cls_name, self.modules[file]):
                        SharedTables.setup(cls, self.modules[file])
                    else:
                        cls.setUpClass()
                except Abort:
                    raise
                except BaseException as error:
                    ok = False
                    print(f"setUpClass of {file}::{cls_name} failed: {error!r}", file=sys.stderr)
                    try:
                        cls.doClassCleanups()
                    except BaseException:
                        pass
            self.classes[key] = ok
            if ok:
                self.class_objects.append((key, cls))
        return self.classes[key]

    def run(self, test_id: str, limit: float | None = None, only=None) -> bool:
        """True when the test passes. ``limit`` is a wall-clock cap in seconds;
        ``only`` restricts the test to some of its items (see ``Items``)."""
        file, cls_name, method = split_id(test_id)
        armed = False
        try:
            if limit is not None:
                signal.setitimer(signal.ITIMER_REAL, limit)
                armed = True
            module = self.load(file)
            if module is None:
                return False
            cls = getattr(module, cls_name, None)
            if cls is None or not hasattr(cls, method):
                return False
            if not self.setup_class(file, cls_name):
                return False
            verdict = _Verdict()
            Items.begin(only, test_id)
            try:
                cls(method).run(verdict)
            finally:
                Items.end()
            return not verdict.bad
        except Abort:
            return False
        finally:
            if armed:
                signal.setitimer(signal.ITIMER_REAL, 0)

    def finish(self):
        """Class teardown. A failure here is one pytest pins on the class's last test."""
        for key, cls in reversed(self.class_objects):
            if getattr(cls, "__unittest_skip__", False):
                continue
            try:
                cls.tearDownClass()
                cls.doClassCleanups()
                if getattr(cls, "tearDown_exceptions", None):
                    self.teardown_failed.append(key)
            except BaseException:
                self.teardown_failed.append(key)
        self.class_objects.clear()


def _alarm(signum, frame):
    raise Abort()


def install_alarm():
    signal.signal(signal.SIGALRM, _alarm)


def cpu_now() -> float:
    return time.process_time()
