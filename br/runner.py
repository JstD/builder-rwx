"""Run tests against patched copies of sqlglot, cheaply.

A slot is a long-lived process that has already imported the heavy third-party
modules the tests need (duckdb, pandas, numpy) but never sqlglot. For each job it
forks a worker; the worker puts the job's patched tree first on ``sys.path``,
imports sqlglot from it fresh, runs the listed tests in order and reports each
verdict. Nothing from one commit can leak into the next, and the per-commit cost
is one import of sqlglot.

The patched tree is an overlay in /tmp: symlinks to the pinned checkout, with real
directories only along the paths of the changed files.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time

from br import harness

PRELOAD = (
    "unittest", "unittest.mock", "decimal", "sqlite3", "csv", "gzip", "tempfile",
    "datetime", "concurrent.futures", "multiprocessing", "pickle", "logging",
)
PRELOAD_HEAVY = ("duckdb", "numpy", "pandas", "pandas.testing")  # optimizer and executor tests


def preload(heavy: bool = True):
    for name in PRELOAD + (PRELOAD_HEAVY if heavy else ()):
        try:
            __import__(name)
        except Exception as error:  # the tests that need it will fail and be kept
            print(f"preload of {name} failed: {error!r}", file=sys.stderr)
    # Everything loaded so far lives for the whole run. Freezing it keeps the workers'
    # garbage collections from walking it again and again (sqlglot's tests allocate a
    # lot), and keeps forked pages from being copied just to update GC bookkeeping.
    import gc

    gc.collect()
    gc.freeze()


def build_overlay(base: str, dest: str, files: dict[str, str | None]):
    """``files`` maps a path relative to ``base`` to its new text (None = deleted)."""
    tree: dict = {}
    for path, text in files.items():
        node = tree
        parts = path.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError(f"file used as directory: {path}")
        node[parts[-1]] = (text,)

    def fill(src: str, dst: str, node: dict):
        os.mkdir(dst)
        existing = os.listdir(src) if os.path.isdir(src) else []
        for name in existing:
            if name not in node:
                os.symlink(os.path.join(src, name), os.path.join(dst, name))
        for name, value in node.items():
            if isinstance(value, dict):
                fill(os.path.join(src, name), os.path.join(dst, name), value)
            elif value[0] is not None:
                with open(os.path.join(dst, name), "w", encoding="utf-8", newline="") as handle:
                    handle.write(value[0])

    fill(base, dest, tree)


def remove_overlay(dest: str):
    shutil.rmtree(dest, ignore_errors=True)


def _rebase(checkout: str, root: str):
    """Make the already imported, unpatched sqlglot modules belong to the patched tree.

    A warm slot imported sqlglot's core from the pinned checkout. None of those
    modules is patched in this job, so their state is what a fresh import from the
    patched tree would give. What still points at the pinned checkout is where they
    say they live, and that decides where the modules not yet imported (the lazily
    loaded dialects, the patched one among them) are looked for.
    """
    prefix = checkout.rstrip("/") + "/"

    def move(path):
        if isinstance(path, str) and path.startswith(prefix):
            return root + "/" + path[len(prefix):]
        return path

    for name, module in list(sys.modules.items()):
        if name != "sqlglot" and not name.startswith("sqlglot."):
            continue
        for attr in ("__file__", "__cached__"):
            value = getattr(module, attr, None)
            if value:
                setattr(module, attr, move(value))
        if hasattr(module, "__path__"):
            module.__path__ = [move(path) for path in module.__path__]
        spec = getattr(module, "__spec__", None)
        if spec is not None:
            if spec.origin:
                spec.origin = move(spec.origin)
            if spec.cached:
                spec.cached = move(spec.cached)
            if spec.submodule_search_locations is not None:
                spec.submodule_search_locations = [move(path) for path in spec.submodule_search_locations]
        loader = getattr(module, "__loader__", None)
        if loader is not None and isinstance(getattr(loader, "path", None), str):
            loader.path = move(loader.path)
    sys.path[:] = [path for path in sys.path if path.rstrip("/") != checkout.rstrip("/")]


def _work(job: dict, out_fd: int):
    """Runs in a forked worker; never returns."""
    try:
        os.setpgid(0, 0)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        root = job["root"]
        os.environ["SKIP_INTEGRATION"] = "0"
        os.chdir(root)
        if job.get("warm"):
            _rebase(job["warm"], root)
        sys.path.insert(0, root)
        harness.install_alarm()
        probe = job.get("probe") or ()
        if probe:
            # The candidates were narrowed on the assumption that the patched modules
            # still import. Check that first, with everything that imports them.
            import importlib

            ok = True
            signal.setitimer(signal.ITIMER_REAL, job.get("probe_limit", 20.0))
            try:
                for name in probe:
                    importlib.import_module(name)
            except BaseException:
                ok = False
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
            os.write(out_fd, b"P %d\n" % (1 if ok else 0))
            if not ok:
                os._exit(0)
        session = harness.Session()
        for number, test_id, limit in job["tests"]:
            ok = session.run(test_id, limit)
            os.write(out_fd, b"R %d %d\n" % (number, 1 if ok else 0))
            if not ok and job.get("stop", True):
                break
    except BaseException:
        pass
    finally:
        os._exit(0)


def _serve(job_fd: int, out_fd: int, warm: str | None = None):
    """The slot's loop: one job at a time, each in a forked worker."""
    current = [0]
    if warm:
        # Import sqlglot's core once, from the pinned checkout, and say which files
        # that covered: a job that patches none of them can start from here.
        files: list[str] = []
        try:
            sys.path.insert(0, warm)
            import sqlglot  # noqa: F401

            prefix = warm.rstrip("/") + "/"
            for name, module in list(sys.modules.items()):
                path = getattr(module, "__file__", None)
                if (name == "sqlglot" or name.startswith("sqlglot.")) and path and path.startswith(prefix):
                    files.append(path[len(prefix):])
            import gc

            gc.collect()
            gc.freeze()
        except BaseException:
            files = []
        os.write(out_fd, ("W " + json.dumps(sorted(files)) + "\n").encode("utf-8"))
        if not files:
            os._exit(0)

    def kill_worker(*_):
        pid = current[0]
        if pid:
            for target in (-pid, pid):
                try:
                    os.kill(target, signal.SIGKILL)
                except OSError:
                    pass

    def terminate(*_):
        kill_worker()
        os._exit(0)

    signal.signal(signal.SIGALRM, kill_worker)
    signal.signal(signal.SIGTERM, terminate)
    reader = os.fdopen(job_fd, "r", encoding="utf-8")
    for line in reader:
        job = json.loads(line)
        pid = os.fork()
        if pid == 0:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            _work(job, out_fd)
        current[0] = pid
        signal.setitimer(signal.ITIMER_REAL, job.get("wall", 60.0))
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        signal.setitimer(signal.ITIMER_REAL, 0)
        current[0] = 0
        try:
            os.killpg(pid, signal.SIGKILL)  # anything the worker left running
        except OSError:
            pass
        os.write(out_fd, b"E\n")
    os._exit(0)


class Slot:
    """``warm`` is the pinned checkout to import sqlglot's core from, or None for a
    slot whose workers import everything themselves."""

    def __init__(self, warm: str | None = None):
        self.warm = warm
        self.core: set[str] | None = None  # files of the modules a warm slot holds
        job_r, job_w = os.pipe()
        out_r, out_w = os.pipe()
        self.pid = os.fork()
        if self.pid == 0:
            os.close(job_w)
            os.close(out_r)
            try:
                # stdout belongs to the answers: nothing below this process may write to it
                os.dup2(os.open(os.devnull, os.O_WRONLY), 1)
                _serve(job_r, out_w, warm)
            finally:
                os._exit(0)
        os.close(job_r)
        os.close(out_w)
        self.job_w = job_w
        self.out_r = out_r
        self.buffer = b""
        self.job = None  # the caller's handle for the running job

    def submit(self, job: dict, handle):
        self.job = handle
        os.write(self.job_w, (json.dumps(job) + "\n").encode("utf-8"))

    def read(self):
        """Lines available now: ('R', number, ok) and ('E',)."""
        data = os.read(self.out_r, 65536)
        if not data:
            return [("E",)]
        self.buffer += data
        *lines, self.buffer = self.buffer.split(b"\n")
        events = []
        for line in lines:
            parts = line.split()
            if parts and parts[0] == b"R":
                events.append(("R", int(parts[1]), parts[2] == b"1"))
            elif parts and parts[0] == b"P":
                events.append(("P", parts[1] == b"1"))
            elif parts and parts[0] == b"W":
                self.core = set(json.loads(line[2:].decode("utf-8")))
                events.append(("W",))
            elif parts and parts[0] == b"E":
                events.append(("E",))
        return events

    def close(self):
        try:
            os.close(self.job_w)
        except OSError:
            pass
        try:
            os.kill(self.pid, signal.SIGTERM)
            os.waitpid(self.pid, 0)
        except OSError:
            pass
