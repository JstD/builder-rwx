"""Parse a git-style unified diff and apply it to the pinned sources, strictly."""

from __future__ import annotations

import re

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


class FilePatch:
    def __init__(self):
        self.old_path: str | None = None
        self.new_path: str | None = None
        self.hunks: list[tuple[int, list[str]]] = []  # (old start line, raw hunk lines)
        self.binary = False
        self.mode_change = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path  # type: ignore[return-value]


def _strip(path: str) -> str | None:
    path = path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        path = path.encode("latin-1", "replace").decode("utf-8", "replace")
    if "\t" in path:
        path = path.split("\t", 1)[0]
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse(text: str) -> list[FilePatch]:
    files: list[FilePatch] = []
    current: FilePatch | None = None
    lines = text.split("\n")
    i = 0
    count = len(lines)
    while i < count:
        line = lines[i]
        if line.startswith("diff --git "):
            current = FilePatch()
            files.append(current)
            match = re.match(r'^diff --git "?a/(.*?)"? "?b/(.*?)"?$', line)
            if match:
                current.old_path, current.new_path = match.group(1), match.group(2)
            i += 1
            continue
        if current is None:
            if line.startswith("--- ") and i + 1 < count and lines[i + 1].startswith("+++ "):
                current = FilePatch()
                files.append(current)
            else:
                i += 1
                continue
        if line.startswith("--- "):
            current.old_path = _strip(line[4:])
        elif line.startswith("+++ "):
            current.new_path = _strip(line[4:])
        elif line.startswith("new file mode"):
            current.old_path = None
        elif line.startswith("deleted file mode"):
            current.mode_change = True
        elif line.startswith(("old mode", "new mode")):
            current.mode_change = True
        elif line.startswith("rename from "):
            current.old_path = line[len("rename from "):].strip()
        elif line.startswith("rename to "):
            current.new_path = line[len("rename to "):].strip()
        elif line.startswith(("Binary files ", "GIT binary patch")):
            current.binary = True
        elif line.startswith("@@"):
            match = HUNK.match(line)
            if not match:
                raise PatchError(f"bad hunk header: {line!r}")
            old_len = int(match.group(2)) if match.group(2) is not None else 1
            new_len = int(match.group(4)) if match.group(4) is not None else 1
            body: list[str] = []
            i += 1
            while i < count and (old_len > 0 or new_len > 0):
                hunk_line = lines[i]
                tag = hunk_line[:1]
                if tag == "\\":
                    body.append(hunk_line)
                elif tag == "-":
                    old_len -= 1
                    body.append(hunk_line)
                elif tag == "+":
                    new_len -= 1
                    body.append(hunk_line)
                elif tag == " " or hunk_line == "":
                    old_len -= 1
                    new_len -= 1
                    body.append(" " + hunk_line[1:])
                else:
                    raise PatchError(f"bad hunk line: {hunk_line!r}")
                i += 1
            if i < count and lines[i].startswith("\\"):
                body.append(lines[i])
                i += 1
            current.hunks.append((int(match.group(1)), body))
            continue
        i += 1
    return files


def apply(old: str | None, patch: FilePatch) -> tuple[str | None, list[tuple[int, int, int]]]:
    """Returns the new text and the changed blocks as (old start, old end, new line count).

    Lines are 1-based; ``old end`` is exclusive, so an insertion has start == end.
    A deleted file gives ``None`` for the new text.
    """
    if patch.binary:
        raise PatchError("binary patch")
    old_lines = old.split("\n") if old is not None else []
    old_no_eol = old is not None and not old.endswith("\n")
    if old is not None and old.endswith("\n"):
        old_lines.pop()
    out: list[str] = []
    blocks: list[tuple[int, int, int]] = []
    pos = 0  # index into old_lines
    new_no_eol = old_no_eol
    for start, body in patch.hunks:
        index = start - 1 if (start > 0) else 0
        if old is None:
            index = 0
        if index < pos:
            raise PatchError("overlapping hunks")
        out.extend(old_lines[pos:index])
        pos = index
        block_start = None
        added = 0
        previous = " "
        old_marker = new_marker = False
        for line in body:
            tag, content = line[:1], line[1:]
            if tag == "\\":
                # "\ No newline at end of file" describes the line just before it.
                if previous in "- ":
                    old_marker = True
                if previous in "+ ":
                    new_marker = True
                continue
            if tag == " ":
                if block_start is not None:
                    blocks.append((block_start + 1, pos + 1, added))
                    block_start, added = None, 0
                if pos >= len(old_lines) or old_lines[pos] != content:
                    raise PatchError(f"context mismatch at line {pos + 1}")
                out.append(content)
                pos += 1
            elif tag == "-":
                if block_start is None:
                    block_start, added = pos, 0
                if pos >= len(old_lines) or old_lines[pos] != content:
                    raise PatchError(f"removed line mismatch at line {pos + 1}")
                pos += 1
            elif tag == "+":
                if block_start is None:
                    block_start, added = pos, 0
                out.append(content)
                added += 1
            previous = tag
        if block_start is not None:
            blocks.append((block_start + 1, pos + 1, added))
        if old_marker or new_marker:
            new_no_eol = new_marker
    out.extend(old_lines[pos:])
    if patch.new_path is None:
        return None, blocks
    text = "\n".join(out)
    if out and not new_no_eol:
        text += "\n"
    return text, blocks
