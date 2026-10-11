from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from pudge.manager_models import NyaaRelease


def _config(tmp_path: Path) -> AppConfig:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return config


def _release(title: str, info_hash: str) -> NyaaRelease:
    return NyaaRelease(
        title=title,
        link=f"https://nyaa.si/view/{info_hash}",
        torrent_url=f"https://nyaa.si/download/{info_hash}.torrent",
        info_hash=info_hash,
        size_text="1.2 GiB",
        size_bytes=1_200_000_000,
        seeders=12,
        leechers=1,
        downloads=50,
        trusted=False,
        remake=False,
    )


def test_literature_search_uses_raw_category_and_target_volume(tmp_path: Path, monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeNyaa:
        def __init__(self, *_args, **_kwargs):
            pass

        def search(self, query: str, *, category: str):
            calls.append((query, category))
            return [
                _release(
                    "[Novel] 狼と香辛料 第01-22巻 [Ookami to Koushinryou vol 01-22]",
                    "batch",
                ),
                _release("狼と香辛料 volume 23", "wrong"),
            ]

    monkeypatch.setattr("pudge.light_novels.NyaaClient", FakeNyaa)
    service = LightNovelService(_config(tmp_path))

    rows = service.search_nyaa("狼と香辛料 light novel volume 01", target_volume=1)

    assert calls == [("狼と香辛料", "3_3")]
    assert [row["info_hash"] for row in rows] == ["batch"]
    assert rows[0]["volume_range"] == [1, 22]
    assert rows[0]["contains_target_volume"] is True


def test_batch_file_selection_keeps_only_requested_volume(tmp_path: Path) -> None:
    service = LightNovelService(_config(tmp_path))

    class FakeQbt:
        def __init__(self):
            self.calls: list[tuple[list[int], int]] = []

        def files(self, _torrent_hash: str):
            return [
                {"index": 0, "name": "Ookami vol 01-22/Ookami 01.epub"},
                {"index": 1, "name": "Ookami vol 01-22/Ookami 02.epub"},
                {"index": 2, "name": "Ookami vol 01-22/cover.jpg"},
            ]

        def set_file_priority(self, _torrent_hash: str, ids: list[int], priority: int):
            self.calls.append((ids, priority))

    qbt = FakeQbt()
    selected = service._select_torrent_volume_files(qbt, "hash", 2)

    assert selected == ["Ookami vol 01-22/Ookami 02.epub"]
    assert qbt.calls == [([0, 1, 2], 0), ([1], 6)]


def test_single_batch_archive_extracts_only_requested_volume(tmp_path: Path) -> None:
    service = LightNovelService(_config(tmp_path))
    archive_path = service.root / "Ookami vol 01-22.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("Ookami 01.epub", b"first")
        archive.writestr("Ookami 02.epub", b"second")

    class FakeQbt:
        def __init__(self):
            self.calls: list[tuple[list[int], int]] = []

        def files(self, _torrent_hash: str):
            return [{"index": 0, "name": archive_path.name}]

        def set_file_priority(self, _torrent_hash: str, ids: list[int], priority: int):
            self.calls.append((ids, priority))

    qbt = FakeQbt()
    selected = service._select_torrent_volume_files(qbt, "hash", 2)
    cleanup = service._materialize_selected_volume(
        [{"index": 0, "name": selected[0], "priority": 6, "progress": 1.0}],
        2,
    )

    assert selected == [archive_path.name]
    assert cleanup == [archive_path.resolve()]
    assert (service.root / "Ookami 02.epub").read_bytes() == b"second"
    assert not (service.root / "Ookami 01.epub").exists()


def test_audiobook_uses_linked_light_novel_cover_and_anilist(tmp_path: Path, page) -> None:
    config = _config(tmp_path)
    database = Database(config.library.database_path)
    novels = LightNovelService(config)
    now = time.time()
    with novels._connect() as conn:
        conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,volume,anilist_id,cover_url,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "Spice and Wolf Vol. 1",
                str(tmp_path / "spice.epub"),
                "epub",
                1,
                127,
                "https://img.example/spice.jpg",
                now,
                now,
            ),
        )
        novel_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    audio = AudiobookService(database, ffprobe="ffprobe", mpv="mpv", cache_dir=config.paths.cache_dir)
    audiobook = audio._upsert(
        path=tmp_path / "spice.m4b",
        title="Spice and Wolf Vol. 1",
        duration=100.0,
        files=[],
        chapters=[],
    )
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO reading_audio_links(ln_book_id,audiobook_id,alignment_mode,created_at,updated_at) VALUES(?,?,'chapter',?,?)",
            (novel_id, int(audiobook["id"]), now, now),
        )

    enriched = audio.book(int(audiobook["id"]))

    assert enriched["cover_url"] == "https://img.example/spice.jpg"
    assert enriched["anilist_id"] == 127
    assert enriched["anilist_site_url"] == "https://anilist.co/manga/127"

    # The audiobook card shows that cover; a click on it opens the AniList page.
    state = json.loads(json.dumps(audio.state(queue_missing=False), default=str))
    result = page.run(
        r"""
        setPage('audiobooks'); await __tick(50);
        const card = document.querySelector('#audiobooksContent .audiobook-card');
        const cover = card.querySelector('.audiobook-cover img');
        await __click('#audiobooksContent .audiobook-cover');
        return {hasCover: card.classList.contains('has-cover'), src: cover?.getAttribute('src'),
                scrubber: card.querySelector('input.audiobook-scrubber[type=range]')?.max};
        """,
        api={"audiobook_state": state},
    )
    assert result.value == {"hasCover": True, "src": "https://img.example/spice.jpg", "scrubber": "100"}
    assert result.calls_of("open_url") == [["https://anilist.co/manga/127"]]


def test_matching_kana_pitch_is_rendered_on_the_word(page) -> None:
    result = page.run(
        r"""
        const pitch = (surface, card) => PudgeReadingTools.study.inlinePitchOnSurface(surface, card);
        const host = document.createElement('div');
        host.innerHTML = pitch('かな', {reading: 'カナ', pitchAccents: [1]});
        return {morae: [...host.querySelectorAll('.pudge-inline-pitch .pudge-pitch-mora')].map(m => m.innerText + (m.classList.contains('high') ? '↑' : '↓')),
                kanji: pitch('仮名', {reading: 'かな', pitchAccents: [1]})};
        """,
    )
    # かな in katakana reading カナ, atamadaka (1): high, then low; a kanji surface gets nothing.
    assert result.value["morae"][:2] == ["か↑", "な↓"]
    assert result.value["kanji"] == ""


def test_pitch_accent_color_is_independent_and_persists(tmp_path: Path) -> None:
    service = LightNovelService(_config(tmp_path))
    saved = service.save_settings({"word_color_due": "#ff0000", "pitch_accent_color": "#12abef"})

    assert saved["word_color_due"] == "#ff0000"
    assert saved["pitch_accent_color"] == "#12abef"
    assert LightNovelService(service.config).settings_payload()["pitch_accent_color"] == "#12abef"


# Planning: finished titles with Jiten numbers (4 has no Jiten deck, 5 is still airing).
PLANNED = [
    {"media_id": i, "title": title, "media_status": "FINISHED", "episodes": 12}
    for i, title in [(1, "Alpha"), (2, "Beta"), (3, "Gamma"), (4, "Delta")]
]
PLANNED.append({"media_id": 5, "title": "Epsilon", "media_status": "RELEASING", "episodes": 12})
PLANNED_JITEN = r"""
const STATS = {
  1: {available: true, difficulty: 2, character_count: 40000, unique_word_count: 3000, coverage_available: true, known_coverage: 91},
  2: {available: true, difficulty: 2, character_count: 120000, unique_word_count: 2000, coverage_available: true, known_coverage: 75},
  3: {available: true, difficulty: 4, character_count: 400000, unique_word_count: 9000, coverage_available: true, known_coverage: 60},
  4: {available: false}};
__api.planning_jiten_stats = async id => STATS[id];
"""


def test_planning_filters_and_sorts_by_jiten_difficulty_length_known_share_and_unique_words(page) -> None:
    result = page.run(
        r"""
        setPage('planned'); await __tick(50);
        const shown = () => [...document.querySelectorAll('#plannedContent > [data-media-id]')].filter(n => !n.hidden).map(n => n.dataset.mediaId);
        const pick = async (id, value) => { await __input('#' + id, value); await __tick(50); return shown(); };
        const out = {all: shown(), lazy: __apiCalls.filter(c => c.name === 'planning_jiten_stats').length};
        out.difficulty = await pick('plannedDifficultyFilter', '2');
        out.short = await pick('plannedLengthFilter', 'short');
        await pick('plannedDifficultyFilter', 'all'); await pick('plannedLengthFilter', 'all');
        out.known = await pick('plannedKnownFilter', '80');
        await pick('plannedKnownFilter', 'all');
        out.unique = await pick('plannedSort', 'uniqueWords');
        return out;
        """,
        state={"planned": PLANNED},
        before=PLANNED_JITEN,
    )
    out = result.value
    assert out["all"] == ["1", "2", "4", "5", "3"]  # by title; Jiten is not asked yet
    assert out["lazy"] == 0
    assert out["difficulty"] == ["1", "2"]
    assert out["short"] == ["1"]  # under 50k characters
    assert out["known"] == ["1"]  # known ≥ 80%
    # Fewest unique words first; titles without Jiten numbers go last.
    assert out["unique"][:3] == ["2", "1", "3"] and sorted(out["unique"][3:]) == ["4", "5"]
    # A Jiten filter fetches every finished title (not the airing one).
    assert sorted(call[0] for call in result.calls_of("planning_jiten_stats")) == [1, 2, 3, 4]


READER_COLORS = r"""
populateLnReaderAppearance(ui.lnState.settings); syncLnReaderControlAvailability();
const controls = ['lnrWordTheme', 'lnrWordNew', 'lnrWordDue', 'lnrStudyMode'];
const available = () => controls.map(id => !$(id).disabled && !$(id).classList.contains('settings-unavailable'));
"""


def test_reader_word_color_pickers_and_custom_css_stay_in_sync(page) -> None:
    result = page.run(
        READER_COLORS
        + r"""
        const out = {available: available()};
        await __input('#lnrWordDue', '#123456'); await __tick(300);
        out.css = $('lnrCustomCss').value; out.theme = $('lnrWordTheme').value;
        await __input('#lnrCustomCss', $('lnrCustomCss').value.replace('#123456', '#abcdef')); await __tick(300);
        out.due = $('lnrWordDue').value;
        return out;
        """,
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {"jiten_api_key": "key", "custom_css": ".ln-reader p{margin:0}"},
                "books": [],
            }
        },
    )
    out = result.value
    assert out["available"] == [True, True, True, True]
    # A picked color becomes the custom theme and a managed block in the custom CSS (the user's CSS stays).
    assert out["theme"] == "custom"
    assert out["css"].startswith(".ln-reader p{margin:0}") and "--pudge-study-due: #123456;" in out["css"]
    # Editing the color in the CSS moves the picker.
    assert out["due"] == "#abcdef"
    saved = [call[0] for call in result.calls_of("light_novel_save_settings")]
    assert [
        (row["word_color_due"], "--pudge-study-due: " + row["word_color_due"] in row["custom_css"])
        for row in saved
    ] == [("#123456", True), ("#abcdef", True)]
    # Without a Jiten key the word-color controls are unavailable.
    no_key = page.run(
        READER_COLORS + "return available();",
        api={"light_novel_state": {"version": 1, "settings": {}, "books": []}},
    )
    assert no_key.value == [False, False, False, False]


def test_semantic_subtitle_checks_need_the_llm(page) -> None:
    result = page.run(
        r"""
        const box = () => ({checked: $('s_subtitle_semantic_checks').checked, available: !$('s_subtitle_semantic_checks').disabled
                            && !$('s_subtitle_semantic_checks').classList.contains('settings-unavailable')});
        const off = box();
        await __input('#s_llm_enabled', true);
        return {off, on: box()};
        """,
        state={"settings": {"llm_enabled": False, "subtitle_semantic_checks": True}},
    )
    # Switched on in an old config but without an LLM: shown off and unavailable until the LLM is on.
    assert result.value == {
        "off": {"checked": False, "available": False},
        "on": {"checked": False, "available": True},
    }


def test_word_card_numbers_dictionary_definitions(page) -> None:
    payload = {
        "text": "猫",
        "paragraphs": ["猫"],
        "tokens": [[{"wordId": 7, "readingIndex": 0, "start": 0, "end": 1}]],
        "vocabulary": [
            {
                "wordId": 7,
                "readingIndex": 0,
                "spelling": "猫",
                "reading": "ねこ",
                "meanings": [["cat"], ["geisha"]],
                "knownState": ["new"],
            }
        ],
        "settings": {},
    }
    result = page.run(
        r"""
        await __settle(PudgeReadingTools.study.openText('猫', {left: 0, top: 0, right: 20, bottom: 20}));
        return [...document.querySelectorAll('#pudgeStudyCard ol li')].map(li => li.innerText);
        """,
        api={"study_parse_text": payload},
    )
    assert result.value == ["cat", "geisha"]
