from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pudge.audiobooks import AudiobookService
from pudge.config import AppConfig
from pudge.database import Database
from pudge.light_novels import LightNovelService
from tests.css_support import css_rules
from tests.i18n_support import en, has_text, ru


def _audio(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "db.sqlite3"),
        ffprobe="ffprobe",
        ffmpeg="ffmpeg",
        mpv="mpv",
        cache_dir=tmp_path / "cache",
    )


def _ln(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.database_path = tmp_path / "db.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    return LightNovelService(cfg)


def _put_audio(service: AudiobookService, source: Path, title: str, duration: float = 10.0) -> dict:
    source.write_bytes(b"audio")
    return service._upsert(
        path=source,
        title=title,
        duration=duration,
        files=[
            {
                "index": 0,
                "path": str(source),
                "title": title,
                "duration": duration,
                "start": 0.0,
                "end": duration,
            }
        ],
        chapters=[{"index": 0, "title": title, "start": 0.0, "end": duration}],
    )


def test_icloud_probe_timeout_imports_placeholder_and_defers_stt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _audio(tmp_path)
    source = tmp_path / "また、同じ夢を見ていた.m4b"
    source.write_bytes(b"audio")
    calls = []

    def timeout_probe(path: Path, *, timeout: float = 30.0):
        calls.append(timeout)
        raise subprocess.TimeoutExpired(["ffprobe", str(path)], timeout)

    queued = []
    monkeypatch.setattr(service, "_probe", timeout_probe)
    monkeypatch.setattr(
        service, "_queue_metadata_refresh", lambda book_id, path: queued.append((book_id, path))
    )
    monkeypatch.setattr(
        service, "prepare_transcription", lambda *_a, **_k: pytest.fail("STT must wait for metadata")
    )
    book = service.import_file(source)
    assert calls == [3.0]
    assert book["duration"] == 0.0
    assert queued and queued[0][1] == source.resolve()


def test_zero_duration_state_retries_metadata_instead_of_stt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _audio(tmp_path)
    source = tmp_path / "cloud.m4b"
    book = _put_audio(service, source, "Cloud", 0.0)
    queued = []
    monkeypatch.setattr(
        service, "_queue_metadata_refresh", lambda book_id, path: queued.append((book_id, path))
    )
    monkeypatch.setattr(
        service, "prepare_transcription", lambda *_a, **_k: pytest.fail("must not queue STT before metadata")
    )
    state = service.state(queue_missing=True)
    assert state["books"][0]["metadata_pending"] is True
    assert queued == [(int(book["id"]), source.resolve())]


def test_v2_transcript_cache_is_migrated_without_retranscription(tmp_path: Path) -> None:
    service = _audio(tmp_path)
    source = tmp_path / "wolf.m4b"
    book = _put_audio(service, source, "Wolf", 60.0)
    book_id = int(book["id"])
    legacy = service._legacy_transcript_path_v2(book_id)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps(
            {
                "schema": "audiobook-stt-v2",
                "model": service.stt_model,
                "segments": [{"start": 1.0, "end": 2.0, "text": "狼"}],
            }
        ),
        encoding="utf-8",
    )
    current = service._transcript_path(book_id)
    assert not current.exists()
    status = service.prepare_transcription(book_id)
    assert status["ready"] is True and status["status"] == "ready"
    payload = json.loads(current.read_text(encoding="utf-8"))
    assert payload["schema"] == "audiobook-stt-v3"
    assert payload["migrated_from"] == "audiobook-stt-v2"
    assert not service._transcription_queue


def test_bulk_delete_avoids_expensive_cancel_and_stop_for_inactive_books(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _audio(tmp_path)
    ids = []
    for i in range(3):
        ids.append(int(_put_audio(service, tmp_path / f"b{i}.mp3", f"B{i}")["id"]))
    monkeypatch.setattr(
        service, "cancel_transcription", lambda *_a, **_k: pytest.fail("inactive STT cancel must not run")
    )
    monkeypatch.setattr(service, "stop", lambda *_a, **_k: pytest.fail("inactive player stop must not run"))
    result = service.delete_many(ids)
    assert result == {"ok": True, "removed": ids, "errors": []}
    assert service.state(queue_missing=False)["books"] == []


def test_horizontal_furigana_has_a_fixed_lane_and_centered_overlay() -> None:
    rules = css_rules("index.html")
    # One fixed half-em lane above every horizontal line; the reading never reflows the text.
    assert rules.get("#lnReader:not(.vertical)", "line-height") == "calc(var(--ln-line-height,1.9) + .5)"
    overlay = rules.declarations("#lnReader:not(.vertical) .ln-furigana-ruby::after")
    assert (overlay["position"], overlay["font-size"], overlay["white-space"]) == (
        "absolute",
        ".5em",
        "nowrap",
    )
    # 1em of the half-size overlay = half a base glyph: the reading sits over the word's middle.
    assert overlay["transform"] == "translateX(calc(-50% - 1em))"


def test_ln_card_jiten_tooltip_is_the_catalog_detail_line(page) -> None:
    assert en("uiA.jiten.detail", words=5, unique=3) == "Words: 5 · unique: 3"
    assert ru("uiA.jiten.detail", words=5, unique=3) == "Слов: 5 · уникальных: 3"
    assert not has_text("Jiten · words:") and not has_text("Jiten · слов:", "ru")
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('lightnovels'); await __tick(100);
            const locale = PudgeI18n.t('ui.en');
            return {tooltip: document.querySelector('[data-ln-book="4"] [data-ln-card-jiten] [data-tooltip]')?.getAttribute('data-tooltip'),
                    expected: t('uiA.jiten.detail', {words: (12345).toLocaleString(locale), unique: (3210).toLocaleString(locale)})};
            """,
            lang=lang,
            api={
                "light_novel_state": {
                    "version": 1,
                    "settings": {},
                    "books": [
                        {"id": 4, "title": "Novel", "volume": 1, "character_count": 1000, "jiten_deck_id": 44}
                    ],
                },
                "light_novel_jiten_stats": {
                    "available": True,
                    "difficulty": 2,
                    "word_count": 12345,
                    "unique_word_count": 3210,
                },
            },
        )
        assert result.value["tooltip"] == result.value["expected"]
        assert "Jiten ·" not in result.value["tooltip"]


# A light novel (id 6) linked to audiobook 75. window.__link is what light_novel_open says
# about the audiobook player, window.__actual what the player really does.
PAIRED = r"""
window.__wait = ms => new Promise(resolve => setTimeout(resolve, ms));
window.__link = {id: 75, playing: false, player_running: false, paused: false};
window.__actual = {audiobook_id: 75, ln_book_id: 6, ln_chapter_index: 0, alignment: {ready: true, status: 'ready'}, linked: true,
                   playing: false, player_running: false, paused: false, position: 0, speed: 1, duration: 3600, chapter_char_offset_exact: 0};
window.__starts = [];                                   // answers of light_novel_play_paired (null: lost)
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}],
  paired_audio: {ln_book_id: id, alignment_mode: 'ready', alignment: {ready: true, status: 'ready'}, book: {...window.__link}}});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['本文です。'], tokens: [[]]});
__api.light_novel_paired_state = async () => { await window.__wait(5); return {...window.__actual}; };
__api.light_novel_play_paired = async () => {
  await window.__wait(20);
  if (window.__starts.length && window.__starts.shift() === null) return null;
  window.__actual = {...window.__actual, playing: true, paused: false, player_running: true, position: 50};
  return {...window.__actual};
};
__api.audiobook_set_paused = async (_id, paused) => {
  await window.__wait(30); window.__actual = {...window.__actual, playing: !paused, paused}; return {ok: true};
};
"""
LISTEN = r"""
const settle = async promise => { let done = false; promise.then(() => { done = true; }, () => { done = true; });
                                  for (let k = 0; k < 200 && !done; k++) await __tick(10); };
await settle(openLightNovel(6));
const label = __text('#lnPairedAudio');               // before the reader's first look at the player
await __click('#lnPairedAudio');
await __tick(3000);
return {label, after: __text('#lnPairedAudio'), playing: window.__actual.playing,
        labels: {listen: PudgeI18n.t('ui.listen'), resume: PudgeI18n.t('ui.resume'), pause: PudgeI18n.t('ui.pause')}};
"""
LN_SETTINGS = {"settings": {"review_gate_ln_enabled": False, "review_gate_manga_enabled": False}}
IDLE = {"startup_maintenance": {"running": False}, "ui_state_versions": {"ready": "1", "ui": "1"}}


def test_first_listen_starts_from_the_reader_and_a_live_player_is_resumed(page) -> None:
    def listen(setup: str):
        result = page.run(LISTEN, before=PAIRED + setup, state=LN_SETTINGS, api=IDLE)
        transport = [
            [call["name"], *call["args"]]
            for call in result.calls
            if call["name"] in {"light_novel_play_paired", "audiobook_set_paused"}
        ]
        return result.value, transport

    # Nothing plays: Listen starts the audiobook at the reader's position.
    out, transport = listen("")
    assert out["label"] == out["labels"]["listen"] and out["after"] == out["labels"]["pause"]
    assert transport == [["light_novel_play_paired", 6, 0, 0, 1]]

    # The player still holds a paused session at an old position the reader was not told about,
    # and the first start is not acknowledged: Listen starts from the reader again instead of
    # resuming the stale position.
    out, transport = listen(
        "window.__actual = {...window.__actual, player_running: true, paused: true, position: 999}; window.__starts = [null];"
    )
    assert out["label"] == out["labels"]["listen"] and out["playing"] is True
    assert transport == [["light_novel_play_paired", 6, 0, 0, 1]] * 2

    # Opened while this audiobook's player is paused (an active read-together session): the
    # button resumes that player where it is.
    out, transport = listen(
        "window.__link = {id: 75, playing: false, player_running: true, paused: true};"
        "window.__actual = {...window.__actual, player_running: true, paused: true, position: 999};"
    )
    assert out["label"] == out["labels"]["resume"] and out["after"] == out["labels"]["pause"]
    assert transport == [["audiobook_set_paused", 75, False]]


AUDIOBOOKS = [
    {
        "id": 2,
        "title": "Zeta Story",
        "duration": 100,
        "position": 0,
        "cover_url": "covers/zeta.jpg",
        "anilist_id": 202,
        "anilist_site_url": "https://anilist.co/manga/202",
        "chapters": [
            {"index": 0, "title": "One", "start": 0, "end": 50},
            {"index": 1, "title": "Two", "start": 50, "end": 100},
        ],
        "files": [],
    },
    {
        "id": 1,
        "title": "Alpha Story",
        "duration": 100,
        "position": 0,
        "cover_url": "covers/alpha.jpg",
        "anilist_id": 101,
        "anilist_site_url": "https://anilist.co/manga/101",
        "chapters": [{"index": 0, "title": "One", "start": 0, "end": 100}],
        "files": [],
    },
    {"id": 3, "title": "Mid Story", "duration": 100, "position": 0, "chapters": [], "files": []},
]


def test_v135_library_selection_and_furigana_contracts(page) -> None:
    # Audiobooks are selected like light novels and manga: a click on a card, Cmd+A, the page's
    # selection bar (count, Cancel selection, Delete). There is no separate selection mode.
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('audiobooks'); await __tick(100);
            const selected = () => [...document.querySelectorAll('#audiobooksContent .audiobook-card.selected')].map(card => card.dataset.audiobookId);
            const bar = () => __visible('#pageSelectionActions') ? __text('#pageSelectionActions').split('\n') : null;
            const out = {modeControls: document.querySelectorAll('[data-audio-selection-mode],[data-audio-select-all],.audiobook-select-toggle').length,
                         labels: [PudgeI18n.t('ui.cancelSelection'), PudgeI18n.t('ui.delete')]};
            await __click('#audiobooksContent [data-audiobook-id="1"] .audiobook-title-row strong');
            out.one = {selected: selected(), bar: bar()};
            await __click('#audiobooksContent [data-audiobook-id="2"] summary');          // opening the chapter list
            out.summary = selected();
            await __key('a', {metaKey: true}, 'body');
            out.all = {selected: selected(), bar: bar()};
            await __click('#pageSelectionActions [data-selection-action=clear]');
            out.cleared = {selected: selected(), bar: bar()};
            await __click('#audiobooksContent [data-audiobook-id="3"] .audiobook-title-row strong');
            await __click('#audiobooksContent [data-audiobook-id="2"] .audiobook-title-row strong');
            await __click('#pageSelectionActions [data-selection-action=delete]'); await __tick(20);
            out.question = __text('#pudgeConfirmMessage');
            await __click('#pudgeConfirmBackdrop [data-pudge-confirm=ok]'); await __tick(20);
            out.left = [...document.querySelectorAll('#audiobooksContent .audiobook-card')].map(card => card.dataset.audiobookId);
            out.expectedQuestion = t('ui.removeSelectedFromPudgeSource', {count: 2});
            // Cmd+A on the manga page selects the manga volumes.
            setPage('manga'); await __tick(100);
            await __key('a', {metaKey: true}, 'body');
            out.manga = {selected: [...document.querySelectorAll('#mangaContent [data-manga-book].selected')].map(card => card.dataset.mangaBook), bar: bar()};
            return out;
            """,
            lang=lang,
            api={
                "audiobook_state": {"books": AUDIOBOOKS},
                "audiobook_delete_many": {"ok": True, "removed": [3, 2], "errors": []},
                "manga_state": {
                    "books": [
                        {"id": 8, "title": "Vol 1", "series_title": "Manga", "page_count": 20, "position": 0},
                        {"id": 9, "title": "Vol 2", "series_title": "Manga", "page_count": 20, "position": 0},
                    ],
                    "ocr_available": False,
                },
            },
        )
        out = result.value
        cancel, delete = out["labels"]
        assert out["modeControls"] == 0
        assert out["one"] == {"selected": ["1"], "bar": ["1", cancel, delete]}
        assert out["summary"] == ["1"]
        assert out["all"] == {"selected": ["1", "3", "2"], "bar": ["3", cancel, delete]}
        assert out["cleared"] == {"selected": [], "bar": None}
        assert out["question"] == out["expectedQuestion"]
        assert result.calls_of("audiobook_delete_many") == [[[3, 2]]]
        assert out["left"] == ["1"]
        assert sorted(out["manga"]["selected"]) == ["8", "9"] and out["manga"]["bar"][0] == "2"
    # A selected card is visibly marked; hovering a card hints that it is clickable.
    media = css_rules("media.css")
    assert media.get(".audiobook-card.selected", "box-shadow").startswith("inset ")
    assert (
        media.get(".audiobook-card:hover", "border-color")
        and media.get(".audiobook-card", "cursor") == "pointer"
    )
    # The study card header is a bounded grid (text column + controls); the actions cell is a row.
    study = css_rules("reading_tools.css")
    assert (
        study.get(".pudge-study-head", "display"),
        study.get(".pudge-study-head", "grid-template-columns"),
    ) == ("grid", "minmax(0,1fr) auto auto")
    assert (
        study.get(".pudge-study-head-actions", "display"),
        study.get(".pudge-study-head-actions", "align-items"),
    ) == ("flex", "center")


def test_sorted_audiobook_cover_actions_bind_by_card_id_not_render_index(page) -> None:
    # The library is drawn sorted by title (Alpha, Mid, Zeta) while the state lists Zeta first;
    # each card gets its own cover and AniList actions.
    result = page.run(
        r"""
        setPage('audiobooks'); await __tick(100);
        const cards = [...document.querySelectorAll('#audiobooksContent .audiobook-card[data-audiobook-id]')].map(card => ({
          id: card.dataset.audiobookId, cover: card.querySelector('.audiobook-cover img')?.getAttribute('src') ?? null,
          identify: card.querySelector('[data-media-action="identify-audio"]')?.dataset.id ?? null}));
        await __click('#audiobooksContent [data-audiobook-id="1"] .audiobook-cover');
        return cards;
        """,
        api={"audiobook_state": {"books": AUDIOBOOKS}},
    )
    assert result.value == [
        {"id": "1", "cover": "covers/alpha.jpg", "identify": "1"},
        {"id": "3", "cover": None, "identify": "3"},
        {"id": "2", "cover": "covers/zeta.jpg", "identify": "2"},
    ]
    assert result.calls_of("open_url") == [["https://anilist.co/manga/101"]]


def test_import_file_keeps_legacy_one_argument_probe_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _audio(tmp_path)
    source = tmp_path / "legacy-probe.mp3"
    source.write_bytes(b"audio")
    calls: list[Path] = []

    def old_probe(path: Path):
        calls.append(path)
        return 42.0, []

    monkeypatch.setattr(service, "_probe", old_probe)
    monkeypatch.setattr(service, "prepare_transcription", lambda *_a, **_k: {"status": "disabled"})
    book = service.import_file(source)
    assert calls == [source.resolve()]
    assert book["duration"] == 42.0
