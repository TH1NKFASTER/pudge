"""Release items of v0.7.20, part one (behaviour; audit 71Б rewrite).

Settings order and placement, listen-together pause, debug exports, manga word clicks
and the manga reader's OCR button. Window checks run index.html in node
(tests/page_harness.py, fixture `page`)."""

from __future__ import annotations

import os
import time
from pathlib import Path

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from pudge.logging_utils import DEBUG_LOG_RETENTION_SECONDS, debug_log_dir
from t71g20_support import LN_PAIRED, PAIRED_VIEW
from tests.i18n_support import en, ru

# --- Settings ---------------------------------------------------------------------------

SETTINGS_TABS = r"""
setPage('settings'); await __tick(50);
const tab = async name => { await __click(`.settings-categories [data-settings-category="${name}"]`); await __tick(10); };
"""


def test_essential_settings_show_jiten_jpdb_keys_then_srs_source_then_mpv_plugin(page) -> None:
    assert en("ui.jitenJpdb") == "Jiten / JPDB"
    assert en("settings.lnStudyBackend") == "SRS/Vocab source"
    assert ru("settings.lnStudyBackend") == "Источник SRS/словаря"
    for lang in ("en", "ru"):
        result = page.run(
            SETTINGS_TABS
            + r"""
            await tab('essential');
            const ids = ['s_ln_jiten_key', 's_ln_jpdb_key', 's_ln_backend', 's_mpv_study_plugin'];
            const block = $('s_ln_jiten_key').closest('.setting-block');
            return {visible: ids.filter(id => __visible('#' + id)),
                    order: [...block.querySelectorAll('[id]')].map(n => n.id).filter(id => ids.includes(id)),
                    heading: block.querySelector('h3').innerText,
                    label: __text('label[for="s_ln_backend"]'),
                    expected: {heading: t('ui.jitenJpdb'), label: t('settings.lnStudyBackend')}};
            """,
            lang=lang,
        )
        out = result.value
        # One block on the Essential tab: the two keys, then the SRS source, then the mpv plugin.
        assert out["visible"] == ["s_ln_jiten_key", "s_ln_jpdb_key", "s_ln_backend", "s_mpv_study_plugin"]
        assert out["order"] == out["visible"]
        assert (out["heading"], out["label"]) == (out["expected"]["heading"], out["expected"]["label"])


def test_light_novel_auto_download_is_an_advanced_setting(page) -> None:
    result = page.run(
        SETTINGS_TABS
        + r"""
        const shown = {};
        for (const name of ['essential', 'downloads', 'advanced']) { await tab(name); shown[name] = __visible('#s_ln_auto_download'); }
        return shown;
        """,
    )
    assert result.value == {"essential": False, "downloads": False, "advanced": True}


def test_light_novel_auto_download_is_off_until_the_user_turns_it_on(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.root_dir.mkdir(parents=True)
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.qbittorrent.enabled = True
    service = LightNovelService(cfg)
    looked: list[bool] = []
    monkeypatch.setattr(service, "anilist_novels", lambda **_kw: looked.append(True) or [])

    assert service.settings().auto_download_nyaa is False
    assert service.auto_download_missing() == [] and looked == []  # a fresh install never searches Nyaa

    service.save_settings({"auto_download_nyaa": True})
    assert service.settings().auto_download_nyaa is True
    service.auto_download_missing()
    assert looked == [True]


# --- Listen together ----------------------------------------------------------------------


def test_pausing_listen_together_reaches_mpv_before_any_slow_state_read(page) -> None:
    # Old bug: the pause first waited for light_novel_paired_state (slow while the backend is
    # busy), so the narration kept talking. The pause must go to mpv at once and the
    # highlight must stop where it is.
    result = page.run(
        PAIRED_VIEW
        + r"""
        await openBook();
        await __key(' ', {code: 'Space'}, 'body');
        await __tick(3200);
        __api.light_novel_paired_state = async () => { await new Promise(r => setTimeout(r, 2000)); return pairedState(); };
        const since = __apiCalls.length;
        const atPause = highlight();
        await __key(' ', {code: 'Space'}, 'body');
        await __flush();
        const transport = __apiCalls.slice(since).filter(c => ['audiobook_set_paused', 'light_novel_paired_state'].includes(c.name))
          .map(c => [c.name, ...c.args]);
        await __tick(1500);
        return {transport, atPause, later: highlight(), button: __text('#lnPairedAudio'), resume: t('ui.resume')};
        """,
        before=LN_PAIRED,
    )
    out = result.value
    assert out["transport"][0] == ["audiobook_set_paused", 9, True]
    assert out["later"] == out["atPause"] and out["atPause"]["words"] == ["0:いいい"]
    assert out["button"] == out["resume"]
    assert result.calls_of("audiobook_set_paused") == [[9, False], [9, True]]


# --- Debug exports ------------------------------------------------------------------------


def test_debug_exports_stay_in_the_debug_folder_for_a_week(tmp_path: Path, monkeypatch) -> None:
    from test_web_app import make_api

    week = 7 * 24 * 3600
    assert DEBUG_LOG_RETENTION_SECONDS == week
    folder = debug_log_dir(cleanup=False)
    expired, recent = folder / "expired-manga-ocr.zip", folder / "recent-manga-ocr.zip"
    for path, age in ((expired, week + 60), (recent, week - 60)):
        path.write_bytes(b"zip")
        os.utime(path, (time.time() - age, time.time() - age))
    # Pudge's own unpacked staging folder of an export goes after two hours.
    staging = folder / "20260101-000000-000001-manga-ocr"
    staging.mkdir()
    (staging / "page.png").write_bytes(b"png")
    os.utime(staging, (time.time() - 2 * 3600 - 60, time.time() - 2 * 3600 - 60))

    api = make_api(tmp_path)
    monkeypatch.setattr(
        api.manga,
        "page",
        lambda book_id, index: {
            "book_id": book_id,
            "page_index": index,
            "name": "p1.png",
            "data_uri": "data:image/png;base64,AAAA",
        },
    )
    monkeypatch.setattr(api.manga, "text_regions", lambda *_a, **_k: {"regions": []})
    opened: list[list[str]] = []
    monkeypatch.setattr("pudge.web_app.subprocess.Popen", lambda command, *a, **k: opened.append(command))

    result = api.manga_export_ocr_debug({"book": {"id": 3}, "page_index": 0, "events": [], "frames": []})

    archive = Path(result["path"])
    assert result["ok"] is True and archive.is_file()
    assert archive.parent == folder  # Pudge's own debug folder, never Downloads
    assert opened == [["open", "-R", str(archive)]]
    # Exporting cleaned up bundles older than a week and old staging folders, and kept the rest.
    assert not expired.exists() and recent.exists() and not staging.exists()
    recent.unlink()


# --- Manga reader -------------------------------------------------------------------------

# One page (1000 x 1000 px on screen) with OCR regions; y is measured from the bottom of the
# page, as Vision reports it:
#   0: 猫が好き at the top, with per-character boxes from Vision (x 100..500, y 150..200 px)
#   1: 猫 alone, without character boxes (x 600..700, y 150..200 px)
#   2: 犬が来た without character boxes (x 100..500, y 450..500 px)
#   3: vertical 鳥が鳴く without character boxes (x 800..850, y 300..700 px)
MANGA_WORDS = r"""
const seg = (text, x) => ({text, x, y: 0.8, width: 0.1, height: 0.05, source: 'vision-accurate-range-v2'});
const REGIONS = [
  {text: '猫が好き', orientation: 'horizontal', x: 0.1, y: 0.8, width: 0.4, height: 0.05,
   segments: [seg('猫', 0.1), seg('が', 0.2), seg('好', 0.3), seg('き', 0.4)]},
  {text: '猫', orientation: 'horizontal', x: 0.6, y: 0.8, width: 0.1, height: 0.05},
  {text: '犬が来た', orientation: 'horizontal', x: 0.1, y: 0.5, width: 0.4, height: 0.05},
  {text: '鳥が鳴く', orientation: 'vertical', x: 0.8, y: 0.3, width: 0.05, height: 0.4},
];
const WORDS = {'猫が好き': ['猫', 'が', '好き'], '猫': ['猫'], '犬が来た': ['犬', 'が', '来た'], '鳥が鳴く': ['鳥', 'が', '鳴く']};
const ids = {};
__api.manga_state = async () => ({books: [{id: 3, title: 'Vol 1', series_title: 'Manga', page_count: 1, position: 0, ocr_complete: true}], ocr_available: true});
__api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: 1, name: 'p1.png', spread: false,
                                         data_uri: 'data:image/png;base64,AAAA', width: 1000, height: 1000});
__api.manga_text_regions = async (id, index) => ({book_id: id, page_index: index, available: true, cached: true, regions: REGIONS});
__api.study_parse_text = async text => {
  let at = 0;
  const words = WORDS[text];
  for (const w of words) ids[w] = ids[w] || Object.keys(ids).length + 1;
  return {paragraphs: [text], tokens: [words.map(w => ({wordId: ids[w], readingIndex: 0, start: at, end: (at += w.length)}))],
          vocabulary: words.map(w => ({wordId: ids[w], readingIndex: 0, spelling: w, reading: w, knownState: ['new']}))};
};
"""
CLICK_WORD = r"""
await __settle(PudgeMangaReaderV2.openBook(3)); await __tick(500);
document.querySelector('#mangaV2Pages .manga-v2-page-frame img').__rect = {x: 0, y: 0, width: 1000, height: 1000};
const clickAt = async (x, y) => {
  await __click('#mangaV2Pages .manga-v2-page-frame img', {clientX: x, clientY: y}); await __tick(300);
  const card = document.getElementById('pudgeStudyCard');
  const word = card?.classList.contains('open') ? card.innerText.split('\n')[0] : null;
  document.querySelector('[data-pudge-study-close]')?.click(); await __tick(50);
  return word;
};
"""


def test_manga_word_clicks_follow_ocr_character_boxes_and_never_a_made_up_grid(page) -> None:
    result = page.run(
        CLICK_WORD
        + r"""
        return {
          first: await clickAt(130, 175),      // on 猫 of 猫が好き
          suki: await clickAt(350, 175),       // on 好
          ki: await clickAt(470, 175),         // on き: same word
          alone: await clickAt(660, 190),      // the one-word region without character boxes
          noBoxes: await clickAt(430, 475),    // 来 of 犬が来た: no character boxes, several words
          vertical: await clickAt(825, 500),   // 鳴 of the vertical 鳥が鳴く without character boxes
        };
        """,
        before=MANGA_WORDS,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    # Characters map to words through Vision's character boxes on the page image.
    assert result.value["first"] == "猫"
    assert result.value["suki"] == result.value["ki"] == "好き"
    # A region that is one word is that word everywhere.
    assert result.value["alone"] == "猫"
    # Without character boxes Pudge does not guess where each word is (no synthetic grid).
    assert result.value["noBoxes"] is None and result.value["vertical"] is None


def test_manga_reader_offers_volume_ocr_and_no_single_page_ocr(page) -> None:
    result = page.run(
        r"""
        await __settle(PudgeMangaReaderV2.openBook(3)); await __tick(100);
        const actions = [...document.querySelectorAll('#mangaReaderV2 [data-manga-v2-action]')].map(n => n.dataset.mangaV2Action);
        return {actions, ocrBook: __visible('#mangaReaderV2 [data-manga-v2-action="ocr-book"]')};
        """,
        before=MANGA_WORDS,
        state={"settings": {"review_gate_manga_enabled": False}},
    )
    assert result.value["ocrBook"] is True
    assert "ocr-page" not in result.value["actions"]
