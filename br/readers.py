"""Who reads a name that a module- or class-level statement binds?

When ``WEEK_OFFSET = -1`` changes in a dialect class, line coverage can only say
which tests imported the module. The value matters to a test only if the test
executes code that reads ``WEEK_OFFSET``. This module finds those reading lines by
scanning the pinned sources, following the value when it is merely handed on:

  self.x = self.NAME          the value now also lives in attribute ``x``
  f(numeric_escapes=NAME)     ... in a parameter called ``numeric_escapes``
  OTHER = [.. NAME ..]        (at module/class level) ... in ``OTHER``
  from mod import NAME        ... in the importing module's global

Every other mention is a use, and the tests that execute a use are the readers.
Each place the value lands also says who can reach it there: a value that stays
on objects of the changed class is bounded by the tests that used that class.

``None`` means the reads could not be pinned down and nothing may be narrowed.
"""

from __future__ import annotations

import ast
import bisect
import os
import re

MAX_ELEMENTS = 40
MAX_DEPTH = 4
MAX_PARSE_BYTES = 2_500_000
SLICED = os.environ.get("BR_SLICED", "1") == "1"  # parse only the statements needed

_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_SELF = ("self", "cls")


class _File:
    """One parsed source file. Only statements on lines that mention a looked-up
    name are ever walked, so a lookup costs little beyond the parse."""

    _BLOCKS = ("body", "orelse", "finalbody")

    def __init__(self, path: str, text: str):
        self.path = path
        self.lines = text.split("\n")
        self.tree = ast.parse(text)
        self.parents: dict[ast.AST, ast.AST] = {}
        self.walked: set[int] = set()
        self.found: dict[str, list[ast.AST]] = {}
        self.star_import = any(
            isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names)
            for node in self.tree.body
        )

    def mentions(self, name: str, pattern) -> list[ast.AST]:
        if name not in self.found:
            wanted = {
                number
                for number, line in enumerate(self.lines, 1)
                if name in line and pattern.search(line)
            }
            out: list[ast.AST] = []
            self._descend(self.tree, wanted, name, out)
            self.found[name] = out
        return self.found[name]

    def _descend(self, owner, wanted, name, out):
        blocks = [getattr(owner, field, None) for field in self._BLOCKS]
        for handler in getattr(owner, "handlers", ()) or ():
            self.parents[handler] = owner
            if handler.type is not None:
                self._walk(handler.type, handler, name, out)
            blocks.append(handler.body)
        for case in getattr(owner, "cases", ()) or ():
            self.parents[case] = owner
            self._walk(case.pattern, case, name, out)
            if case.guard is not None:
                self._walk(case.guard, case, name, out)
            blocks.append(case.body)
        for block in blocks:
            if not isinstance(block, list):
                continue
            for statement in block:
                if not isinstance(statement, ast.stmt):
                    continue
                first = statement.lineno
                for decorator in getattr(statement, "decorator_list", ()):
                    first = min(first, decorator.lineno)
                if not any(first <= number <= statement.end_lineno for number in wanted):
                    continue
                if isinstance(owner, (ast.ExceptHandler, ast.match_case)) or statement not in self.parents:
                    self.parents.setdefault(statement, owner)
                nested = any(
                    isinstance(getattr(statement, field, None), list) and getattr(statement, field)
                    for field in self._BLOCKS
                ) or getattr(statement, "handlers", None) or getattr(statement, "cases", None)
                if nested and not isinstance(statement, ast.Lambda):
                    for field, value in ast.iter_fields(statement):
                        if field in self._BLOCKS or field in ("handlers", "cases"):
                            continue
                        for item in value if isinstance(value, list) else [value]:
                            if isinstance(item, ast.AST):
                                self._walk(item, statement, name, out)
                    self._descend(statement, wanted, name, out)
                else:
                    self._walk(statement, self.parents[statement], name, out, is_statement=True)

    def _walk(self, node, parent, name, out, is_statement=False):
        if not is_statement:
            self.parents[node] = parent
        stack = [node]
        parents = self.parents
        while stack:
            current = stack.pop()
            if isinstance(current, ast.Name):
                if current.id == name:
                    out.append(current)
            elif isinstance(current, ast.Attribute):
                if current.attr == name:
                    out.append(current)
            elif isinstance(current, ast.Constant):
                if current.value == name and isinstance(current.value, str):
                    out.append(current)
            elif isinstance(current, ast.alias):
                if current.name == name:
                    out.append(current)
            for child in ast.iter_child_nodes(current):
                parents[child] = current
                stack.append(child)

    def place(self, node):
        """(innermost function or lambda whose body holds the node, qualname of the
        classes around it, innermost statement)."""
        function = None
        classes: list[str] = []
        statement = None
        child = node
        parent = self.parents.get(child)
        while parent is not None:
            if statement is None and isinstance(child, ast.stmt):
                statement = child
            if isinstance(parent, _FUNCS):
                if function is None and any(child is s for s in parent.body):
                    function = parent
            elif isinstance(parent, ast.Lambda):
                if function is None and child is parent.body:
                    function = parent
            elif isinstance(parent, ast.ClassDef):
                if any(child is s for s in parent.body):
                    classes.append(parent.name)
            child, parent = parent, self.parents.get(parent)
        if statement is None and isinstance(child, ast.stmt):
            statement = child
        return function, ".".join(reversed(classes)), statement

    def has_param(self, node, name: str) -> bool:
        parent = self.parents.get(node)
        while parent is not None:
            if isinstance(parent, (*_FUNCS, ast.Lambda)):
                args = parent.args
                if any(a.arg == name for a in args.posonlyargs + args.args + args.kwonlyargs):
                    return True
            parent = self.parents.get(parent)
        return False


class _Sliced(_File):
    """The same lookups without parsing the whole file.

    ``outline`` (maps/outline.json, from the pinned commit) says which lines each
    module- and class-level statement spans. Only the statements on lines that
    mention the name are cut out and parsed, then given their real line numbers.
    """

    def __init__(self, path: str, text: str, outline: dict):
        self.path = path
        self.lines = text.split("\n")
        self.units = outline["units"]
        self.starts = [unit[0] for unit in self.units]
        self.star_import = outline["star"]
        self.parents: dict[ast.AST, ast.AST] = {}
        self.found: dict[str, list[ast.AST]] = {}
        self.by_name: dict[tuple[int, int], dict[str, list[ast.AST]]] = {}
        self.chain: dict[ast.AST, str] = {}  # top statement of a unit -> classes around it

    def mentions(self, name: str, pattern) -> list[ast.AST]:
        if name not in self.found:
            spans: list[tuple[int, int]] = []
            for number, line in enumerate(self.lines, 1):
                if name in line and pattern.search(line):
                    index = bisect.bisect_right(self.starts, number) - 1
                    if index < 0:
                        continue
                    # statements can share a start line (``a = 1; b = 2``)
                    first = self.units[index][0]
                    while index >= 0 and self.units[index][0] == first:
                        unit = self.units[index]
                        if unit[0] <= number <= unit[1]:
                            span = (unit[0], unit[1])
                            if span not in spans:
                                spans.append(span)
                                self._parse(unit)
                        index -= 1
            out: list[ast.AST] = []
            for span in sorted(spans):
                out.extend(self.by_name[span].get(name, ()))
            self.found[name] = out
        return self.found[name]

    def _parse(self, unit):
        start, end, indent, kind, chain = unit[:5]
        span = (start, end)
        if span in self.by_name:
            return
        text = "\n".join(self.lines[start - 1:end]) + "\n"
        if kind == "head":
            text += " " * (indent + 4) + "pass\n"
            chain = chain.split("|")[0]
        offset = start - 1
        if indent:
            text = "if 1:\n" + text
            offset -= 1
        tree = ast.parse(text)
        roots = tree.body[0].body if indent else tree.body
        index: dict[str, list[ast.AST]] = {}
        parents = self.parents
        for root in roots:
            ast.increment_lineno(root, offset)
            self.chain[root] = chain
            stack = [root]
            while stack:
                current = stack.pop()
                if isinstance(current, ast.Name):
                    index.setdefault(current.id, []).append(current)
                elif isinstance(current, ast.Attribute):
                    index.setdefault(current.attr, []).append(current)
                elif isinstance(current, ast.Constant):
                    if isinstance(current.value, str):
                        index.setdefault(current.value, []).append(current)
                elif isinstance(current, ast.alias):
                    index.setdefault(current.name, []).append(current)
                for child in ast.iter_child_nodes(current):
                    parents[child] = current
                    stack.append(child)
        self.by_name[span] = index

    def place(self, node):
        function, classes, statement = super().place(node)
        top = node
        while top in self.parents:
            top = self.parents[top]
        outer = self.chain.get(top, "")
        if outer and classes:
            classes = f"{outer}.{classes}"
        elif outer:
            classes = outer
        return function, classes, statement


def _targets(statement) -> tuple[list[str], list[str]]:
    """(plain names, attribute names) a statement binds."""
    names: list[str] = []
    attrs: list[str] = []
    targets = []
    if isinstance(statement, ast.Assign):
        targets = statement.targets
    elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
        targets = [statement.target]
    elif isinstance(statement, (*_FUNCS, ast.ClassDef)):
        names.append(statement.name)
    for target in targets:
        for node in ast.walk(target):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.append(node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                attrs.append(node.attr)
    return names, attrs


def _span(statement) -> tuple[int, int]:
    """The lines that run when the statement itself (not its nested block) runs."""
    if isinstance(statement, (*_FUNCS, ast.ClassDef)):
        first = min([statement.lineno] + [d.lineno for d in statement.decorator_list])
        return first, max(statement.lineno, statement.body[0].lineno - 1)
    body = getattr(statement, "body", None)
    if isinstance(body, list) and body:
        return statement.lineno, max(statement.lineno, body[0].lineno - 1)
    return statement.lineno, statement.end_lineno


def _tail(node) -> str | None:
    """The last identifier of an attribute's receiver: ``MySQLParser`` in
    ``mysql.MySQLParser.SHOW_PARSERS``."""
    value = node.value
    if isinstance(value, ast.Name):
        return value.id
    if isinstance(value, ast.Attribute):
        return value.attr
    return None


def class_id(path: str, qualname: str) -> str:
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    return f"{module}:{qualname}"


class Finder:
    """``maps`` gives line coverage, ``tree`` the class relations and class users."""

    def __init__(self, checkout: str, maps, tree):
        self.checkout = checkout
        self.maps = maps
        self.tree = tree
        self.texts: dict[str, str] = {}
        self.files: dict[str, _File | None] = {}
        self.simple: dict[str, set[str]] = {}
        for cls in tree.mro:
            self.simple.setdefault(cls.split(":")[1].split(".")[-1], set()).add(cls)

    def _text(self, path: str) -> str:
        if path not in self.texts:
            try:
                with open(os.path.join(self.checkout, path), encoding="utf-8", newline="") as handle:
                    self.texts[path] = handle.read()
            except (OSError, UnicodeDecodeError):
                self.texts[path] = ""
        return self.texts[path]

    def _file(self, path: str) -> _File | None:
        if path not in self.files:
            outline = self.maps.outline(path) if SLICED else None
            try:
                if outline is not None:
                    self.files[path] = _Sliced(path, self._text(path), outline)
                else:
                    self.files[path] = _File(path, self._text(path))
            except (SyntaxError, ValueError, RecursionError):
                self.files[path] = None
        return self.files[path]

    def _users(self, scope: str | None) -> set[int] | None:
        """Tests that can reach a value kept on objects of class ``scope``.
        None is "any test": no class, or one whose users the traces cannot bound."""
        if scope is None:
            return None
        module, qualname = scope.split(":")
        return self.tree.users(module.replace(".", "/") + ".py", qualname)

    def find(self, path: str, cls: str | None, names) -> set[int] | None:
        """Tests that execute a read of what ``names`` hold, for a statement in class
        ``cls`` (or at module level) of ``path``. None when that is unknowable."""
        if not names:
            return None
        owner = class_id(path, cls) if cls else None
        self.owner = owner
        self.related = None
        if owner is not None:
            if owner not in self.tree.mro:
                return None
            self.related = self.tree.descendants(owner)
        self.memo: dict[tuple, set[int] | None] = {}
        self.parsed_bytes = 0
        self.mentions = 0
        result: set[int] = set()
        for name in names:
            # An element is one place the value lives:
            #   ("attr", class id or None, name, scope)   an attribute called ``name``
            #   ("global", file, name, scope)             a module-level name of ``file``
            #   ("param", None, name, scope)              a parameter called ``name``
            # ``scope`` is the class whose users bound the tests that can reach it.
            element = ("attr", owner, name, owner) if owner else ("global", path, name, None)
            reached = self._reach(element, 0)
            if reached is None:
                return None
            result |= reached
        if not self.mentions:
            return None  # nothing reads it by name: it is reached some other way
        return result

    def _reach(self, element, depth: int) -> set[int] | None:
        """Tests that execute a use of the value held in ``element``. None = any test."""
        if element in self.memo:
            return self.memo[element]  # None while in progress: a cycle bounds nothing
        self.memo[element] = None
        kind, key, name, scope = element
        if depth > MAX_DEPTH or len(self.memo) > MAX_ELEMENTS:
            return None
        if (name.startswith("__") and name != "__doc__") or len(name) < 3:
            return None
        pattern = re.compile(r"\b%s\b" % re.escape(name))
        total: set[int] = set()
        for file_path in self.maps.index["files"]:
            text = self._text(file_path)
            if name not in text or not pattern.search(text):
                continue
            if file_path not in self.files:
                self.parsed_bytes += len(text)
                if self.parsed_bytes > MAX_PARSE_BYTES:
                    return None
            parsed = self._file(file_path)
            if parsed is None:
                return None
            for node in parsed.mentions(name, pattern):
                outcome = self._mention(parsed, node, element)
                if outcome is None:
                    continue
                cover, derived = outcome
                if depth == 0:
                    self.mentions += 1
                if derived and cover:
                    # Handed on: to be affected a test must run this line and then
                    # run a use of wherever the value went.
                    onward: set[int] | None = set()
                    for item in derived:
                        reached = self._reach(item, depth + 1)
                        if reached is None:
                            onward = None
                            break
                        onward |= reached
                    if onward is not None:
                        cover = cover & onward
                total |= cover
        users = self._users(scope)
        if users is not None:
            total &= users
        self.memo[element] = total
        return total

    def _mention(self, parsed: _File, node, element):
        """None when the node is not a read of the element. Otherwise (tests that
        execute it, the elements it hands the value on to); with nothing handed on,
        executing it is a use."""
        kind, key, name, scope = element
        path = parsed.path
        function, class_name, statement = parsed.place(node)
        parent = parsed.parents.get(node)
        here = class_id(path, class_name) if class_name else None
        related = self.related if (kind, key) == ("attr", self.owner) else None

        if isinstance(node, ast.alias):
            if kind != "global" or not isinstance(parent, ast.ImportFrom) or path == key:
                return None
            module = (parent.module or "").replace(".", "/")
            source = key[:-3]
            if source.endswith("/__init__"):
                source = source[: -len("/__init__")]
            if parent.level == 0 and source != module:
                return None
            # the importing module now has its own global holding the value
            cover = self.maps.lines(path, "immediate", parent.lineno, parent.end_lineno)
            return cover, [("global", path, node.asname or node.name, None)]

        if isinstance(node, ast.Name):
            if not isinstance(node.ctx, ast.Load):
                return None
            if kind == "attr":
                # a class-level name is visible bare only in its own class body
                if key is None or function is not None or here != key:
                    return None
            elif kind == "global":
                if path != key and not parsed.star_import:
                    return None
            elif not parsed.has_param(node, name):
                return None
        elif isinstance(node, ast.Attribute):
            if not isinstance(node.ctx, ast.Load) or kind == "param":
                return None
            tail = _tail(node)
            if kind == "global":
                if tail in _SELF or (tail is not None and tail in self.simple):
                    return None  # an attribute of an object or class, not of the module
            elif related is not None and tail is not None:
                if tail in self.simple and not (self.simple[tail] & related):
                    return None  # another class's attribute of the same name
                if tail in _SELF and function is not None and here in self.tree.mro:
                    if not (self.tree.descendants(here) & related):
                        return None  # ``self`` here is never one of ours
        elif isinstance(node, ast.Constant):
            # a string equal to the name is a use (getattr, __dict__.get, kwargs[...]),
            # except where it only declares a slot
            names, _ = _targets(statement) if statement is not None else ([], [])
            if "__slots__" in names or kind == "global" or name == "__doc__":
                return None
        else:
            return None

        if statement is None:
            return self.maps.whole_file(path), []

        if function is None:
            # Evaluated while a module or class body runs, in every process that
            # imports it: whatever the statement binds now depends on the value.
            start, end = _span(statement)
            cover = self.maps.lines(path, "immediate", start, end)
            names, attrs = _targets(statement)
            if class_name:
                derived = [("attr", here, n, here) for n in names]
            else:
                derived = [("global", path, n, None) for n in names]
            return cover, derived + [("attr", None, a, None) for a in attrs]

        if isinstance(function, ast.Lambda):
            start, end = function.body.lineno, function.body.end_lineno
        else:
            start, end = _span(statement)
        cover = self.maps.lines(path, "deferred", start, end)

        # Inside a function: handed on unchanged, or used.
        if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is node:
            names, attrs = _targets(parent)
            if attrs and not names:
                return cover, [("attr", None, a, scope) for a in attrs]
        elif isinstance(parent, ast.keyword) and parent.value is node and parent.arg:
            return cover, [("param", None, parent.arg, scope), ("attr", None, parent.arg, scope)]
        elif cover and not self.maps.lines(path, "runtime", start, end):
            # Only ever runs while modules are imported (class construction): what it
            # stores is where the value went.
            names, attrs = _targets(statement)
            if attrs and not names and isinstance(statement, (ast.Assign, ast.AnnAssign)):
                return cover, [("attr", None, a, scope) for a in attrs]
        return cover, []
