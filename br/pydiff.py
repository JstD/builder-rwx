"""Work out, from the old and new source of one Python file, which old lines must
run for the change to matter.

The result is a list of anchors over the OLD file's line numbers. A test that never
executes an anchor's lines cannot observe the change the anchor stands for:

  deferred   code inside a function or lambda body: it runs only when called
  immediate  module or class body code: it runs when the module is imported, and
             its effect is whatever the module's objects then carry
  file       the change cannot be localized: anything that touches the file counts

Statements are paired through the diff's changed line blocks, so untouched code is
never compared. When the statement structure of a block changes (statements added,
removed, re-nested), the anchor widens to the smallest enclosing statement or scope.
"""

from __future__ import annotations

import ast
import difflib
import os

ALIGN = os.environ.get("BR_ALIGN", "1") == "1"  # pair statements around ones added or removed

DEFERRED = "deferred"
IMMEDIATE = "immediate"
FILE = "file"

_COMPOUND_BLOCKS = ("body", "orelse", "finalbody")


class Anchor:
    __slots__ = ("kind", "start", "end", "cls", "names", "doc", "probe")

    def __init__(self, kind, start=0, end=0, cls=None, names=(), doc=False, probe=False):
        self.kind = kind
        self.start = start  # inclusive
        self.end = end  # inclusive
        self.cls = cls  # dotted qualname of the enclosing class, for immediate anchors
        self.names = tuple(names)  # names the changed statement binds
        self.doc = doc  # the change is to a docstring
        # True: whoever ran these lines is affected only if the patched module no
        # longer imports (a name used while it loads is gone), which is checked by
        # importing it
        self.probe = probe

    def __repr__(self):
        extra = f" cls={self.cls}" if self.cls else ""
        extra += f" names={list(self.names)}" if self.names else ""
        extra += " doc" if self.doc else ""
        extra += " probe" if self.probe else ""
        return f"<{self.kind} {self.start}-{self.end}{extra}>"


def _first_line(node) -> int:
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        return min(node.lineno, min(d.lineno for d in decorators))
    return node.lineno


def _dump(node) -> str:
    return ast.dump(node, annotate_fields=False)


def _is_string(statement) -> bool:
    """A bare string statement: a docstring when first in a body, otherwise a no-op."""
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Constant)
        and isinstance(statement.value.value, str)
    )


_TRACED = (
    ast.Assign, ast.AugAssign, ast.Return, ast.Raise, ast.If, ast.For, ast.AsyncFor, ast.While,
    ast.With, ast.AsyncWith, ast.Try, ast.TryStar, ast.Delete, ast.Import, ast.ImportFrom,
    ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assert, ast.Match,
)
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
_BINDERS = (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Import, ast.ImportFrom)


def _traced(statement) -> bool:
    """Reaching the statement always leaves one of its lines in a trace."""
    if isinstance(statement, ast.Expr):
        return not isinstance(statement.value, ast.Constant)
    if isinstance(statement, ast.AnnAssign):
        return statement.value is not None
    return isinstance(statement, _TRACED)


def _inert(statement) -> bool:
    """Compiles to nothing that can fail or be seen."""
    return _is_string(statement) or isinstance(statement, ast.Pass)


def _scoping(function):
    """What decides, at compile time, how the names in a function resolve and what
    kind of function it is: the names its own body binds, the ones it declares
    global or nonlocal, and whether it yields or awaits."""
    args = function.args
    bound: set[str] = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
    bound.update(a.arg for a in (args.vararg, args.kwarg) if a is not None)
    declared: set[tuple[str, str]] = set()
    flags: set[str] = set()
    stack = list(function.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (*_DEFS, ast.ClassDef)):
            bound.add(node.name)
            # its decorators, defaults and bases are evaluated here; its body is not ours
            stack.extend(node.decorator_list)
            if isinstance(node, ast.ClassDef):
                stack.extend(node.bases)
                stack.extend(k.value for k in node.keywords)
            else:
                stack.extend(d for d in node.args.defaults + node.args.kw_defaults if d is not None)
            continue
        if isinstance(node, ast.Lambda):
            stack.extend(d for d in node.args.defaults + node.args.kw_defaults if d is not None)
            # a walrus inside still binds in the enclosing function
            stack.extend(n for n in ast.walk(node.body) if isinstance(n, ast.NamedExpr))
            continue
        if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for inner in ast.walk(node):
                if isinstance(inner, ast.NamedExpr) and isinstance(inner.target, ast.Name):
                    bound.add(inner.target.id)
                elif isinstance(inner, (ast.Yield, ast.YieldFrom, ast.Await)):
                    flags.add(type(inner).__name__)
            continue
        if isinstance(node, ast.Name):
            if not isinstance(node.ctx, ast.Load):
                bound.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update((type(node).__name__, name) for name in node.names)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ExceptHandler):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)):
            if node.name:
                bound.add(node.name)
        elif isinstance(node, ast.MatchMapping):
            if node.rest:
                bound.add(node.rest)
        elif isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await, ast.AsyncFor, ast.AsyncWith)):
            flags.add(type(node).__name__)
        stack.extend(ast.iter_child_nodes(node))
    return bound, declared, flags


class _Differ:
    def __init__(self, blocks):
        # blocks are (old start, old end exclusive, new line count), in order
        self.old_ranges = []
        self.new_ranges = []
        delta = 0
        for start, end, added in blocks:
            self.old_ranges.append((start, end))
            self.new_ranges.append((start + delta, start + delta + added))
            delta += added - (end - start)
        self.anchors: list[Anchor] = []

    @staticmethod
    def _touched(ranges, first, last) -> bool:
        for start, end in ranges:
            if start == end:
                # an insertion (or, on the new side, a deletion) point: inside the
                # statement only when it falls between two of its lines
                if first < start <= last:
                    return True
            elif start <= last and end > first:
                return True
        return False

    def touched_old(self, node) -> bool:
        return self._touched(self.old_ranges, _first_line(node), node.end_lineno)

    def touched_new(self, node) -> bool:
        return self._touched(self.new_ranges, _first_line(node), node.end_lineno)

    # scope is ("module", None), ("class", qualname) or ("func", node)
    def widen(self, scope, owner):
        """The structure of a block changed: anchor its owner."""
        kind = scope[0]
        if owner is not None and kind == "func":
            self.anchors.append(Anchor(DEFERRED, _first_line(owner), owner.end_lineno))
        elif kind == "func":
            node = scope[1]
            self.anchors.append(Anchor(DEFERRED, node.body[0].lineno, node.end_lineno))
        elif kind == "class":
            node = owner if owner is not None else scope[2]
            self.anchors.append(
                Anchor(IMMEDIATE, _first_line(node), node.end_lineno, cls=scope[1])
            )
        else:
            self.anchors.append(Anchor(FILE))

    def block(self, old_stmts, new_stmts, scope, owner):
        old_touched = [s for s in old_stmts if self.touched_old(s)]
        new_touched = [s for s in new_stmts if self.touched_new(s)]
        moved = len(old_touched) != len(new_touched) or len(old_stmts) != len(new_stmts)
        if not moved:
            position = {id(s): i for i, s in enumerate(old_stmts)}
            new_position = {id(s): i for i, s in enumerate(new_stmts)}
            moved = any(
                position[id(o)] != new_position[id(n)] for o, n in zip(old_touched, new_touched)
            )
        if moved:
            # statements were added, removed or reordered; equal dumps mean no code changed
            old_dumps = [_dump(s) for s in old_stmts]
            new_dumps = [_dump(s) for s in new_stmts]
            if old_dumps != new_dumps:
                old_code = [d for s, d in zip(old_stmts, old_dumps) if not _is_string(s)]
                new_code = [d for s, d in zip(new_stmts, new_dumps) if not _is_string(s)]
                if old_code == new_code:
                    # only bare strings came or went: nothing but a docstring changed
                    if scope[0] == "class":
                        node = scope[2]
                        self.anchors.append(Anchor(IMMEDIATE, _first_line(node), node.lineno,
                                                   cls=scope[1], names=("__doc__",), doc=True))
                    elif scope[0] == "module" and old_stmts:
                        first = old_stmts[0]
                        self.anchors.append(Anchor(IMMEDIATE, _first_line(first), first.end_lineno,
                                                   names=("__doc__",), doc=True))
                    elif scope[0] == "func":
                        # a function's docstring: no line of it ever runs
                        self.anchors.append(Anchor(DEFERRED, 0, 0, doc=True))
                    return
                if ALIGN and self.align(old_stmts, new_stmts, old_dumps, new_dumps, scope):
                    return
                self.widen(scope, owner)
                if scope[0] == "class":
                    # whoever ran a method that is now gone or different also counts,
                    # however they reached it
                    for statement in old_touched:
                        self.anchors.append(
                            Anchor(DEFERRED, _first_line(statement), statement.end_lineno)
                        )
            return
        for old, new in zip(old_touched, new_touched):
            self.statement(old, new, scope, old_stmts)

    def align(self, old_stmts, new_stmts, old_dumps, new_dumps, scope) -> bool:
        """Statements came or went in this block. Pair the ones that stayed, compare
        the ones that changed in place, and anchor each one that was removed or
        added by itself. False when some part of that is not understood, and the
        caller then anchors the whole scope."""
        low = 0
        while low < len(old_dumps) and low < len(new_dumps) and old_dumps[low] == new_dumps[low]:
            low += 1
        high_old, high_new = len(old_dumps), len(new_dumps)
        while high_old > low and high_new > low and old_dumps[high_old - 1] == new_dumps[high_new - 1]:
            high_old -= 1
            high_new -= 1
        matcher = difflib.SequenceMatcher(None, old_dumps[low:high_old], new_dumps[low:high_new], autojunk=False)
        pending: list[Anchor] = []
        pairs = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            gone = old_stmts[low + i1:low + i2]
            came = new_stmts[low + j1:low + j2]
            if len(gone) == len(came) and all(self._same_thing(a, b) for a, b in zip(gone, came)):
                pairs.extend(zip(gone, came))
                continue
            for statement in gone:
                if not self.removed(statement, scope, pending):
                    return False
            for statement in came:
                if not self.added(statement, old_stmts, low + i1, scope, pending):
                    return False
        self.anchors.extend(pending)
        for old, new in pairs:
            self.statement(old, new, scope, old_stmts)
        return True

    @staticmethod
    def _same_thing(old, new) -> bool:
        if type(old) is not type(new):
            return False
        if isinstance(old, (*_DEFS, ast.ClassDef)):
            return old.name == new.name
        return True

    def removed(self, statement, scope, pending) -> bool:
        kind = scope[0]
        if kind == "func":
            if isinstance(statement, (ast.Global, ast.Nonlocal)):
                return False
            if not _inert(statement):
                # only a test that used to run it can tell it is gone
                pending.append(Anchor(DEFERRED, _first_line(statement), statement.end_lineno))
            return True
        cls = scope[1] if kind == "class" else None
        first = _first_line(statement)
        if isinstance(statement, ast.Pass):
            return True
        if _is_string(statement):
            pending.append(Anchor(IMMEDIATE, first, statement.end_lineno, cls=cls, names=("__doc__",), doc=True))
            return True
        if isinstance(statement, _DEFS):
            # whoever ran it, however they reached it
            pending.append(Anchor(DEFERRED, statement.body[0].lineno, statement.end_lineno))
            if kind == "class":
                # a use of the name while the module loads now fails the import
                pending.append(Anchor(IMMEDIATE, first, statement.lineno, cls=cls, probe=True))
            else:
                # and whoever imports the name or reads it without calling
                pending.append(Anchor(IMMEDIATE, first, statement.lineno, names=(statement.name,)))
            return True
        if isinstance(statement, ast.ClassDef):
            qual = statement.name if kind != "class" else f"{scope[1]}.{statement.name}"
            pending.append(Anchor(IMMEDIATE, first, statement.lineno, cls=qual))
            return True
        if isinstance(statement, _BINDERS):
            names = _bound_names(statement)
            if not names:
                return False
            pending.append(Anchor(IMMEDIATE, first, statement.end_lineno, cls=cls, names=names))
            return True
        return False

    def added(self, statement, old_stmts, index, scope, pending) -> bool:
        """``index`` is where in ``old_stmts`` the new statement now sits."""
        kind = scope[0]
        if kind == "func":
            if isinstance(statement, (ast.Global, ast.Nonlocal)):
                return False
            if _inert(statement):
                return True
            near = self._reached_with(old_stmts, index)
            if near is None:
                return False
            # it runs only where control used to pass this point
            pending.append(Anchor(DEFERRED, _first_line(near), near.end_lineno))
            return True
        if isinstance(statement, ast.Pass):
            return True
        cls = scope[1] if kind == "class" else None
        if kind == "class":
            node = scope[2]
            first, last = _first_line(node), node.lineno  # the class statement: runs with the body
        else:
            near = self._reached_with(old_stmts, index)
            if near is None:
                return False
            first, last = _first_line(near), near.end_lineno
        if _is_string(statement):
            pending.append(Anchor(IMMEDIATE, first, last, cls=cls, names=("__doc__",), doc=True))
            return True
        if isinstance(statement, (*_DEFS, ast.ClassDef)):
            names: tuple[str, ...] = (statement.name,)
        elif isinstance(statement, _BINDERS):
            names = _bound_names(statement)
        else:
            return False
        if not names:
            return False
        pending.append(Anchor(IMMEDIATE, first, last, cls=cls, names=names))
        return True

    @staticmethod
    def _reached_with(old_stmts, index):
        """An old statement of the block that runs whenever control reaches the gap
        before ``old_stmts[index]``: the nearest traced one before it, or the one
        right after it."""
        for k in range(min(index, len(old_stmts)) - 1, -1, -1):
            if _traced(old_stmts[k]):
                return old_stmts[k]
        for k in range(index, len(old_stmts)):
            if _traced(old_stmts[k]):
                return old_stmts[k]
            if not (_inert(old_stmts[k]) or isinstance(old_stmts[k], (ast.Global, ast.Nonlocal))):
                return None
        return None

    def statement(self, old, new, scope, siblings):
        if type(old) is not type(new):
            self.widen(scope, None if scope[0] != "func" else old)
            return
        if isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef)):
            self.function(old, new, scope)
        elif isinstance(old, ast.ClassDef):
            self.klass(old, new, scope)
        elif isinstance(old, (ast.If, ast.While, ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
            self.compound(old, new, scope)
        elif hasattr(old, "handlers"):
            self.try_(old, new, scope)
        elif isinstance(old, ast.Match):
            if _dump(old) != _dump(new):
                self.leaf_changed(old, new, scope, siblings, whole=True)
        else:
            if _dump(old) != _dump(new):
                self.leaf_changed(old, new, scope, siblings)

    def function(self, old, new, scope):
        if old.name != new.name:
            self.widen(scope, None)
            return
        if [_dump(d) for d in old.decorator_list] != [_dump(d) for d in new.decorator_list]:
            self.anchors.append(
                Anchor(
                    IMMEDIATE if scope[0] != "func" else DEFERRED,
                    _first_line(old),
                    old.lineno,
                    cls=scope[1] if scope[0] == "class" else None,
                    names=(old.name,),
                )
            )
        signature_old = _dump(old.args) + (_dump(old.returns) if old.returns else "")
        signature_new = _dump(new.args) + (_dump(new.returns) if new.returns else "")
        if signature_old != signature_new or type(old) is not type(new):
            self.anchors.append(Anchor(DEFERRED, old.body[0].lineno, old.end_lineno))
            return
        if _scoping(old) != _scoping(new):
            # a name became local or stopped being one, or the function turned into a
            # generator: that is decided when it is compiled, for the whole body
            self.anchors.append(Anchor(DEFERRED, old.body[0].lineno, old.end_lineno))
            return
        self.block(old.body, new.body, ("func", old, new), None)

    def klass(self, old, new, scope):
        header_old = [_dump(x) for x in old.bases + old.keywords + old.decorator_list]
        header_new = [_dump(x) for x in new.bases + new.keywords + new.decorator_list]
        qual = old.name if scope[0] != "class" else f"{scope[1]}.{old.name}"
        if old.name != new.name or header_old != header_new:
            if scope[0] == "func":
                self.anchors.append(Anchor(DEFERRED, _first_line(old), old.end_lineno))
            else:
                self.anchors.append(Anchor(IMMEDIATE, _first_line(old), old.lineno, cls=qual))
            return
        if scope[0] == "func":
            # a class built at run time: its body runs when the enclosing code does
            if _dump(old) != _dump(new):
                self.anchors.append(Anchor(DEFERRED, _first_line(old), old.end_lineno))
            return
        self.block(old.body, new.body, ("class", qual, old), None)

    def compound(self, old, new, scope):
        header_old, header_new = [], []
        for field in ("test", "target", "iter", "items"):
            if hasattr(old, field):
                value_old, value_new = getattr(old, field), getattr(new, field)
                if isinstance(value_old, list):
                    header_old += [_dump(v) for v in value_old]
                    header_new += [_dump(v) for v in value_new]
                else:
                    header_old.append(_dump(value_old))
                    header_new.append(_dump(value_new))
        if header_old != header_new:
            last = max(old.lineno, old.body[0].lineno - 1) if old.body else old.end_lineno
            if scope[0] == "func":
                self.anchors.append(Anchor(DEFERRED, old.lineno, last))
            else:
                self.anchors.append(
                    Anchor(IMMEDIATE, old.lineno, last, cls=scope[1] if scope[0] == "class" else None)
                )
        for field in _COMPOUND_BLOCKS:
            block_old, block_new = getattr(old, field, []), getattr(new, field, [])
            if not block_old and not block_new:
                continue
            if bool(block_old) != bool(block_new):
                self.widen(scope, old)
                return
            self.block(block_old, block_new, scope, old)

    def try_(self, old, new, scope):
        def head(handler):
            return (_dump(handler.type) if handler.type else "", handler.name)

        if [head(h) for h in old.handlers] != [head(h) for h in new.handlers]:
            self.widen(scope, old)
            return
        pairs = [(getattr(old, f, []), getattr(new, f, [])) for f in _COMPOUND_BLOCKS]
        pairs += [(ho.body, hn.body) for ho, hn in zip(old.handlers, new.handlers)]
        for block_old, block_new in pairs:
            if bool(block_old) != bool(block_new):
                self.widen(scope, old)
                return
            if block_old:
                self.block(block_old, block_new, scope, old)

    def leaf_changed(self, old, new, scope, siblings, whole=False):
        first, last = _first_line(old), old.end_lineno
        if scope[0] == "func":
            self.anchors.append(Anchor(DEFERRED, first, last))
            return
        cls = scope[1] if scope[0] == "class" else None
        doc = (
            isinstance(old, ast.Expr)
            and isinstance(old.value, ast.Constant)
            and isinstance(old.value.value, str)
            and siblings
            and siblings[0] is old
        )
        if not whole and not doc:
            lambdas = _changes_inside_lambdas(old, new)
            if lambdas is not None:
                for node in lambdas:
                    self.anchors.append(Anchor(DEFERRED, node.body.lineno, node.body.end_lineno))
                return
        if doc:
            names: tuple[str, ...] = ("__doc__",)
        else:
            names = _bound_names(old)
            names += tuple(n for n in _bound_names(new) if n not in names)  # a new name can shadow one
        self.anchors.append(Anchor(IMMEDIATE, first, last, cls=cls, names=names, doc=bool(doc)))


def _bound_names(stmt) -> tuple[str, ...]:
    names: list[str] = []
    targets = []
    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
        return tuple((alias.asname or alias.name).split(".")[0] for alias in stmt.names)
    for target in targets:
        for node in ast.walk(target):
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
    return tuple(names)


def _changes_inside_lambdas(old, new):
    """The lambdas whose bodies hold every difference, or None when some difference
    sits outside any lambda body (so it takes effect when the statement runs)."""
    found: list[ast.Lambda] = []

    def same_or_in_lambda(a, b) -> bool:
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.Lambda):
            if _dump(a.args) != _dump(b.args):
                return False
            if _dump(a.body) != _dump(b.body):
                found.append(a)
            return True
        for field in a._fields:
            value_a, value_b = getattr(a, field, None), getattr(b, field, None)
            if isinstance(value_a, list):
                if not isinstance(value_b, list) or len(value_a) != len(value_b):
                    return False
                for item_a, item_b in zip(value_a, value_b):
                    if isinstance(item_a, ast.AST):
                        if not isinstance(item_b, ast.AST) or not same_or_in_lambda(item_a, item_b):
                            return False
                    elif item_a != item_b or type(item_a) is not type(item_b):
                        return False
            elif isinstance(value_a, ast.AST):
                if not isinstance(value_b, ast.AST) or not same_or_in_lambda(value_a, value_b):
                    return False
            elif value_a != value_b or type(value_a) is not type(value_b):
                return False
        return True

    return found if same_or_in_lambda(old, new) and found else None


class _Stand:
    """Stands in for a class statement that was not parsed: just its lines."""

    def __init__(self, first: int, lineno: int, last: int):
        self.lineno = lineno
        self.end_lineno = last
        self.decorator_list = [_Stand(first, first, first)] if first < lineno else []


def _is_blank(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _cut(lines, start: int, end: int, indent: int, head: bool):
    """Parse lines start..end on their own and give the statements their real line
    numbers. None when they do not stand alone as code."""
    text = "\n".join(lines[start - 1:end]) + "\n"
    if head:
        text += " " * (indent + 4) + "pass\n"
    offset = start - 1
    if indent:
        text = "if 1:\n" + text
        offset -= 1
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    roots = tree.body[0].body if indent else tree.body
    for root in roots:
        ast.increment_lineno(root, offset)
    return roots


def _diff_sliced(old_source: str, new_source: str, blocks, outline) -> list[Anchor] | None:
    """The same anchors as the whole-file comparison, from parsing only the changed
    statements. ``outline`` gives the line span of every module- and class-level
    statement of the old file. None whenever the change is not plainly inside such
    statements (something added between them, a statement split or merged): the
    whole-file comparison then decides."""
    units = outline["units"]
    old_lines = old_source.split("\n")
    new_lines = new_source.split("\n")
    heads = {unit[4].split("|")[1]: unit for unit in units if unit[3] == "head"}

    def unit_at(first: int, last: int, point: bool):
        for unit in units:
            if unit[0] > last:
                break
            if point:
                if unit[0] < first <= unit[1]:
                    return unit
            elif unit[0] <= first and last <= unit[1]:
                return unit
        return None

    touched: list[tuple[list, int, int]] = []  # unit, line shift before it, line shift after it
    delta = 0
    for start, end, added in blocks:
        new_start = start + delta
        unit = unit_at(start, start if start == end else end - 1, start == end)
        if unit is None:
            # between statements: only blank and comment lines may come or go there
            if not all(_is_blank(line) for line in old_lines[start - 1:end - 1]):
                return None
            if not all(_is_blank(line) for line in new_lines[new_start - 1:new_start - 1 + added]):
                return None
            inside = [u for u in units if u[0] <= end - 1 and u[1] >= start] if start != end else []
            if inside:
                return None  # the block reaches into a statement as well
        elif touched and touched[-1][0] is unit:
            touched[-1] = (unit, touched[-1][1], delta + added - (end - start))
        else:
            touched.append((unit, delta, delta + added - (end - start)))
        delta += added - (end - start)

    differ = _Differ(blocks)
    for unit, before, after in touched:
        start, end, indent, kind, chain = unit[:5]
        head = kind == "head"
        old_roots = _cut(old_lines, start, end, indent, head)
        new_roots = _cut(new_lines, start + before, end + after, indent, head)
        if old_roots is None or new_roots is None or len(old_roots) != 1 or len(new_roots) != 1:
            return None
        old, new = old_roots[0], new_roots[0]
        if type(old) is not type(new):
            return None
        outer = chain.split("|")[0] if head else chain
        if outer:
            owner = heads.get(outer)
            if owner is None:
                return None
            scope = ("class", outer, _Stand(owner[0], owner[7], owner[6]))
        else:
            scope = ("module", None)
        if head:
            differ.klass(old, new, scope)
        else:
            differ.statement(old, new, scope, [old] if unit[5] else [None, old])
    return differ.anchors


def diff(old_source: str, new_source: str | None, blocks, outline=None) -> list[Anchor]:
    """Anchors for one changed Python file. An empty list means no code changed.
    ``outline`` (see tools/outline.py) lets the common case skip parsing the file."""
    if new_source is None:
        return [Anchor(FILE)]
    if outline is not None:
        try:
            anchors = _diff_sliced(old_source, new_source, blocks, outline)
        except Exception:
            anchors = None
        if anchors is not None:
            return anchors
    try:
        new_tree = ast.parse(new_source)
    except (SyntaxError, ValueError, RecursionError):
        return [Anchor(FILE)]
    old_tree = ast.parse(old_source)
    differ = _Differ(blocks)
    differ.block(old_tree.body, new_tree.body, ("module", None), None)
    if not differ.anchors:
        # nothing found through the changed blocks: make sure nothing was missed
        if _dump(old_tree) != _dump(new_tree):
            return [Anchor(FILE)]
    return differ.anchors
