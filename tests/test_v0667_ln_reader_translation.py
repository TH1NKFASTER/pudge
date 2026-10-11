from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import httpx
import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from study_page_support import SELECTION
from t71g18_support import ln_book
from tests.i18n_support import has_text


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
            json=[[["Это тест.", "これはテストです。", None, None]]],
        )

    monkeypatch.setattr(httpx, "get", fake_get)
    first = service.translate_selection("これはテストです。", "前の文脈")
    second = service.translate_selection("これはテストです。", "前の文脈")
    assert first["translation"] == "Это тест."
    assert second["translation"] == "Это тест."
    assert second["cached"] is True
    assert calls == [("https://translate.googleapis.com/translate_a/single", "ru")]


def test_local_llm_translation_uses_extended_preceding_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    # Anime/LN translation now keeps the extended preceding context.
    before = user.split("SELECTED TEXT:", 1)[0]
    assert before.count("前") == 260


def test_light_novel_state_refresh_is_background_and_deduplicated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


READER_CONTROLS = [
    "lnrFont",
    "lnrFontSize",
    "lnrTheme",
    "lnrTextColor",
    "lnrBgColor",
    "lnrWidth",
    "lnrLineHeight",
    "lnrIndent",
    "lnrVertical",
    "lnrMode",
    "lnrWordTheme",
    "lnrWordNew",
    "lnrWordNotInDeck",
    "lnrWordYoung",
    "lnrWordMature",
    "lnrWordMastered",
    "lnrWordDue",
    "lnrWordBlacklisted",
    "lnrWordSuspended",
    "lnrWordRedundant",
    "lnrWordJitenProfile",
    "lnrCustomCss",
]
REMOVED_SETTINGS = ["s_ln_reader_font", "s_ln_furigana", "s_ln_custom_css", "s_ln_translation_language"]
# Chapter: a long first paragraph (the context) and a short one to translate.
TRANSLATE_BOOK = (
    ln_book(paragraphs=["前" * 300, "これはテストです。"])
    + """
__api.light_novel_save_settings = async patch => ({...patch});
__api.translate_text = async () => ({translation: 'This is a test.'});
__api.light_novel_translate = async () => ({translation: 'This is a test.'});
"""
)


def test_reader_owns_its_appearance_and_settings_do_not(page) -> None:
    result = page.run(
        r"""
        setPage('settings', true); await __tick(50);
        const out = {settings: REMOVED.filter(id => document.getElementById(id)), settingsText: __text('#settingsContent')};
        await __settle(openLightNovel(6)); await __tick(300);
        await __click('#lnReaderAppearanceToggle'); await __tick(10);
        const panel = $('lnReaderAppearance');
        out.panel = {open: panel.checkVisibility(), missing: CONTROLS.filter(id => !panel.querySelector('#' + id)),
                     furiganaSelect: Boolean(document.getElementById('lnrFurigana'))};
        const saves = () => __apiCalls.filter(c => c.name === 'light_novel_save_settings').map(c => c.args[0]);
        await __input('#lnrFontSize', '26'); await __tick(400);
        out.size = {shown: $('lnReader').style.getPropertyValue('--ln-font-size'), saved: saves().at(-1)?.reader_font_size};
        await __input('#lnrCustomCss', '.ln-reader p { letter-spacing: 1px; }'); await __tick(400);
        out.applied = {size: $('lnReader').style.getPropertyValue('--ln-font-size'), css: $('lnCustomStyle').textContent};
        out.saved = saves().at(-1);
        // Furigana for unknown words: a click turns it off, a right-click chooses the word states.
        await __click('#lnUnknownFuriganaToggle'); await __tick(400);
        out.furigana = saves().at(-1);
        await __fire('#lnUnknownFuriganaToggle', 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent'); await __tick(10);
        out.menu = $('lnStudyStateMenu')?.classList.contains('open');
        await __input('#lnStudyStateMenu [data-ln-study-state="known"]', true); await __tick(400);
        out.states = saves().at(-1);
        return out;
        """.replace("REMOVED", json.dumps(REMOVED_SETTINGS)).replace("CONTROLS", json.dumps(READER_CONTROLS)),
        before=TRANSLATE_BOOK,
        state={"settings": {"review_gate_ln_enabled": False}},
    )
    out = result.value
    assert out["settings"] == []
    assert out["panel"] == {"open": True, "missing": [], "furiganaSelect": False}
    assert out["size"] == {"shown": "26px", "saved": 26}
    assert out["applied"] == {"size": "26px", "css": ".ln-reader p { letter-spacing: 1px; }"}
    assert (
        out["saved"]["reader_font_size"] == 26
        and out["saved"]["custom_css"] == ".ln-reader p { letter-spacing: 1px; }"
    )
    assert out["furigana"] == {"show_furigana": False}
    assert out["menu"] is True and "known" in out["states"]["furigana_states"]
    # The old note about standard app shortcuts is gone from Settings and the catalogs.
    for lang, text in (
        ("en", "App navigation uses standard Cmd+1…N and Cmd+F"),
        ("ru", "Навигация приложения использует стандартные Cmd+1…N и Cmd+F"),
    ):
        assert not has_text(text, lang)
    assert "App navigation uses standard" not in out["settingsText"]


def test_selection_translation_sends_the_preceding_context_and_closing_cancels_background_work(page) -> None:
    for lang, missing in (("en", []), ("ru", ["translate_text"])):  # older backends: light_novel_translate
        result = page.run(
            SELECTION
            + r"""
            await __settle(openLightNovel(6)); await __tick(300);
            await selectParagraph(1);
            await press('[data-pudge-translate-now]'); await __tick(20);
            const out = {shown: __text('#pudgeTranslationPop .pudge-translation-text')};
            await __click('#lnReaderClose'); await __tick(50);
            return out;
            """,
            lang=lang,
            before=TRANSLATE_BOOK,
            state={"settings": {"review_gate_ln_enabled": False}},
            missingApi=missing,
        )
        [[text, context, language, _media]] = result.calls_of(
            "light_novel_translate" if missing else "translate_text"
        )
        assert (text, language) == ("これはテストです。", lang)
        assert context == "前" * 200  # the last 200 characters before the selection
        assert result.value["shown"] == "This is a test."
        assert result.calls_of("light_novel_cancel_reader_background") == [[]]


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
