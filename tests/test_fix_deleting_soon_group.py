"""Titles whose files are scheduled for deletion show only in "Deleting soon", with Keep."""

from __future__ import annotations

import json
from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi


class FakeAniList:
    def __init__(self, calls: list) -> None:
        self.calls = calls

    def set_list_status(self, media_id: int, status: str) -> dict:
        self.calls.append((media_id, status))
        return {"status": status}

    def close(self) -> None:
        pass


def _api(tmp_path: Path) -> tuple[WebAppApi, list]:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    write_config(config, config.config_path)
    api = WebAppApi(config.config_path)
    calls: list = []
    api._anilist_client = lambda: FakeAniList(calls)  # type: ignore[method-assign]
    return api, calls


def _anime(
    api: WebAppApi, tmp_path: Path, media_id: int, title: str, status: str, state: str, **extra
) -> Path:
    api.manager.db.upsert_anime(LibraryAnime(media_id=media_id, title=title, status=status, **extra))
    video = tmp_path / f"{title}.mkv"
    video.write_bytes(b"v")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=media_id,
            title=title,
            episode=extra.get("progress", 0) + 1,
            video_path=video,
            state=state,
        )
    )
    return video


def _groups(api: WebAppApi) -> dict[str, list[str]]:
    home = json.loads(json.dumps(api.get_state()["home"], default=str))
    out: dict[str, list[str]] = {}
    for key, cards in home.items():
        if not isinstance(cards, list):
            continue
        for card in cards:
            items = card.get("items") if card.get("kind") == "watch_sequence" else [card]
            for item in items or []:
                out.setdefault(str(item.get("title")), []).append(key)
    return out


def test_planning_movie_deleted_from_the_menu_leaves_waiting_for_deleting_soon(tmp_path: Path) -> None:
    # The user's case: a Planning movie with one local file, still preparing subtitles.
    api, _ = _api(tmp_path)
    _anime(
        api,
        tmp_path,
        100723,
        "Two Heroes",
        "PLANNING",
        "local",
        format="MOVIE",
        episodes=1,
        media_status="FINISHED",
    )
    assert _groups(api)["Two Heroes"] == ["waiting"]
    api.drop_anime(100723, "PLANNING")
    assert _groups(api)["Two Heroes"] == ["dropped"]
    card = next(item for item in api.get_state()["home"]["dropped"] if item["media_id"] == 100723)
    assert card["delete_at"] and card["delete_remaining_seconds"] is not None


def test_keep_restores_the_group_and_lifts_the_redownload_block(tmp_path: Path) -> None:
    api, calls = _api(tmp_path)
    _anime(
        api,
        tmp_path,
        100723,
        "Two Heroes",
        "PLANNING",
        "local",
        format="MOVIE",
        episodes=1,
        media_status="FINISHED",
    )
    api.drop_anime(100723, "PLANNING")
    assert api.manager.db.redownload_blocked(100723) == {1}
    result = api.keep_dropped_anime(100723)
    assert result["kept"] == 1
    assert _groups(api)["Two Heroes"] == ["waiting"], "back in its normal group"
    assert api.manager.db.episodes(100723)[0].state == "local", "the episode state from before Delete"
    assert api.manager.db.redownload_blocked(100723) == set()
    assert calls == [] and api.manager.db.get_anime(100723).status == "PLANNING"


def test_keep_after_drop_restores_the_previous_status(tmp_path: Path) -> None:
    api, calls = _api(tmp_path)
    _anime(api, tmp_path, 5, "Show", "CURRENT", "ready", progress=2, episodes=12, media_status="FINISHED")
    api.drop_anime(5)
    assert calls == [(5, "DROPPED")] and _groups(api)["Show"] == ["dropped"]
    result = api.keep_dropped_anime(5)
    assert result["status"] == "CURRENT"
    assert calls[-1] == (5, "CURRENT") and api.manager.db.get_anime(5).status == "CURRENT"
    assert api.manager.db.episodes(5)[0].state == "ready"
    assert "dropped" not in _groups(api)["Show"]


def test_deleting_twice_keeps_the_original_restore_point(tmp_path: Path) -> None:
    api, calls = _api(tmp_path)
    _anime(api, tmp_path, 5, "Show", "CURRENT", "ready", progress=2, episodes=12, media_status="FINISHED")
    api.drop_anime(5)
    api.drop_anime(5, "DROPPED")
    api.keep_dropped_anime(5)
    assert api.manager.db.get_anime(5).status == "CURRENT"
    assert api.manager.db.episodes(5)[0].state == "ready"


def test_title_leaves_the_group_once_the_files_are_deleted(tmp_path: Path) -> None:
    api, _ = _api(tmp_path)
    video = _anime(
        api, tmp_path, 5, "Show", "CURRENT", "ready", progress=2, episodes=12, media_status="FINISHED"
    )
    api.drop_anime(5, "CURRENT")
    assert _groups(api)["Show"] == ["dropped"]
    video.unlink()
    api.manager.db.delete_episode_record(video)
    assert "Show" not in _groups(api) or "dropped" not in _groups(api)["Show"]


# --- window ------------------------------------------------------------------

HOME = r"""
setPage('current', true); renderCurrent(); await __tick(50);
const root = $('currentContent');
const titles = key => [...root.querySelectorAll(`[data-home-section="${key}"] article`)].map(node => node.dataset.title);
const out = {
  deleting: titles('section.deletingSoon'), dropped: titles('section.dropped'), waiting: titles('section.waitingPreparation'),
  heading: __text('[data-home-section="section.deletingSoon"] > h2'), expected: t('section.deletingSoon'),
  when: __text('[data-home-section="section.deletingSoon"] .airing-state'),
  expectedWhen: t('label.dropDeletes', {time: formatRemaining(7200)}),
  keep: __text('[data-home-section="section.deletingSoon"] [data-action="keep-dropped"]'), keepLabel: PudgeI18n.t('ui.keep'),
};
await __click('[data-home-section="section.deletingSoon"] [data-action="keep-dropped"]'); await __tick(50);
out.after = titles('section.deletingSoon'); out.afterWaiting = titles('section.waitingPreparation');
return out;
"""


def test_home_shows_deleting_soon_with_keep(page) -> None:
    pending = {
        "media_id": 9,
        "title": "Two Heroes",
        "delete_at": 1790900000,
        "delete_remaining_seconds": 7200,
    }
    kept = {"media_id": 8, "title": "Old Drop", "delete_at": None}
    restored = {"media_id": 9, "title": "Two Heroes", "next_episode": 1, "local": None}
    for lang in ("en", "ru"):
        result = page.run(
            HOME,
            lang=lang,
            state={"home": {"dropped": [pending, kept]}},
            api={
                "keep_dropped_anime": {
                    "ok": True,
                    "kept": 1,
                    "state": {"home": {"dropped": [kept], "waiting": [restored]}},
                }
            },
        )
        out = result.value
        assert out["deleting"] == ["Two Heroes"] and out["dropped"] == ["Old Drop"]
        assert out["heading"] == out["expected"]
        assert out["when"] == out["expectedWhen"] and out["keep"] == out["keepLabel"]
        assert result.calls_of("keep_dropped_anime") == [[9]]
        assert out["after"] == [] and out["afterWaiting"] == ["Two Heroes"]
