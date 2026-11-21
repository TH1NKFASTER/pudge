"""rv-web: a Home build's per-episode questions cost a component compare and one policy read per
title, not a Path per ancestor per unfinished download and one SQLite query per episode."""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge.config import AppConfig, write_config
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi


def _reference_within(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.expanduser().resolve()
    return any(resolved == root or root in resolved.parents for root in roots)


def test_path_within_answers_as_before(tmp_path: Path) -> None:
    library = (tmp_path / "library").resolve()
    (library / "Show A").mkdir(parents=True)
    (library / "Show AB").mkdir()
    (library / "Show A" / "ep01.mkv").write_bytes(b"x")
    (tmp_path / "link").symlink_to(library / "Show A")
    roots_sets = [
        (),
        (library / "Show A",),
        (library / "Show A" / "ep01.mkv",),
        (library / "Show AB", Path("relative/Show A")),
        (library,),
        (Path("/"),),
        (library / "Show A" / "missing",),
    ]
    paths = [
        library / "Show A" / "ep01.mkv",
        library / "Show A",
        library / "Show AB" / "ep01.mkv",
        library / "Show A" / "sub" / "deeper.mkv",
        tmp_path / "link" / "ep01.mkv",  # resolves into Show A
        tmp_path / "elsewhere.mkv",
        Path("relative/Show A/ep01.mkv"),
    ]
    for roots in roots_sets:
        for path in paths:
            assert AnimeManager._path_within(path, roots) is _reference_within(path, roots), (path, roots)
    # A list or set of roots (callers pass both) answers the same.
    roots = [library / "Show AB", library / "Show A"]
    assert AnimeManager._path_within(paths[0], roots)  # type: ignore[arg-type]
    assert AnimeManager._path_within(paths[0], set(roots))  # type: ignore[arg-type]


def test_path_within_without_roots_does_not_walk_the_disk(monkeypatch) -> None:
    def no_resolve(self, strict=False):  # noqa: ANN001
        raise AssertionError("no roots: nothing to resolve")

    monkeypatch.setattr(Path, "resolve", no_resolve)
    assert AnimeManager._path_within(Path("/library/Show/ep01.mkv"), ()) is False


def test_path_within_builds_no_ancestor_paths(tmp_path: Path, monkeypatch) -> None:
    roots = tuple((tmp_path / f"dl{index}" / "a" / "b" / "c").resolve() for index in range(40))
    target = (tmp_path / "library" / "x" / "y" / "z" / "ep.mkv").resolve()
    built = 0
    real = type(target).parents.fget

    def counting(self):  # noqa: ANN001
        nonlocal built
        built += 1
        return real(self)

    monkeypatch.setattr(type(target), "parents", property(counting))
    for _ in range(50):
        assert AnimeManager._path_within(target, roots) is False
    assert built == 0  # before: 40 roots x every ancestor, per call


def _api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


@pytest.fixture
def api(tmp_path: Path) -> WebAppApi:
    api = _api(tmp_path)
    db = api.manager.db
    root = api.config.library.root_dir
    for index in range(3):
        media_id = 930100 + index
        db.upsert_anime(
            LibraryAnime(
                media_id=media_id,
                title=f"Probe {index}",
                status="CURRENT" if index else "PLANNING",
                episodes=6,
                progress=1,
                format="TV",
            )
        )
        folder = root / f"Probe {index}"
        folder.mkdir()
        for number in range(1, 7):
            video = folder / f"Probe {index} - {number:02d}.mkv"
            video.write_bytes(b"x")
            db.upsert_episode(
                LibraryEpisode(
                    media_id=media_id,
                    title=f"Probe {index}",
                    episode=number,
                    video_path=video,
                    state="ready" if number % 2 else "pending_subtitles",
                )
            )
        db.upsert_download(
            DownloadItem(
                torrent_hash=f"{index + 1:040x}",
                name=f"Probe {index} - 07.mkv",
                state="downloading",
                progress=0.4,
                save_path=str(folder),
                content_path=str(folder / f"Probe {index} - 07.mkv"),
                media_id=media_id,
                episode=7,
                media_episode=7,
                release_episode=7,
            )
        )
    api.manager.set_japanese_subtitles_required(930101, False)
    return api


def test_home_build_reads_each_titles_subtitle_policy_once(api: WebAppApi, monkeypatch) -> None:
    asked: list[int | None] = []
    real = api.manager.japanese_subtitles_required

    def counting(media_id):  # noqa: ANN001
        asked.append(media_id)
        return real(media_id)

    monkeypatch.setattr(api.manager, "japanese_subtitles_required", counting)
    state = api.get_state()
    assert asked, "the build asks for the policy"
    assert len(asked) == len(set(asked)) <= 3
    cards = [card for card in state["current"] + state["planned"] if card.get("media_id") == 930101]
    assert cards and all(card["japanese_subtitles_required"] is False for card in cards)
    # Outside a build every question reaches the stored policy (a change shows at once).
    asked.clear()
    api.manager.set_japanese_subtitles_required(930101, True)
    assert api._build_japanese_subtitles_required(930101) is True
    assert api._build_japanese_subtitles_required(930101) is True
    assert asked == [930101, 930101]
    assert api._build_japanese_subtitles_required(None) is True


def test_home_build_state_is_unchanged_by_the_memo(api: WebAppApi, monkeypatch) -> None:
    fast = api.get_state()
    api._ui_state_cache.invalidate()
    monkeypatch.setattr(
        WebAppApi,
        "_build_japanese_subtitles_required",
        lambda self, media_id: self.manager.japanese_subtitles_required(media_id),
    )
    slow = api.get_state()
    for key in ("current", "planned", "home", "library", "downloads"):
        assert fast[key] == slow[key], key


def test_home_build_checks_each_file_against_unfinished_downloads_once(api: WebAppApi, monkeypatch) -> None:
    asked: list[tuple[str, int]] = []
    real = api.manager._path_within

    def counting(path, roots):  # noqa: ANN001
        asked.append((str(path), len(tuple(roots))))
        return real(path, roots)

    api.get_state()  # disk usage (its own walk, kept 30 s) is measured before counting
    api._ui_state_cache.invalidate()
    monkeypatch.setattr(api.manager, "_path_within", counting)
    api.get_state()
    unfinished = [entry for entry in asked if entry[1] == 3]  # the three downloads under way
    assert unfinished
    assert len(unfinished) == len(set(unfinished))  # before: once per helper that lists the file
    # Outside a build it is asked every time.
    asked.clear()
    path = api.config.library.root_dir / "Probe 0" / "Probe 0 - 01.mkv"
    api._build_path_within(path, ())
    api._build_path_within(path, ())
    assert len(asked) == 2
