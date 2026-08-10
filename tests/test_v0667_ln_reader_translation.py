from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService


def cfg(tmp_path: Path) -> AppConfig:
    c = AppConfig()
    c.library.root_dir = tmp_path / "library"
    c.library.database_path = tmp_path / "db.sqlite3"
    c.paths.cache_dir = tmp_path / "cache"
    c.library.root_dir.mkdir(parents=True)
    c.paths.cache_dir.mkdir(parents=True)
    return c


def test_japanese_bare_fullwidth_suffix_is_volume_and_reuses_series_anilist(tmp_path: Path) -> None:
    service = LightNovelService(cfg(tmp_path))
    first_path = tmp_path / "あそびのかんけい.txt"
    first_path.write_text("本文", encoding="utf-8")
    first = service.import_file(first_path)
    with service._connect() as conn:
        conn.execute(
            "UPDATE ln_books SET anilist_id=188236,anilist_status='CURRENT',anilist_total_volumes=4 WHERE id=?",
            (first["id"],),
        )

    second_path = tmp_path / "あそびのかんけい２.txt"
    second_path.write_text("第二巻", encoding="utf-8")
    second = service.import_file(second_path)

    assert second["volume"] == 2
    assert second["anilist_id"] == 188236
    assert LightNovelService._anilist_search_text("&#x20;あそびのかんけい２") == "あそびのかんけい"


def test_binding_one_volume_propagates_to_existing_siblings(tmp_path: Path) -> None:
    service = LightNovelService(cfg(tmp_path))
    a = tmp_path / "あそびのかんけい.txt"
    b = tmp_path / "あそびのかんけい２.txt"
    a.write_text("一", encoding="utf-8")
    b.write_text("二", encoding="utf-8")
    first = service.import_file(a)
    second = service.import_file(b)
    selection = {
        "media_id": 188236,
        "status": "CURRENT",
        "progress_volumes": 1,
        "volumes": 4,
        "cover": "https://example/cover.jpg",
    }
    service.bind_anilist(first["id"], 188236, selection)
    linked = service.book(second["id"])
    assert linked["anilist_id"] == 188236
    assert linked["anilist_total_volumes"] == 4


def test_online_selection_translation_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = cfg(tmp_path)
    c.ui.language = "ru"
    service = LightNovelService(c)
    # Legacy overrides must no longer win over General -> Language.
    service.save_settings({"translation_language": "en"})
    calls: list[tuple[str, str]] = []

    def fake_get(url: str, **kwargs):
        calls.append((url, kwargs["params"]["tl"]))
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json=[[['Это тест.', 'これはテストです。', None, None]]],
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    first = service.translate_selection("これはテストです。", "前の文脈")
    second = service.translate_selection("これはテストです。", "前の文脈")
    assert first["translation"] == "Это тест."
    assert second["translation"] == "Это тест."
    assert second["cached"] is True
    assert calls == [("https://translate.googleapis.com/translate_a/single", "ru")]


def test_local_llm_is_translation_fallback_with_200_chars_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    c = cfg(tmp_path)
    c.llm.enabled = True
    c.llm.base_url = "http://127.0.0.1:11434"
    c.llm.model = "local-model"
    c.ui.language = "en"
    service = LightNovelService(c)
    service.save_settings({"translation_language": "ru"})
    seen: dict[str, object] = {}

    def fail_get(*_args, **_kwargs):
        raise httpx.ConnectError("offline")

    def fake_post(url: str, **kwargs):
        seen["url"] = url
        seen["payload"] = kwargs["json"]
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"message": {"content": json.dumps({"translation": "context-aware result"})}},
        )

    monkeypatch.setattr(httpx, "get", fail_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    context = "前" * 260
    result = service.translate_selection("彼はそう言った。", context)
    assert result["translation"] == "context-aware result"
    assert seen["url"] == "http://127.0.0.1:11434/api/chat"
    user = seen["payload"]["messages"][1]["content"]
    # The fallback gets at most the requested preceding 200 characters.
    before = user.split("SELECTED TEXT:", 1)[0]
    assert before.count("前") == 200


def test_light_novel_state_refresh_is_background_and_deduplicated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = LightNovelService(cfg(tmp_path))
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def slow_scan() -> int:
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(2)
        return 0

    monkeypatch.setattr(service, "scan_downloaded", slow_scan)
    started = time.monotonic()
    state = service.state()
    assert time.monotonic() - started < 0.25
    assert state["refreshing"] is True
    assert entered.wait(1)
    # Multiple UI refresh polls do not start more workers.
    service.state()
    service.refresh_state()
    assert calls == 1
    release.set()
    deadline = time.monotonic() + 2
    while service._state_refreshing and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service._state_refreshing is False


def test_reader_ui_owns_appearance_and_selection_translation() -> None:
    html = Path("pudge/web/index.html").read_text(encoding="utf-8")
    settings_block = html[html.index("function renderSettings(){"):html.index("function fillSettings")]
    assert 'id="lnReaderAppearance"' in html
    assert 'id="lnrFont"' in html and 'id="lnrFurigana"' in html and 'id="lnrCustomCss"' in html
    assert 'id="s_ln_reader_font"' not in settings_block
    assert 'id="s_ln_furigana"' not in settings_block
    assert 'id="s_ln_custom_css"' not in settings_block
    assert 'id="s_ln_translation_language"' not in settings_block
    assert "light_novel_translate" in html
    assert ".slice(-200)" in html
    assert "light_novel_cancel_reader_background" in html
    assert "App navigation uses standard Cmd+1…N and Cmd+F" not in html
    assert "Навигация приложения использует стандартные Cmd+1…N и Cmd+F" not in html


def test_cli_reuses_library_anilist_identity_for_absolute_bleach(tmp_path: Path) -> None:
    from pudge.cli import _resolve_tracking_anilist
    from pudge.database import Database
    from pudge.filename import parse_anime_filename
    from pudge.manager_models import LibraryAnime, LibraryEpisode

    c = cfg(tmp_path)
    c.anilist.enabled = True
    c.anilist.access_token = "token"
    video = c.library.root_dir / "BLEACH_ Sennen Kessen-hen - Kashin-tan" / "Bleach.2004.S17E43.REPACK.mkv"
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"video")
    db = Database(c.library.database_path)
    db.upsert_anime(
        LibraryAnime(
            media_id=185874,
            title="BLEACH: Sennen Kessen-hen - Kashin-tan",
            titles=["BLEACH: Thousand-Year Blood War - The Calamity"],
            episodes=13,
            format="TV",
            season_year=2026,
        )
    )
    db.upsert_episode(
        LibraryEpisode(
            media_id=185874,
            title="BLEACH: Sennen Kessen-hen - Kashin-tan",
            episode=43,
            video_path=video.resolve(),
        )
    )
    anime, _key = _resolve_tracking_anilist(
        video,
        parse_anime_filename(video.name),
        c,
        None,
        False,
    )
    assert anime is not None
    assert anime.id == 185874
    assert anime.episodes == 13


def test_cli_jimaku_alias_uses_manager_release_numbering_cache(tmp_path: Path) -> None:
    import logging
    from pudge.cli import _jimaku_episode_aliases
    from pudge.models import AniListAnime

    c = cfg(tmp_path)
    anime = AniListAnime(
        id=185874,
        titles=["BLEACH: Sennen Kessen-hen - Kashin-tan"],
        synonyms=[],
        season_year=2026,
        episodes=13,
        format="TV",
    )
    cache = c.paths.cache_dir / "anilist-release-numbering" / "185874.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"offset": 40, "prequel_titles": []}), encoding="utf-8")
    assert _jimaku_episode_aliases(anime, 43, c, logging.getLogger("test")) == (3,)
