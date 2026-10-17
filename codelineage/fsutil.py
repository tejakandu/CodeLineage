"""File access that survives Windows' 260-character path limit.

Deep folders (e.g. ddl_archive/manual_scripts/<long ticket names>/...) exceed MAX_PATH on Windows.
Paths are given the extended-length prefix (\\\\?\\) there, which works without the registry's
LongPathsEnabled setting or admin rights. Elsewhere these are plain pathlib calls.
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

IS_WINDOWS = os.name == "nt"
_PREFIX, _UNC_PREFIX = "\\\\?\\", "\\\\?\\UNC\\"


def extended(path: str, windows: bool = IS_WINDOWS) -> str:
    """Absolute path string with the Windows extended-length prefix (no-op elsewhere)."""
    if not windows or path.startswith(_PREFIX):
        return path
    if path.startswith("\\\\"):                      # \\server\share\... -> \\?\UNC\server\share\...
        return _UNC_PREFIX + path[2:]
    return _PREFIX + path


def plain(path: str) -> str:
    """Strip the extended-length prefix again, for display and relative paths."""
    if path.startswith(_UNC_PREFIX):
        return "\\\\" + path[len(_UNC_PREFIX):]
    if path.startswith(_PREFIX):
        return path[len(_PREFIX):]
    return path


def read_text(path: Path) -> str:
    return Path(extended(str(Path(path).absolute()))).read_text(encoding="utf-8", errors="replace")


def _matches(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(rel + "/", p) for p in patterns)


def walk_files(base: Path, suffix: str, root: Path, exclude: list[str], errors: list[str]) -> list[Path]:
    """Files under base ending in suffix, sorted. Excluded folders are not entered at all (so an
    excluded deep archive costs nothing), and unreadable folders are reported in errors."""
    base = Path(base).absolute()
    root = Path(root).absolute()
    if base.is_file():
        return [base]
    if not base.exists():
        return []

    def rel(p: str) -> str:
        try:
            return Path(plain(p)).relative_to(root).as_posix()
        except ValueError:
            return Path(plain(p)).as_posix()

    out = []
    for dirpath, dirnames, filenames in os.walk(extended(str(base)), onerror=lambda e: errors.append(
            f"{rel(getattr(e, 'filename', '') or '')}: skipped folder ({e.strerror or e})")):
        dirnames[:] = sorted(d for d in dirnames if not _matches(rel(os.path.join(dirpath, d)), exclude))
        for name in filenames:
            full = os.path.join(dirpath, name)
            if name.lower().endswith(suffix) and not _matches(rel(full), exclude):
                out.append(Path(plain(full)))
    return sorted(out)
