"""One volume out of a .rar/.7z light novel batch: only its files, under the book limit (behaviour).

The whole batch used to be unpacked into the system temp folder with no size limit
(``limit=None``, up to 300 s) just to pick out one volume: a large or hostile batch
filled the internal disk even with the library on an external one. Now the batch is
listed, only the volume's own files are unpacked (names with brackets match
themselves), under ``BOOK_ARCHIVE_LIMIT``, into a hidden folder next to the library.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import pudge.providers.jimaku as jimaku
from pudge.archive_limits import BOOK_ARCHIVE_LIMIT
from pudge.config import AppConfig
from pudge.light_novels import LightNovelError, LightNovelService

# A stand-in archive tool: lists MEMBERS, "unpacks" the named ones (bsdtar patterns
# unescaped), logs every run as JSON.
FAKE = """#!{python}
import json, os, re, sys
args = sys.argv[1:]
members = {members!r}
with open(os.environ["FAKE_TOOL_LOG"], "a") as log:
    log.write(json.dumps([os.path.basename(sys.argv[0]), *args]) + "\\n")
if os.path.basename(sys.argv[0]) == "bsdtar":
    if args[0] == "-tf":
        print("\\n".join(members)); sys.exit(0)
    root = args[args.index("-C") + 1]
    names = [re.sub(r"\\\\(.)", r"\\1", name) for name in args[args.index("-C") + 2:]] or members
else:
    if args[0] == "l":
        print("\\n".join("Path = " + name for name in members)); sys.exit(0)
    root = next(arg[2:] for arg in args if arg.startswith("-o"))
    names = args[args.index("--") + 2:] or members
for name in names:
    if name not in members:
        print("no such member: " + name, file=sys.stderr); sys.exit(1)
    target = os.path.join(root, name)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    open(target, "w").write("text of " + name)
"""

MEMBERS = [
    "Batch/",
    "Batch/[Group] Ookami 01.epub",
    "Batch/[Group] Ookami 02.epub",
    "Batch/[Group] Ookami 03.epub",
    "Batch/cover 02.jpg",
]


def _service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return LightNovelService(config)


@pytest.fixture
def tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    log = tmp_path / "tools.log"
    log.touch()
    monkeypatch.setenv("FAKE_TOOL_LOG", str(log))
    monkeypatch.setattr(jimaku, "find_bsdtar", lambda: None)
    monkeypatch.setattr(jimaku, "find_7zip", lambda: None)
    limits: list[int | None] = []
    original = jimaku.run_extraction

    def recorded(command, root, *, limit, timeout, env=None):
        limits.append(limit)
        return original(command, root, limit=limit, timeout=timeout, env=env)

    monkeypatch.setattr(jimaku, "run_extraction", recorded)

    def install(name: str) -> None:
        path = tmp_path / "bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(FAKE.format(python=sys.executable, members=MEMBERS), encoding="utf-8")
        path.chmod(0o755)
        monkeypatch.setattr(jimaku, "find_bsdtar" if name == "bsdtar" else "find_7zip", lambda: str(path))

    def runs() -> list[list[str]]:
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    install.runs = runs  # type: ignore[attr-defined]
    install.limits = limits  # type: ignore[attr-defined]
    return install


def _batch(service: LightNovelService, name: str = "Ookami 01-03.rar") -> dict:
    archive = service.root / name
    archive.write_bytes(b"rar")
    return {"index": 0, "name": archive.name, "priority": 6, "progress": 1.0}


def test_only_the_volumes_own_file_is_unpacked_under_the_book_limit(tmp_path: Path, tool) -> None:
    tool("bsdtar")
    service = _service(tmp_path)
    service._materialize_selected_volume([_batch(service)], 2)

    listing, extract = tool.runs()
    assert listing[:2] == ["bsdtar", "-tf"]
    root = Path(extract[extract.index("-C") + 1])
    assert root.parent == service.root and root.name.startswith(".")
    assert extract[extract.index("-C") + 2 :] == [r"Batch/\[Group\] Ookami 02.epub"]
    assert tool.limits == [BOOK_ARCHIVE_LIMIT]
    assert (service.root / "[Group] Ookami 02.epub").read_text() == "text of Batch/[Group] Ookami 02.epub"
    assert sorted(path.name for path in service.root.iterdir()) == [
        "Ookami 01-03.rar",
        "[Group] Ookami 02.epub",
    ]


def test_7zip_fallback_unpacks_only_the_volume_with_wildcards_off(tmp_path: Path, tool) -> None:
    tool("7zz")
    service = _service(tmp_path)
    service._materialize_selected_volume([_batch(service, "Ookami.7z")], 3)
    listing, extract = tool.runs()
    assert listing[:3] == ["7zz", "l", "-ba"]
    assert "-spd" in extract and extract[-1] == "Batch/[Group] Ookami 03.epub"
    assert (service.root / "[Group] Ookami 03.epub").is_file()


def test_a_volume_missing_from_the_batch_unpacks_nothing(tmp_path: Path, tool) -> None:
    tool("bsdtar")
    service = _service(tmp_path)
    with pytest.raises(LightNovelError):
        service._materialize_selected_volume([_batch(service)], 9)
    assert [run[1] for run in tool.runs()] == ["-tf"]
