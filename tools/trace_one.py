"""Trace one sqlglot test in a fresh process, on the pinned commit.

Records, for the test run alone:
  run     lines of sqlglot/tests code executed while no module import was in progress
  imp_body  lines of module and class bodies executed while a module was being
            imported, lazily loaded dialects included
  imp_call  lines of functions and lambdas executed during those imports
  types   classes of the objects (or classes) whose methods were called
  import_types  classes of the objects whose methods were called during imports
  opens   files under the checkout that were opened, dirs that were listed

Usage: python trace_one.py <tests.json> <test number> <out dir>   (cwd = checkout)
"""

from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.getcwd()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from br import harness  # noqa: E402

mon = sys.monitoring
TOOL = mon.COVERAGE_ID
EV = mon.events
DISABLE = mon.DISABLE
PREFIX = ROOT + os.sep

file_ids: dict[str, int] = {}  # co_filename -> index, or -1 when outside the checkout
files: list[str] = []
run_lines: set[int] = set()
imp_body: set[int] = set()
imp_call: set[int] = set()
types: set[type] = set()
import_types: set[type] = set()
stack: list[int] = []
method_arg: dict[object, str | None] = {}
opens: set[str] = set()
dirs: set[str] = set()

# With BR_ITEMS=1 the lines a test runs while one of its items is executing (a
# fixture pair, a pool task) are kept per item instead of in ``run``.
target: set[int] = run_lines
item_lines: dict[tuple, set[int]] = {}
item_time: dict[tuple, float] = {}
clock = [None, 0.0]  # current item key, time it started


def on_switch(key):
    global target
    now = time.perf_counter()
    item_time[clock[0]] = item_time.get(clock[0], 0.0) + now - clock[1]
    clock[0], clock[1] = key, now
    target = run_lines if key is None else item_lines.setdefault(key, set())
    mon.restart_events()


def file_id(name: str) -> int:
    idx = file_ids.get(name)
    if idx is None:
        if name.startswith(PREFIX) and name.endswith(".py"):
            idx = len(files)
            files.append(name[len(PREFIX):].replace(os.sep, "/"))
        else:
            idx = -1
        file_ids[name] = idx
    return idx


def on_line(code, line):
    idx = file_id(code.co_filename)
    if idx < 0:
        return DISABLE
    if stack:
        # CO_OPTIMIZED marks function code; module and class bodies do not have it.
        (imp_call if code.co_flags & 1 else imp_body).add((idx << 16) | line)
        return None
    target.add((idx << 16) | line)
    return DISABLE


def on_start(code, offset):
    name = code.co_name
    if name == "<module>":
        if file_id(code.co_filename) >= 0:
            stack.append(file_ids[code.co_filename])
            if len(stack) == 1:
                mon.restart_events()
        return None
    arg = method_arg.get(code)
    if arg is None:
        arg = ""
        if file_id(code.co_filename) >= 0 and code.co_argcount > 0:
            first = code.co_varnames[0]
            if first in ("self", "cls", "klass", "mcs"):
                arg = first
        method_arg[code] = arg
    if not arg:
        return DISABLE
    try:
        obj = sys._getframe(1).f_locals[arg]
    except Exception:
        return None
    if stack:
        # While modules are imported every class gets set up, which says nothing
        # about use; an object having its methods called does.
        if not isinstance(obj, type):
            import_types.add(type(obj))
        return None
    types.add(obj if isinstance(obj, type) else type(obj))
    return None


def on_return(code, offset, retval):
    if code.co_name == "<module>":
        _pop(code)
        return None
    return DISABLE


def on_unwind(code, offset, exc):
    if code.co_name == "<module>":
        _pop(code)


def _pop(code):
    if file_ids.get(code.co_filename, -1) >= 0 and stack:
        stack.pop()
        if not stack:
            mon.restart_events()


def audit(event, args):
    if event == "open":
        path = args[0]
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if isinstance(path, str) and not path.endswith(".pyc"):
            path = os.path.abspath(path)
            if path.startswith(PREFIX):
                opens.add(path[len(PREFIX):])
    elif event in ("os.listdir", "os.scandir", "glob.glob", "glob.glob/2"):
        path = args[0]
        if isinstance(path, bytes):
            path = os.fsdecode(path)
        if isinstance(path, str):
            caller = sys._getframe(1).f_code.co_filename
            if caller.startswith("<frozen importlib"):
                return
            path = os.path.abspath(path or ".")
            if (path + os.sep).startswith(PREFIX):
                dirs.add(path[len(PREFIX):])


def decode(lines):
    out: dict[str, list[int]] = {}
    for value in lines:
        out.setdefault(files[value >> 16], []).append(value & 0xFFFF)
    for value in out.values():
        value.sort()
    return out


def main():
    tests = json.load(open(sys.argv[1]))["tests"]
    test = tests[int(sys.argv[2])]
    out_dir = sys.argv[3]

    harness.install_alarm()
    sys.addaudithook(audit)
    mon.use_tool_id(TOOL, "blast-radius")
    mon.register_callback(TOOL, EV.LINE, on_line)
    mon.register_callback(TOOL, EV.PY_START, on_start)
    mon.register_callback(TOOL, EV.PY_RETURN, on_return)
    mon.register_callback(TOOL, EV.PY_UNWIND, on_unwind)
    mon.set_events(TOOL, EV.LINE | EV.PY_START | EV.PY_RETURN | EV.PY_UNWIND)

    started = time.time()
    clock[1] = time.perf_counter()
    if os.environ.get("BR_ITEMS"):
        harness.Items.on_switch = on_switch
        harness.Items.enabled = True
    session = harness.Session()
    ok = session.run(test["id"])
    session.finish()
    mon.set_events(TOOL, 0)

    result = {
        "number": test["number"],
        "id": test["id"],
        "ok": ok,
        "wall": round(time.time() - started, 3),
        "run": decode(run_lines),
        "imp_body": decode(imp_body),
        "imp_call": decode(imp_call),
        "types": sorted(
            f"{t.__module__}:{t.__qualname__}"
            for t in types
            if getattr(t, "__module__", "").split(".")[0] in ("sqlglot", "tests")
        ),
        "import_types": sorted(
            f"{t.__module__}:{t.__qualname__}"
            for t in import_types
            if getattr(t, "__module__", "").split(".")[0] in ("sqlglot", "tests")
        ),
        "opens": sorted(opens),
        "dirs": sorted(dirs),
    }
    if os.environ.get("BR_ITEMS"):
        on_switch(None)
        mon.set_events(TOOL, 0)
        result["items"] = {":".join(map(str, key)): decode(lines) for key, lines in item_lines.items()}
        result["item_time"] = {":".join(map(str, key)): round(spent, 4)
                               for key, spent in item_time.items() if key is not None}
        result["base_time"] = round(item_time.get(None, 0.0), 4)
    path = os.path.join(out_dir, f"{test['number']}.json")
    with open(path + ".tmp", "w") as handle:
        json.dump(result, handle, separators=(",", ":"))
    os.replace(path + ".tmp", path)


if __name__ == "__main__":
    main()
