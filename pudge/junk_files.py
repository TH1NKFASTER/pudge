"""Files macOS and other systems leave next to real media, which Pudge never imports.

Finder's "Compress" adds ``__MACOSX/._<name>`` for every file with extended
attributes (every downloaded image carries the quarantine attribute), and macOS
writes ``._<name>`` AppleDouble companions on exFAT/FAT/SMB volumes. They have
the real file's suffix but are not images or audio: they used to become broken
manga pages (sorted first, so OCR stopped before any real page) and made a
whole audiobook folder fail to import. Hidden files and folders (``.DS_Store``,
``.thumbnails``, a NAS's ``.@__thumb``) are skipped for the same reason.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def junk_part(name: str) -> bool:
    """One path component that is never media: hidden (``.x``, ``._x``) or ``__MACOSX``."""
    text = str(name or "")
    return text.startswith(".") or text.casefold() == "__macosx"


def junk_member(name: str) -> bool:
    """An archive member (``a/b/c.jpg``) inside ``__MACOSX`` or hidden at any level."""
    parts = PurePosixPath(str(name or "").replace("\\", "/")).parts
    return any(junk_part(part) for part in parts)


def junk_path(path: Path, root: Path | None = None) -> bool:
    """A file found under ``root`` (or the file itself) that is hidden or AppleDouble junk."""
    item = Path(path)
    if root is not None:
        try:
            return any(junk_part(part) for part in item.relative_to(root).parts)
        except ValueError:
            pass
    return junk_part(item.name)


# Folders of software, never of books: Python package metadata (LICENSE, top_level.txt,
# entry_points.txt, SOURCES.txt ... of *.egg-info / *.dist-info), installed packages,
# virtual environments, node modules and repositories. Their .txt files became "books".
_SOFTWARE_DIRS = frozenset(
    {"site-packages", "dist-packages", "node_modules", "__pycache__", ".git", "venv", ".venv", ".tox"}
)
_SOFTWARE_SUFFIXES = (".egg-info", ".dist-info")


def software_path(path: Path | str) -> bool:
    """A file inside a package / environment / repository folder (any level): never a book."""
    for part in Path(path).parts[:-1]:
        name = part.casefold()
        if name in _SOFTWARE_DIRS or name.endswith(_SOFTWARE_SUFFIXES):
            return True
    return False
