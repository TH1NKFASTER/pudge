"""Tests check behaviour, not source text (audit 71Б).

Installed by conftest for every test run: when test code reads a file of the code base
(pudge/, install.sh, the iOS app, CI workflows, docs, packaging), the read fails with an
explanation. Reading through tests/source_policy.py (codebase-wide policies, allow-listed
in tests/test_source_policy_guard.py) and through the test harnesses is allowed; the
app's own code may read its files as it likes.
"""

from __future__ import annotations

import builtins
import io
import os
import pathlib
import sys
import sysconfig

ROOT = pathlib.Path(__file__).resolve().parents[1]
_TESTS = str(ROOT / "tests") + os.sep
_ROOT = str(ROOT) + os.sep

# Parts of the repository that are code or its documentation.
_PROTECTED_DIRS = tuple(str(ROOT / name) + os.sep for name in ("pudge", "ios", ".github", "scripts", "docs"))
_PROTECTED_FILES = frozenset(
    str(ROOT / name)
    for name in (
        "install.sh",
        "build_release.sh",
        "pyproject.toml",
        "uv.lock",
        "Makefile",
        "config.example.toml",
        "rename_brand.py",
        "brand_migration.py",
        "run-ios-xcode-checks.command",
        "README.md",
        "CHANGELOG.md",
        "RELEASING.md",
        "DEVELOPMENT.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "MOBILE_SYNC_PROTOCOL.md",
    )
)
# Data the app ships that tests may read as data, not as source text.
_DATA_SUFFIXES = (
    ".env",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".icns",
    ".ico",
    ".svg",
    ".webp",
    ".ttf",
    ".otf",
    ".wav",
    ".mp3",
    ".json",
    ".bin",
    ".npz",
    ".onnx",
)

# Test-side modules allowed to read sources: the policy gateway and the harnesses that
# load or run the code (pages, stylesheets, catalogs, scripts with stand-ins).
_ALLOWED_READERS = frozenset(
    _TESTS + name
    for name in (
        "source_policy.py",
        "css_support.py",
        "i18n_support.py",
        "page_harness.py",
        "lua_harness.py",
        "t71g00_support.py",
        "t71g01_support.py",
        "conftest.py",
        "source_read_guard.py",
    )
)
_STDLIB = tuple({sysconfig.get_paths()["stdlib"], sysconfig.get_paths()["platstdlib"]})

_original_open = builtins.open
_original_io_open = io.open
_original_read_text = pathlib.Path.read_text
_original_read_bytes = pathlib.Path.read_bytes


class SourceTextRead(AssertionError):
    pass


def _protected(path: object) -> str | None:
    if isinstance(path, int):
        return None
    try:
        text = os.path.abspath(os.fspath(path))  # type: ignore[arg-type]
    except TypeError:
        return None
    if not text.startswith(_ROOT):
        return None
    if text in _PROTECTED_FILES:
        return text
    if text.startswith(_PROTECTED_DIRS) and not text.endswith(_DATA_SUFFIXES) and "__pycache__" not in text:
        return text
    return None


def _test_caller() -> str | None:
    """The test-side file that asked for the read, or None if the app's code (or a harness) did."""
    frame = sys._getframe(2)
    while frame is not None:
        filename = frame.f_code.co_filename
        if filename.startswith(_STDLIB) or filename == __file__ or filename.startswith("<frozen"):
            # Copying code into a sandbox to run it there is not reading its text.
            if filename.endswith("shutil.py") and frame.f_code.co_name in {
                "copyfile",
                "copy",
                "copy2",
                "copytree",
                "_copytree",
                "copyfileobj",
            }:
                return None
            frame = frame.f_back
            continue
        if filename.startswith(_TESTS) and filename not in _ALLOWED_READERS:
            return filename
        return None
    return None


def _check(path: object) -> None:
    target = _protected(path)
    if target is None:
        return
    caller = _test_caller()
    if caller is None:
        return
    raise SourceTextRead(
        f"{os.path.relpath(caller, ROOT)} reads {os.path.relpath(target, ROOT)}: tests check behaviour, not "
        "source text (audit 71Б). Test the window with tests/page_harness.py, Python by calling it, mpv "
        "scripts with tests/lua_harness.py, styles with tests/css_support.py. A codebase-wide policy with "
        "nothing to run may read sources through tests/source_policy.py (allow-listed in "
        "tests/test_source_policy_guard.py)."
    )


def _guarded_open(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    _check(file)
    return _original_open(file, *args, **kwargs)


def _guarded_io_open(file, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    _check(file)
    return _original_io_open(file, *args, **kwargs)


def _guarded_read_text(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
    _check(self)
    return _original_read_text(self, *args, **kwargs)


def _guarded_read_bytes(self):  # noqa: ANN001
    _check(self)
    return _original_read_bytes(self)


def install() -> None:
    if os.environ.get("PUDGE_ALLOW_SOURCE_READS") == "1":
        return
    builtins.open = _guarded_open
    io.open = _guarded_io_open
    pathlib.Path.read_text = _guarded_read_text  # type: ignore[method-assign]
    pathlib.Path.read_bytes = _guarded_read_bytes  # type: ignore[method-assign]
