"""v0.7.00 polish: manga OCR wording and in-place text layer, audiobook controls, the anime
context menu on Home cards, Continue watching (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

import json
from pathlib import Path

from types import SimpleNamespace

from css_support import css_rules
from pudge.audiobooks import AudiobookService
from pudge.database import Database
from pudge.manager_models import LibraryAnime, LibraryEpisode
from test_web_app import make_api

MANGA = r"""
__api.manga_state = async () => ({books: [{id: 3, title: 'Vol 1', series_title: 'Manga', page_count: 20, position: 0, ocr_complete: true}], ocr_available: true});
__api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: 20, name: `p${index}.png`, spread: false, data_uri: 'data:image/png;base64,AAAA'});
__api.manga_text_regions = async (id, index) => ({book_id: id, page_index: index, available: true, cached: true,
  regions: index === 0 ? [{text: '猫だ', x: 0.1, y: 0.1, width: 0.2, height: 0.1}] : []});
"""


def test_manga_ocr_is_a_volume_action_and_its_text_lies_invisibly_over_the_art(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            await __settle(PudgeMangaReaderV2.openBook(3)); await __tick(300);
            const reader = $('mangaReaderV2');
            const visible = [...reader.querySelectorAll('.manga-v2-toolbar [data-manga-v2-action]')].filter(b => b.checkVisibility());
            const region = reader.querySelector('[data-page-index="0"] .manga-v2-text-region .manga-v2-region-content');
            return {actions: visible.map(b => b.dataset.mangaV2Action), text: reader.innerText, region: region && region.textContent};
            """,
            lang=lang,
            before=MANGA,
            state={"settings": {"review_gate_manga_enabled": False}},
        )
        out = result.value
        assert "ocr-book" in out["actions"] and "ocr-page" not in out["actions"]
        for wording in ("Recognize", "Распознать", "OCR cache", "OCR из кэша"):
            assert wording not in out["text"]
        assert out["region"] == "猫だ"  # the recognised text sits on its page
    # The text layer is transparent, lets clicks through and is not selected itself.
    rules = css_rules("manga_reader_v2.css").declarations(".manga-v2-region-content")
    assert (rules["background"], rules["pointer-events"], rules["user-select"]) == (
        "transparent",
        "none",
        "none",
    )


def _audiobook_service(tmp_path: Path, monkeypatch, duration: float) -> AudiobookService:
    db = Database(tmp_path / "db.sqlite3")
    service = AudiobookService(db, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    monkeypatch.setattr(service, "_probe", lambda _path: (duration, []))
    return service


def test_audiobook_seek_and_speed_through_the_window_api(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    monkeypatch.setattr(api.audiobooks, "_probe", lambda _path: (300.0, []))
    monkeypatch.setattr(
        api.audiobooks, "prepare_transcription", lambda *_args, **_kwargs: {"status": "disabled"}
    )
    source = tmp_path / "book.mp3"
    source.write_bytes(b"audio")
    book = api.audiobooks.import_file(source)
    api.audiobooks.set_position(book["id"], 100.0)

    assert api.audiobook_seek(book["id"], -15)["book"]["position"] == 85.0
    assert api.audiobook_set_speed(book["id"], 1.75)["speed"] == 1.75
    assert api.audiobooks.book(book["id"])["speed"] == 1.75


def test_audiobook_play_passes_speed_to_mpv(tmp_path: Path, monkeypatch) -> None:
    service = _audiobook_service(tmp_path, monkeypatch, 60.0)
    source = tmp_path / "book.mp3"
    source.write_bytes(b"audio")
    # Import now starts background STT immediately. Keep this playback unit test
    # deterministic and focused on the mpv command rather than racing STT Popen.
    monkeypatch.setattr(service, "prepare_transcription", lambda _book_id: {"status": "disabled"})
    book = service.import_file(source)
    commands: list[list[str]] = []

    class FakeProcess:
        def poll(self):
            return None

    monkeypatch.setattr(
        "pudge.audiobooks.subprocess.Popen",
        lambda command, **_kwargs: commands.append(command) or FakeProcess(),
    )
    monkeypatch.setattr(
        "pudge.audiobooks.threading.Thread", lambda **_kwargs: SimpleNamespace(start=lambda: None)
    )

    service.play(book["id"], speed=1.5)
    mpv_commands = [command for command in commands if "--no-video" in command]
    assert len(mpv_commands) == 1
    assert any(item == "--speed=1.500" for item in mpv_commands[0])
    assert "--audio-pitch-correction=yes" in mpv_commands[0]


AUDIOBOOK = {
    "id": 5,
    "title": "Long Book",
    "duration": 7200,
    "position": 3725,
    "speed": 1,
    "playing": False,
    "player_running": False,
}
PLAYING = {**AUDIOBOOK, "playing": True, "player_running": True}


def test_audiobook_card_shows_hours_skips_15_s_and_marks_playing_and_import_busy(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('audiobooks', true); await __tick(100);
            const card = () => document.querySelector('.audiobook-card[data-audiobook-id="5"]');
            const out = {summary: __text('[data-audio-live-summary="5"]'), left: PudgeI18n.t('media.timeLeft', {time: '57:55'}),
                         playing: card().classList.contains('playing'), importButton: __visible('#audiobookImport'),
                         folderButton: Boolean($('audiobookImportFolder')), speeds: [...card().querySelectorAll('[data-audio-speed] option')].map(o => o.value)};
            await __click('[data-audiobook-id="5"] [data-media-action="seek-audio"][data-seconds="-15"]'); await __tick(20);
            await __click('[data-audiobook-id="5"] [data-media-action="seek-audio"][data-seconds="15"]'); await __tick(20);
            out.playingAfter = card().classList.contains('playing');      // the seek answer says the player runs
            // One import entry (the page's Import button) offers a file or a folder.
            await __click('#pageImportButton'); await __tick(20);
            out.menu = [...document.querySelectorAll('#audiobookImportMenu [data-audio-import-kind]')].map(b => [b.dataset.audioImportKind, b.textContent]);
            out.menuLabels = [PudgeI18n.t('media.file'), PudgeI18n.t('media.folder')];
            let answer; __api.choose_audiobook_folder = () => new Promise(resolve => { answer = resolve; });
            await __click('#audiobookImportMenu [data-audio-import-kind="folder"]'); await __tick(50);
            out.busy = {status: __text('.audiobook-import-status'), expected: PudgeI18n.t('media.readingAudiobookMetadata')};
            answer({cancelled: true}); await __tick(50);
            out.done = __visible('.audiobook-import-status');
            return out;
            """,
            lang=lang,
            api={
                "audiobook_state": {"books": [AUDIOBOOK]},
                "sidebar_audio_state": {"books": [PLAYING]},
                "audiobook_seek": {"book": PLAYING},
            },
        )
        out = result.value
        assert out["summary"].startswith(f"1:02:05 / 2:00:00 · 52% · {out['left']}")
        assert out["playing"] is False and out["playingAfter"] is True
        assert (
            css_rules("media.css").get(".audiobook-card.playing", "border-color") == "rgba(111,140,255,.55)"
        )  # the playing card stands out
        assert out["importButton"] is False and out["folderButton"] is False
        assert out["speeds"] == ["0.75", "1", "1.25", "1.5", "1.75", "2", "2.5", "3"]
        assert result.calls_of("audiobook_seek") == [[5, -15], [5, 15]]
        assert out["menu"] == [["file", out["menuLabels"][0]], ["folder", out["menuLabels"][1]]]
        assert out["busy"]["status"] == out["busy"]["expected"] and out["done"] is False
        assert result.calls_of("choose_audiobook_folder") == [[]]


def test_audiobooks_page_opened_during_playback_shows_the_library(page) -> None:
    # While a book plays, polls only patch the live fields of cards already drawn; the
    # first visit to the page still draws the whole library.
    result = page.run(
        "setPage('audiobooks', true); await __tick(2000); return Boolean(document.querySelector('.audiobook-card[data-audiobook-id=\"5\"]'));",
        api={"audiobook_state": {"books": [PLAYING]}, "sidebar_audio_state": {"books": [PLAYING]}},
    )
    assert result.value is True


# A Continue watching card for a show that is not in the window's lists (the backend
# knows only the episode file): the card itself carries what the menu needs.
def _continue_item(**extra):
    return {
        "media_id": 77,
        "title": "Show",
        "site_url": "https://anilist.co/anime/77/show",
        "media_status": "RELEASING",
        "episode": 3,
        "video_path": "/lib/Show - 03.mkv",
        "position": 400,
        "duration": 1400,
        "resume_start": 390,
        "progress_percent": 28,
        **extra,
    }


CONTINUE = r"""
setPage('current', true); await __tick(50);
const card = '[data-continue-card="1"] .airing-meta';
const rightClick = async () => {
  await __fire(card, 'pointerdown', {button: 2}, 'PointerEvent');
  await __fire(card, 'pointerup', {button: 2}, 'PointerEvent');
  await __fire(card, 'auxclick', {button: 2}, 'MouseEvent');
  await __fire(card, 'contextmenu', {button: 2, clientX: 20, clientY: 20}, 'MouseEvent');
};
const leftClick = async (init = {}) => {
  await __fire(card, 'pointerdown', {button: 0, ...init}, 'PointerEvent');
  await __fire(card, 'pointerup', {button: 0, ...init}, 'PointerEvent');
  await __click(card, {button: 0, ...init});
  await __tick(50);
};
const plays = () => __apiCalls.filter(call => call.name === 'play').length;
"""


RIGHT_CLICK = (
    CONTINUE
    + r"""
await rightClick();
const out = {open: __visible('#contextMenu'), first: __text('#contextMenu button'), label: t('action.openAniList'), plays: plays()};
await __click('#contextMenu [data-context-action="open"]'); await __tick(10);
return out;
"""
)


def test_right_click_on_a_continue_card_opens_the_anime_menu(page) -> None:
    for gone in (False, True):
        # gone: a state refresh already dropped the show while its card is still on screen;
        # the menu is built from the card itself.
        code = RIGHT_CLICK.replace(
            "await rightClick();",
            "if (GONE) ui.state = {...ui.state, home: {...ui.state.home, continue_watching: []}};\nawait rightClick();",
        )
        result = page.run(
            code.replace("GONE", "true" if gone else "false"),
            state={"home": {"continue_watching": [_continue_item()]}},
        )
        out = result.value
        assert out["open"] is True and out["first"] == out["label"]
        assert out["plays"] == 0  # a right click does not start the episode
        assert result.calls_of("open_url") == [["https://anilist.co/anime/77/show"]]


def test_open_anilist_works_for_a_continue_card_without_a_known_link(page) -> None:
    # The backend sends site_url "" when the episode's show is not in the library; the menu
    # then uses the card's own link, https://anilist.co/anime/<id>.
    result = page.run(RIGHT_CLICK, state={"home": {"continue_watching": [_continue_item(site_url="")]}})
    assert result.value["open"] is True
    assert result.calls_of("open_url") == [["https://anilist.co/anime/77"]]


def test_one_left_click_on_a_continue_card_resumes_once(page) -> None:
    result = page.run(
        CONTINUE
        + r"""
        const out = {};
        await leftClick({ctrlKey: true}); out.ctrl = plays();      // ctrl-click is a right click on a Mac
        await leftClick(); out.once = plays();
        return out;
        """,
        state={"home": {"continue_watching": [_continue_item()]}},
        api={"play": {"ok": True, "status": "starting"}},
    )
    assert result.value == {"ctrl": 0, "once": 1}
    [[path, resume, *_rest]] = result.calls_of("play")
    assert (path, resume) == ("/lib/Show - 03.mkv", True)
    # While the player starts or runs, the card offers its menu instead of a second start.
    rules = css_rules("index.html")
    assert rules.get(".airing-card.play-starting", "cursor") == "context-menu"
    assert rules.get(".airing-card.play-running", "cursor") == "context-menu"


def test_continue_watching_payload_carries_the_anime_link_and_status(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    anime = LibraryAnime(
        media_id=77,
        title="Show",
        status="CURRENT",
        progress=2,
        episodes=12,
        media_status="RELEASING",
        site_url="https://anilist.co/anime/77/show",
    )
    api.manager.db.upsert_anime(anime)
    video = tmp_path / "Show - 03.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(media_id=77, title="Show", episode=3, video_path=video, state="ready")
    )
    api.manager.db.record_playback(video, 400.0, 1400.0)
    orphan = tmp_path / "Unknown - 01.mkv"
    orphan.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(media_id=None, title="Unknown", episode=1, video_path=orphan, state="ready")
    )
    api.manager.db.record_playback(orphan, 300.0, 1400.0)
    items = {
        item["title"]: item
        for item in json.loads(json.dumps(api.get_state()["home"]["continue_watching"], default=str))
    }
    assert (items["Show"]["site_url"], items["Show"]["media_status"]) == (
        "https://anilist.co/anime/77/show",
        "RELEASING",
    )
    assert (items["Unknown"]["site_url"], items["Unknown"]["media_status"]) == ("", "")
