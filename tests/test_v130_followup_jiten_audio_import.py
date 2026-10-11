from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from pudge.audiobooks import AudiobookService
from pudge.database import Database
from tests.css_support import css_rules


def _service(tmp_path: Path) -> AudiobookService:
    return AudiobookService(
        Database(tmp_path / "library.sqlite3"),
        ffprobe="ffprobe",
        mpv="mpv",
        ffmpeg="ffmpeg",
        cache_dir=tmp_path / "cache",
        cover_cache_dir=tmp_path / "covers",
    )


def test_study_card_text_is_selectable_and_its_menus_open_above_it() -> None:
    card = css_rules("reading_tools.css")
    # The word card's deck menu (a Pudge select) must open above the card.
    menu_layer = int(css_rules("pudge_select.css").get(".pudge-select-menu", "z-index"))
    assert menu_layer > int(card.get(".pudge-study-card", "z-index"))
    # Text in the card can be selected (copied) although the app disables selection elsewhere;
    # its buttons and selects cannot.
    for selector in (".pudge-study-card", ".pudge-study-card *"):
        assert card.get(selector, "user-select") == "text!important"
        assert card.get(selector, "-webkit-user-select") == "text!important"
    for selector in (".pudge-study-card button", ".pudge-study-card select"):
        assert card.get(selector, "user-select") == "none!important"


# The open preview shows an 800×1200 image, larger than the window.
PREVIEW = r"""
PudgeCoverPreview.openSource('covers/big.jpg');
await __tick(50);
const image = document.querySelector('.pudge-cover-preview img');
image.__rect = {x: 250, y: -200, width: 800, height: 1200};
const state = () => {
  const m = /translate3d\((-?[\d.]+)px,\s*(-?[\d.]+)px,\s*0\) scale\(([\d.]+)\)/.exec(image.style.transform);
  return m ? {x: Number(m[1]), y: Number(m[2]), zoom: Number(m[3]), classes: image.className} : {transform: image.style.transform};
};
const wheel = init => __fire('.pudge-cover-preview img', 'wheel', init, 'WheelEvent');
const pointer = (type, init) => __fire('.pudge-cover-preview img', type, {pointerId: 5, pointerType: 'mouse', ...init}, 'PointerEvent');
"""


def test_cover_preview_zooms_at_the_pointer_and_pans_once_enlarged(page) -> None:
    result = page.run(
        PREVIEW
        + r"""
        await wheel({deltaX: 30, deltaY: 40});                   // not enlarged: scrolling does not move it
        const fitted = state();
        await wheel({ctrlKey: true, deltaY: -100, clientX: innerWidth / 2 + 200, clientY: innerHeight / 2});   // pinch zoom 200 px right of center
        const zoomed = state();
        await wheel({deltaX: 30, deltaY: 40});                   // two-finger scroll pans
        const scrolled = state();
        await pointer('pointerdown', {button: 0, clientX: 500, clientY: 300});
        const grabbing = state().classes;
        await pointer('pointermove', {clientX: 560, clientY: 320});
        const dragged = state();
        await pointer('pointerup', {clientX: 560, clientY: 320});
        return {fitted, zoomed, scrolled, grabbing, dragged, released: state().classes};
        """,
    )
    out = result.value
    assert (out["fitted"]["x"], out["fitted"]["y"], out["fitted"]["zoom"]) == (0, 0, 1)
    zoom = out["zoomed"]["zoom"]
    assert round(zoom, 3) == 2.718  # e^1 for one wheel notch of 100
    # The image point under the pointer stays under it: 200 px right of center before and after.
    assert round(out["zoomed"]["x"] + 200 * zoom, 3) == 200 and out["zoomed"]["y"] == 0
    assert "zoomed" in out["zoomed"]["classes"]
    assert (
        round(out["scrolled"]["x"] - out["zoomed"]["x"], 3),
        round(out["scrolled"]["y"] - out["zoomed"]["y"], 3),
    ) == (-30, -40)
    assert "panning" in out["grabbing"]
    assert (
        round(out["dragged"]["x"] - out["scrolled"]["x"], 3),
        round(out["dragged"]["y"] - out["scrolled"]["y"], 3),
    ) == (60, 20)
    assert "panning" not in out["released"]
    preview = css_rules("cover_preview.css")
    assert preview.get(".pudge-cover-preview img", "touch-action") == "none"
    assert preview.get(".pudge-cover-preview img.zoomed", "cursor") == "grab"
    assert preview.get(".pudge-cover-preview img.panning", "cursor") == "grabbing"


# A word of the reader whose book has an aligned audiobook; the word starts at character 10.
WORD_CARD = r"""
const reader = $('lnReader');
reader.innerHTML = '<p data-ln-paragraph="0">' + ['一', '二', '三'].map((w, i) =>
  `<span class="ln-word" data-ln-token="${i}" data-ln-audio-start="${i * 10}">${w}</span>`).join('') + '</p>';
ui.lnBook = {id: 4, title: 'Novel', paired_audio: {audiobook_id: 9, alignment: {ready: true}}};
ui.lnChapter = {chapter_index: 2};
ui.lnTokenMap = new Map([['1', {surface: '二', card: {spelling: '二', reading: 'に'}, sentence: '一二三'}]]);
await __settle(showLnPopup('1', reader.querySelector('[data-ln-token="1"]')));
const actions = [...document.querySelectorAll('#pudgeStudyCard .pudge-study-header-action')];
"""


def test_play_from_here_is_green_left_of_read_up_to_here_and_plays_from_the_word(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            WORD_CARD
            + r"""
            const out = {actions: actions.map(b => [b.innerText, b.dataset.pudgeStudyActionTone || '']),
                         texts: [t('ui.playFromHere'), t('ui.readUpToHere')]};
            actions[0].click(); await __tick(50);
            return out;
            """,
            lang=lang,
            api={
                "light_novel_play_paired_at_offset": {
                    "audiobook_id": 9,
                    "playing": True,
                    "paused": False,
                    "position": 3,
                }
            },
        )
        play, read = result.value["texts"]
        assert result.value["actions"] == [[play, "listen"], [read, ""]]
        [[book, chapter, offset, _speed]] = result.calls_of("light_novel_play_paired_at_offset")
        assert (book, chapter, offset) == (4, 2, 10)
    listen = css_rules("reading_tools.css").declarations(
        '.pudge-study-header-action[data-pudge-study-action-tone="listen"]'
    )
    assert (listen["background"], listen["border-color"]) == ("#174b31", "#22a65a")


def test_first_listen_starts_from_the_reader_not_from_a_stale_paused_player(page) -> None:
    # A paused player from an earlier session is still running: the first Listen starts at the
    # reader's position; later Pause/Listen only pause and resume that player.
    result = page.run(
        r"""
        ui.lnBook = {id: 4, title: 'Novel', paired_audio: {audiobook_id: 9, alignment: {ready: true}}};
        ui.lnChapter = {chapter_index: 0};
        const paused = {player_running: true, audiobook_id: 9, playing: false, paused: true, alignment: {ready: true}, position: 100, ln_chapter_index: 0};
        let live = paused;
        ui.lnPairedState = paused;
        __api.light_novel_paired_state = async () => live;
        __api.light_novel_play_paired = async () => (live = {...paused, playing: true, paused: false, position: 20});
        __api.audiobook_set_paused = async (_id, pause) => { live = {...live, playing: !pause, paused: pause}; return {ok: true}; };
        const transport = () => __apiCalls.filter(c => ['light_novel_play_paired', 'audiobook_set_paused'].includes(c.name)).map(c => [c.name, ...c.args]);
        void toggleLnPairedPlayback(); await __tick(500);
        const first = transport();
        void toggleLnPairedPlayback(); await __tick(500);
        void toggleLnPairedPlayback(); await __tick(500);
        return {first, all: transport()};
        """,
    )
    first, every = result.value["first"], result.value["all"]
    assert [row[:3] for row in first] == [["light_novel_play_paired", 4, 0]]
    assert every[1:] == [["audiobook_set_paused", 9, True], ["audiobook_set_paused", 9, False]]


def test_play_from_here_resumes_a_paused_player_after_seeking(tmp_path: Path, monkeypatch) -> None:
    import pudge.audiobooks as audiobooks

    service = _service(tmp_path)
    calls: list[tuple] = []
    monkeypatch.setattr(service, "link_for_light_novel", lambda _book: {"book": {"id": 9}})
    monkeypatch.setattr(service, "_load_alignment", lambda *_args: {"chapters": []})
    monkeypatch.setattr(
        audiobooks, "audio_position_for_light_novel_offset", lambda _alignment, chapter, offset: 42.0
    )
    monkeypatch.setattr(service, "book", lambda _id: {"speed": 1.0})
    monkeypatch.setattr(service, "is_playing", lambda _id: True)
    monkeypatch.setattr(service, "seek_to", lambda book, position: calls.append(("seek", book, position)))
    monkeypatch.setattr(service, "set_speed", lambda book, speed: calls.append(("speed", book, speed)))
    monkeypatch.setattr(service, "set_paused", lambda book, paused: calls.append(("paused", book, paused)))
    monkeypatch.setattr(service, "_reader_audio_started", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "paired_state", lambda _book: {"playing": True})

    assert service.play_paired_at_offset(4, 2, 10, speed=1.25) == {"playing": True}
    assert calls == [("seek", 9, 42.0), ("speed", 9, 1.25), ("paused", 9, False)]


def test_folder_import_preserves_embedded_m4b_chapters_with_global_offsets(
    tmp_path: Path, monkeypatch
) -> None:
    service = _service(tmp_path)
    folder = tmp_path / "Book"
    folder.mkdir()
    first = folder / "01.m4b"
    second = folder / "02.m4b"
    first.write_bytes(b"a")
    second.write_bytes(b"b")

    def probe(path: Path):
        if path.name == "01.m4b":
            return 30.0, [
                {"index": 0, "title": "Intro", "start": 0.0, "end": 10.0},
                {"index": 1, "title": "One", "start": 10.0, "end": 30.0},
            ]
        return 20.0, [
            {"index": 0, "title": "Two", "start": 0.0, "end": 8.0},
            {"index": 1, "title": "Three", "start": 8.0, "end": 20.0},
        ]

    monkeypatch.setattr(service, "_probe", probe)
    monkeypatch.setattr(service, "_queue_embedded_cover", lambda _path: None)
    book = service.import_folder(folder, auto_link=False, prepare_transcription=False)

    assert [row["title"] for row in book["chapters"]] == ["Intro", "One", "Two", "Three"]
    assert [row["start"] for row in book["chapters"]] == [0.0, 10.0, 30.0, 38.0]
    assert [row["end"] for row in book["chapters"]] == [10.0, 30.0, 38.0, 50.0]


def test_embedded_m4b_cover_is_extracted_into_shared_cover_cache(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    source = tmp_path / "book.m4b"
    source.write_bytes(b"audio")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append([str(value) for value in command])
        target = Path(command[-1])
        target.write_bytes(b"jpeg")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    url = service._embedded_cover_url(source)

    assert url.startswith("covers/audiobook-") and url.endswith(".jpg")
    assert (tmp_path / "covers" / Path(url).name).read_bytes() == b"jpeg"
    assert "-map" in calls[0] and "0:v:0" in calls[0]
    assert "-frames:v" in calls[0] and "1" in calls[0]


def test_transcription_queue_is_bounded_and_reports_positions(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "_ensure_transcription_dispatcher", lambda: None)
    ids = []
    for index in range(2):
        source = tmp_path / f"book-{index}.mp3"
        source.write_bytes(b"audio")
        book = service._upsert(
            path=source,
            title=f"Book {index}",
            duration=100.0,
            files=[
                {
                    "index": 0,
                    "path": str(source),
                    "title": source.stem,
                    "duration": 100.0,
                    "start": 0.0,
                    "end": 100.0,
                }
            ],
            chapters=[{"index": 0, "title": source.stem, "start": 0.0, "end": 100.0}],
        )
        ids.append(int(book["id"]))

    service.prepare_transcription(ids[0])
    service.prepare_transcription(ids[1])

    first = service.transcription_status(ids[0])
    second = service.transcription_status(ids[1])
    assert first["status"] == second["status"] == "queued"
    assert first["queue_position"] == 1 and second["queue_position"] == 2
    assert first["queue_size"] == second["queue_size"] == 2


def test_transcriptions_share_one_dispatcher_thread(tmp_path: Path, monkeypatch) -> None:
    # One worker serves the whole queue (no thread per audiobook).
    service = _service(tmp_path)
    started: list[str] = []

    class _Thread:
        def start(self) -> None:
            pass

        def is_alive(self) -> bool:
            return True

    monkeypatch.setattr(
        service, "_tracked_thread", lambda *, target, name, args=(): started.append(name) or _Thread()
    )
    for index in range(3):
        source = tmp_path / f"queued-{index}.mp3"
        source.write_bytes(b"audio")
        book = service._upsert(
            path=source,
            title=source.stem,
            duration=10.0,
            files=[
                {
                    "index": 0,
                    "path": str(source),
                    "title": source.stem,
                    "duration": 10.0,
                    "start": 0.0,
                    "end": 10.0,
                }
            ],
            chapters=[{"index": 0, "title": source.stem, "start": 0.0, "end": 10.0}],
        )
        service.prepare_transcription(int(book["id"]))
    assert started == ["audiobook-stt-dispatcher"]


def test_audiobook_bulk_delete_api_and_ui_exist(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(service, "stop", lambda _book_id: {"ok": True})
    ids = []
    for index in range(2):
        source = tmp_path / f"bulk-{index}.mp3"
        source.write_bytes(b"audio")
        book = service._upsert(
            path=source,
            title=source.stem,
            duration=1.0,
            files=[
                {
                    "index": 0,
                    "path": str(source),
                    "title": source.stem,
                    "duration": 1.0,
                    "start": 0.0,
                    "end": 1.0,
                }
            ],
            chapters=[{"index": 0, "title": source.stem, "start": 0.0, "end": 1.0}],
        )
        ids.append(int(book["id"]))

    result = service.delete_many(ids)
    assert result == {"ok": True, "removed": ids, "errors": []}
    assert service.state()["books"] == []


def test_selected_audiobooks_are_deleted_in_one_request(page, tmp_path: Path, monkeypatch) -> None:
    books = [
        {"id": i, "title": f"Book {i}", "duration": 100.0, "position": 0.0, "chapters": []} for i in (1, 2, 3)
    ]
    result = page.run(
        r"""
        setPage('audiobooks'); await __tick(50);
        await __click('#audiobooksContent .audiobook-card[data-audiobook-id="1"] .audiobook-title-row');
        await __click('#audiobooksContent .audiobook-card[data-audiobook-id="3"] .audiobook-title-row');
        const count = __text('#pageSelectionActions .selection-count');
        await __click('#pageSelectionActions [data-selection-action=delete]');
        const asked = {open: PudgeConfirm.isOpen(), message: __text('#pudgeConfirmMessage'),
                       expected: t('ui.removeSelectedFromPudgeSource', {count: 2})};
        await __click('#pudgeConfirmBackdrop [data-pudge-confirm=ok]'); await __tick(50);
        return {count, asked, left: [...document.querySelectorAll('#audiobooksContent .audiobook-card')].map(c => c.dataset.audiobookId),
                actions: __visible('#pageSelectionActions')};
        """,
        api={
            "audiobook_state": {"books": books},
            "audiobook_delete_many": {"ok": True, "removed": [1, 3], "errors": []},
        },
    )
    out = result.value
    assert out["count"] == "2"
    assert out["asked"]["open"] is True and out["asked"]["message"] == out["asked"]["expected"]
    assert result.calls_of("audiobook_delete_many") == [[[1, 3]]]
    assert out["left"] == ["2"] and out["actions"] is False

    # The backend removes them from the library and keeps the files.
    from test_web_app import make_api

    api = make_api(tmp_path / "app")
    removed: list[tuple] = []
    monkeypatch.setattr(
        api.audiobooks,
        "delete_many",
        lambda ids, delete_files=True: (
            removed.append((ids, delete_files)) or {"ok": True, "removed": ids, "errors": []}
        ),
    )
    assert api.audiobook_delete_many(["1", 3]) == {"ok": True, "removed": [1, 3], "errors": []}
    assert removed == [([1, 3], False)]
