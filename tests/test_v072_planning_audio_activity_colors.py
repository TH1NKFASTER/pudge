from __future__ import annotations

import pytest

from pudge.audio_activity import activity_regions_from_features, gate_activity_regions
from pudge.reading_audio_alignment import (
    audio_position_for_light_novel_offset,
    light_novel_position_for_audio,
)

from tests.css_support import css_rules
from tests.i18n_support import ru


def test_fft_features_keep_sentence_pauses_separate() -> None:
    np = pytest.importorskip("numpy")
    energy = np.full(260, -72.0, dtype=np.float32)
    energy[40:100] = -24.0
    energy[135:205] = -22.0
    flux = np.zeros_like(energy)
    flux[40] = 1.0
    flux[135] = 1.0

    regions, diagnostics = activity_regions_from_features(energy, flux)

    assert len(regions) == 2
    assert regions[0]["end"] < regions[1]["start"]
    assert diagnostics["speech_threshold_db"] > diagnostics["noise_floor_db"]


def test_fft_activity_is_gated_by_stt_to_reject_music() -> None:
    regions = [{"start": 0.0, "end": 1.0}, {"start": 2.0, "end": 3.0}]
    hints = [{"start": 2.2, "end": 2.8}]

    assert gate_activity_regions(regions, hints, padding_seconds=0.1) == [{"start": 2.1, "end": 2.9}]


def test_anchor_clock_is_linear_even_when_fft_reports_silence() -> None:
    alignment = {
        "schema": "reading-audio-v3",
        "chapters": [
            {
                "chapter_index": 0,
                "normalized_length": 100,
                "start": 10.0,
                "end": 18.0,
                "anchors": [
                    {"offset": 0, "time": 10.0},
                    {"offset": 100, "time": 18.0},
                ],
                "speech_regions": [
                    {"start": 10.0, "end": 12.0},
                    {"start": 16.0, "end": 18.0},
                ],
            }
        ],
    }

    middle = light_novel_position_for_audio(alignment, 14.0)
    late = light_novel_position_for_audio(alignment, 15.5)
    near_end = light_novel_position_for_audio(alignment, 17.0)

    assert middle is not None
    assert late is not None
    assert near_end is not None
    assert middle["chapter_char_offset_exact"] == 50.0
    assert late["chapter_char_offset_exact"] == 68.75
    assert near_end["chapter_char_offset_exact"] == 87.5
    assert middle["anchor_window"]["activity"] == alignment["chapters"][0]["speech_regions"]
    assert audio_position_for_light_novel_offset(alignment, 0, 50) == 14.0


# A background search for the released episodes of a Planning anime: running, then done.
JOB = """
let statusCalls = 0;
__api.start_planning_episode_download = async id => ({running: true, media_id: id, title: 'Planned', current: 0, total: 3, episodes: []});
__api.planning_episode_download_status = async () => (++statusCalls < 3 ? {running: statusCalls > 1, title: 'Planned', current: 1, total: 3, episodes: []}
  : {running: false, title: 'Planned', current: 3, total: 3, finished_at: 1790000000, episodes: [
      {episode: 1, status: 'local'}, {episode: 2, status: 'added', release: '[Group] Planned - 02'}, {episode: 3, status: 'missing'}]});
"""


def test_planning_card_auto_downloads_released_episodes(page) -> None:
    assert ru("ui.autoDownloadEpisodes") == "Автоскачать серии"
    anime = {
        "media_id": 9,
        "title": "Planned",
        "episodes": 3,
        "media_status": "FINISHED",
        "status": "PLANNING",
        "progress": 0,
    }
    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('planned', true); await __tick(20);
            const states = () => __apiCalls.filter(call => call.name === 'get_state').length;
            const out = {button: __text('#plannedContent [data-action="planning-episodes-auto-card"]'), label: PudgeI18n.t('ui.autoDownloadEpisodes')};
            const before = states();
            await __click('#plannedContent [data-action="planning-episodes-auto-card"]'); await __flush();
            out.progress = {title: __text('#modalTitle'), body: __text('#modalBody')};
            await __tick(1500);
            out.done = {title: __text('#modalTitle'), body: __text('#modalBody'), reloaded: states() - before};
            out.texts = {searching: PudgeI18n.t('ui.searchingReleasedEpisodes'), checking: PudgeI18n.t('ui.checkingLocalFilesAndSearching'),
                         complete: PudgeI18n.t('ui.searchComplete'), added: PudgeI18n.t('ui.added'), local: PudgeI18n.t('ui.alreadyLocal'),
                         missing: PudgeI18n.t('ui.notFound'), summary: PudgeI18n.t('ui.addedAlreadyLocalNotFound', {added: 1, local: 1, missing: 1, error: 0})};
            return out;
            """,
            lang=lang,
            state={"planned": [anime]},
            before=JOB,
        )
        out, texts = result.value, result.value["texts"]
        assert out["button"] == out["label"]
        assert result.calls_of("start_planning_episode_download") == [[9]]
        assert out["progress"]["title"] == f"{texts['searching']} — Planned"
        assert out["progress"]["body"].startswith(f"{texts['checking']} 0 / 3")
        assert out["done"]["title"] == f"{texts['complete']} — Planned"
        body = out["done"]["body"]
        assert body.startswith(texts["summary"])
        assert all(
            text in body
            for text in (texts["local"], texts["added"], "[Group] Planned - 02", texts["missing"])
        )
        assert out["done"]["reloaded"] == 1  # the added episode shows up in the library


READER = """
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}]});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['本文です。'], tokens: [[]]});
__api.light_novel_save_settings = async values => ({settings: values});
"""


def test_ln_word_color_rows_show_no_hex_and_only_the_swatch_opens_the_picker(page) -> None:
    result = page.run(
        """
        await __settle(openLightNovel(6));
        await __click('#lnReaderAppearanceToggle'); await __tick(20);
        const box = document.getElementById('lnReaderAppearance');
        const ids = [...box.querySelectorAll('.ln-word-color-picker input[type=color]')].map(input => input.id);
        const opened = [];
        for (const id of ids) $(id).addEventListener('click', () => opened.push(id));
        const name = $('lnrWordNew').getAttribute('aria-labelledby');
        await __click('#' + name);   // the row's name
        const viaRow = opened.slice();
        await __click('#lnrWordNew');                               // the swatch itself
        return {ids, viaRow, opened, name: $(name).textContent,
                hex: [...box.querySelectorAll('.ln-word-color-picker')].map(row => row.innerText).filter(text => /#[0-9a-f]{3}/i.test(text)),
                labelFor: [...box.querySelectorAll('label[for]')].map(label => label.htmlFor).filter(id => ids.includes(id))};
        """,
        before=READER,
        state={"settings": {"review_gate_ln_enabled": False}},
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {
                    "jiten_api_key": "key",
                    "word_color_theme": "custom",
                    "word_color_new": "#f3f6fb",
                },
                "books": [{"id": 6, "title": "Novel", "volume": 1}],
            }
        },
    )
    out = result.value
    assert out["ids"] == [
        "lnrWordNew",
        "lnrWordNotInDeck",
        "lnrWordYoung",
        "lnrWordMature",
        "lnrWordMastered",
        "lnrWordDue",
        "lnrWordBlacklisted",
        "lnrWordSuspended",
        "lnrWordRedundant",
    ]
    assert out["name"] == "New"
    assert out["hex"] == [] and out["labelFor"] == []
    assert out["viaRow"] == [] and out["opened"] == ["lnrWordNew"]
    # The swatch fills the picker (no inner frame), so the chosen colour is what one sees.
    rules = css_rules("index.html")
    assert (
        rules.get('.ln-word-color-picker input[type="color"]::-webkit-color-swatch-wrapper', "padding") == "0"
    )
    assert rules.get('.ln-word-color-picker input[type="color"]::-webkit-color-swatch', "border") == "0"
