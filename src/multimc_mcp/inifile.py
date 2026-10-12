"""MultiMC's INI dialect (launcher/settings/INIFile.cpp).

No sections, one `key=value` per line, `#` starts a comment unless escaped,
and values escape `\\n`, `\\t`, `\\\\` and `\\#`. MultiMC writes keys sorted.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def escape(value: str) -> str:
    out = []
    for c in value:
        if c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif c == "\\":
            out.append("\\\\")
        elif c == "#":
            out.append("\\#")
        else:
            out.append(c)
    return "".join(out)


def unescape(value: str) -> str:
    out = []
    prev_backslash = False
    for c in value:
        if prev_backslash:
            out.append({"n": "\n", "t": "\t"}.get(c, c))
            prev_backslash = False
        elif c == "\\":
            prev_backslash = True
        else:
            out.append(c)
    return "".join(out)


def _strip_comment(line: str) -> str:
    i = 0
    while (i := line.find("#", i)) != -1:
        if i > 0 and line[i - 1] == "\\":
            i += 1
            continue
        return line[:i]
    return line


def parse(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw in text.split("\n"):
        line = _strip_comment(raw)
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key.strip()] = unescape(value.strip())
    return data


def read(path: Path) -> dict[str, str]:
    try:
        return parse(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def dumps(data: dict[str, str]) -> str:
    return "".join(f"{k}={escape(str(data[k]))}\n" for k in sorted(data))


def write(path: Path, data: dict[str, str]) -> None:
    atomic_write_text(path, dumps(data))


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        # mkstemp makes 0600 files; keep the existing file's mode, else the usual umask default.
        try:
            mode = path.stat().st_mode & 0o777
        except FileNotFoundError:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        if os.name != "nt":  # Windows has no POSIX modes (and no os.fchmod before 3.13)
            os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
