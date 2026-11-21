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

import importlib
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
    on_switch = None  # the tracer's callback(key)

    @classmethod
    def switch(cls, key):
        if key != cls.current:
            cls.current = key
            if cls.on_switch is not None:
                cls.on_switch(key)

    @classmethod
    def begin(cls, only=None):
        cls.active = cls.enabled or only is not None
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


class InlinePool:
    """A drop-in for ProcessPoolExecutor that runs each task at submit time.

    Arguments and results still cross a pickle boundary, as they do with real
    worker processes, because sqlglot's own pickling code is under test.
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        self._initializer = initializer
        self._initargs = initargs
        self._ready = False

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
            Items.begin(only)
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
