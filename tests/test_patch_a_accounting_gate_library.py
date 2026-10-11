"""Audit decisions, patch A: accounting, review gate and library metadata."""

from __future__ import annotations

import json
import logging
import threading
import time
import zipfile
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import library_overrides
from pudge.audiobooks import AudiobookService, _audiobook_title_match_score
from pudge.config import AppConfig
from pudge.consumption import ConsumptionLedger
from pudge.consumption_statistics import ConsumptionStatistics, _local_day, _local_midnight_epoch
from pudge.database import Database
from pudge.light_novels import LightNovelService, _series_title, _volume_from_text
from pudge.manga import MangaService, _manga_volume
from pudge.review_gate import EpisodeReviewIdentity, ReviewGateStore
from tests.i18n_support import en, ru


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


def _audiobook(
    service: AudiobookService, path: Path, monkeypatch: pytest.MonkeyPatch, *, duration: float = 100.0
) -> dict:
    path.write_bytes(b"audio")
    monkeypatch.setattr(service, "_queue_embedded_cover", lambda *_a, **_k: None)
    monkeypatch.setattr(service, "prepare_transcription", lambda *_a, **_k: {"status": "disabled"})
    monkeypatch.setattr(service, "prepare_alignment", lambda *_a, **_k: {"status": "disabled"})
    return service._upsert(
        path=path,
        title=path.stem,
        duration=duration,
        files=[
            {
                "index": 0,
                "path": str(path),
                "title": path.stem,
                "duration": duration,
                "start": 0.0,
                "end": duration,
            }
        ],
        chapters=[{"index": 0, "title": path.stem, "start": 0.0, "end": duration}],
    )


def _insert_ln(
    service: LightNovelService,
    tmp_path: Path,
    title: str,
    *,
    volume: int = 1,
    chapters: tuple[str, ...] = ("本文",),
    anilist_id: int | None = None,
) -> int:
    now = time.time()
    conn = service._connect()
    try:
        conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,volume,anilist_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (title, str(tmp_path / f"{title}-{volume}-{now}.epub"), "epub", volume, anilist_id, now, now),
        )
        book_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for index, text in enumerate(chapters):
            conn.execute(
                "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
                (book_id, index, f"c{index}", text, f"h{book_id}-{index}"),
            )
        conn.commit()
    finally:
        conn.close()
    return book_id


# ---------------------------------------------------------------- 1. audiobook finished


def test_seeking_back_keeps_audiobook_finished_and_unfinish_keeps_position(tmp_path, monkeypatch):
    service = _audio(tmp_path)
    book = _audiobook(service, tmp_path / "本.m4b", monkeypatch)
    service.set_position(int(book["id"]), 99.0)
    assert service.book(int(book["id"]))["finished"] is True
    service.set_position(int(book["id"]), 5.0)
    assert service.book(int(book["id"]))["finished"] is True
    service.mark_finished(int(book["id"]), False)
    after = service.book(int(book["id"]))
    assert after["finished"] is False and after["position"] == pytest.approx(5.0)


def test_unfinish_at_the_very_end_rewinds_and_asks_first(tmp_path, monkeypatch, page):
    assert (
        en("media.markUnfinished") == "Mark unfinished" and ru("media.markUnfinished") == "Снять «прослушано»"
    )
    service = _audio(tmp_path)
    finished = _audiobook(service, tmp_path / "本.m4b", monkeypatch)
    other = _audiobook(service, tmp_path / "別.m4b", monkeypatch)
    service.set_position(int(finished["id"]), 99.5)
    state = json.loads(json.dumps(service.state(queue_missing=False), default=str))
    # At the very end, clearing the mark goes back to the start (the end would count as finished again).
    after = service.mark_finished(int(finished["id"]), False)["book"]
    assert after["finished"] is False and after["position"] == 0.0

    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('audiobooks');
            await __tick(100);
            const button = id => document.querySelector(`[data-audiobook-id="${id}"] [data-media-action="finish-audio"]`);
            const out = {labels: [button(FINISHED).innerText, button(OTHER).innerText],
                         expected: [PudgeI18n.t('media.markUnfinished'), PudgeI18n.t('media.finish')]};
            await __click(`[data-audiobook-id="${FINISHED}"] [data-media-action="finish-audio"]`);
            await __tick(10);
            out.question = __text('#pudgeConfirmMessage') === PudgeI18n.t('media.markThisAudiobookUnfinishedThe');
            await __click('#pudgeConfirmBackdrop [data-pudge-confirm=cancel]');
            await __tick(10);
            out.afterCancel = __apiCalls.filter(c => c.name === 'audiobook_mark_finished').length;
            await __click(`[data-audiobook-id="${FINISHED}"] [data-media-action="finish-audio"]`);
            await __tick(10);
            await __click('#pudgeConfirmBackdrop [data-pudge-confirm=ok]');
            await __tick(10);
            await __click(`[data-audiobook-id="${OTHER}"] [data-media-action="finish-audio"]`);   // finishing asks nothing
            await __tick(10);
            return out;
            """.replace("FINISHED", str(finished["id"])).replace("OTHER", str(other["id"])),
            lang=lang,
            api={"audiobook_state": state, "audiobook_mark_finished": {"ok": True}},
        )
        out = result.value
        assert out["labels"] == out["expected"]
        assert out["question"] is True and out["afterCancel"] == 0
        assert result.calls_of("audiobook_mark_finished") == [[finished["id"], False], [other["id"], True]]


# ------------------------------------------------------- 2. short sessions on a boundary


def test_session_crossing_the_period_start_is_judged_by_its_whole_length(tmp_path):
    db = Database(tmp_path / "stats.sqlite3")
    ledger = ConsumptionLedger(db)
    media = ledger.ensure_media(
        kind="manga",
        title="Boundary",
        aliases=[("stable", "boundary:1")],
        source_revision="r",
        metadata={"page_count": 10},
    )
    day = _local_day(1_700_000_000.0)
    boundary = _local_midnight_epoch(day)
    start = boundary - 100.0
    session = ledger.start_session(
        kind="manga",
        media_uuid=media.media_uuid,
        measurement_method="reader_visible_heartbeat",
        started_at_utc=start,
    )
    offset = 0.0
    while offset < 130.0:
        chunk = min(10.0, 130.0 - offset)
        ledger.append_interval(
            session_id=session,
            kind="manga",
            interval_start_utc=start + offset,
            interval_end_utc=start + offset + chunk,
            elapsed_monotonic_ms=chunk * 1000.0,
            media=[
                {
                    "media_uuid": media.media_uuid,
                    "source_revision": "r",
                    "role": "primary",
                    "locator_start": {"page_id": "p1", "page_index": 0},
                    "locator_end": {"page_id": "p1", "page_index": 0},
                }
            ],
            payload={"page_count": 10},
        )
        offset += chunk
    ledger.end_session(session, ended_at_utc=start + 130.0)
    stats = ConsumptionStatistics(db)
    scope = {"period": "custom", "start_date": day.isoformat(), "end_date": day.isoformat()}
    inside = stats.query(scope, now=boundary + 3600)
    # 30 s fall inside the day; the whole session lasted 130 s, so it counts.
    assert inside["summary"]["total_seconds"] == pytest.approx(30.0, abs=1.0)
    assert inside["summary"]["sessions"] == 1
    previous = (day - timedelta(days=1)).isoformat()
    before = stats.query(
        {"period": "custom", "start_date": previous, "end_date": previous}, now=boundary + 3600
    )
    assert before["summary"]["total_seconds"] == pytest.approx(100.0, abs=1.0)


# ------------------------------------------------------------ 3. alias resolution


def test_two_books_sharing_an_anilist_volume_stay_separate(tmp_path):
    ledger = ConsumptionLedger(Database(tmp_path / "alias.sqlite3"))
    first = ledger.ensure_media(
        kind="light_novel",
        title="本 1",
        aliases=[("content", "sha:a"), ("external", "anilist:1:v1")],
        current_library_kind="light_novel",
        current_library_id="1",
    )
    second = ledger.ensure_media(
        kind="light_novel",
        title="本 1 (別版)",
        aliases=[("content", "sha:b"), ("external", "anilist:1:v1")],
        current_library_kind="light_novel",
        current_library_id="2",
    )
    assert first.media_uuid != second.media_uuid
    # Re-importing the first book (new file, same library row) finds its history.
    again = ledger.ensure_media(
        kind="light_novel",
        title="本 1",
        aliases=[("content", "sha:c"), ("external", "anilist:1:v1")],
        current_library_kind="light_novel",
        current_library_id="1",
    )
    assert again.media_uuid == first.media_uuid


def test_conflicting_own_aliases_pick_the_strongest_instead_of_failing(tmp_path):
    ledger = ConsumptionLedger(Database(tmp_path / "alias2.sqlite3"))
    by_content = ledger.ensure_media(kind="manga", title="A", aliases=[("content", "sha:x")])
    by_path = ledger.ensure_media(kind="manga", title="B", aliases=[("path", "/m/b.cbz")])
    merged = ledger.ensure_media(
        kind="manga", title="A", aliases=[("content", "sha:x"), ("path", "/m/b.cbz")]
    )
    assert merged.media_uuid == by_content.media_uuid != by_path.media_uuid


# ------------------------------------------------------------- 7/8. review gate settle


def test_settle_counts_cards_no_longer_due_and_reissues_unknown_still_due(tmp_path):
    store = ReviewGateStore(Database(tmp_path / "gate.sqlite3"))
    identity = EpisodeReviewIdentity(media_id=5, episode=2)
    store.mark_confirmed("acct", identity, required=3, card_key="a")
    store.mark_unknown("acct", identity, required=3, card_key="b")
    store.mark_unknown("acct", identity, required=3, card_key="c")
    # "b" was reviewed (not due any more); "c" is still due and is re-issued.
    progress = store.settle("acct", identity, required=3, due_keys={"c"})
    assert progress.confirmed_card_keys == ("a", "b")
    assert progress.unknown_card_keys == () and progress.granted is False
    # A card reviewed directly in Jiten drops out of the due list and completes the part.
    progress = store.settle("acct", identity, required=3, due_keys=set(), issued_keys={"a", "b", "c"})
    assert progress.granted is True


def test_episode_review_gate_lets_playback_through_when_nothing_can_be_issued(tmp_path, monkeypatch, caplog):
    import pudge.web_app as web_app_module
    from test_review_gate_episode_due import _FakeDb, _FakeLightNovels

    from pudge.web_app import WebAppApi as WebApi

    video, subtitle = tmp_path / "ep.mkv", tmp_path / "ep.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n猫。\n", encoding="utf-8")
    api = WebApi.__new__(WebApi)
    api.config = SimpleNamespace(
        ui=SimpleNamespace(
            review_gate_enabled=True, review_gate_count=5, review_gate_all_due_episode_words=True
        ),
        paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
        tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    api.manager = SimpleNamespace(db=_FakeDb(video, subtitle))
    api.light_novels = _FakeLightNovels()
    api.logger = logging.getLogger("patch-a-gate")

    def jiten_down(*_args, **_kwargs):
        raise RuntimeError("Jiten is down")

    monkeypatch.setattr(web_app_module, "build_episode_due_review_cards", jiten_down)
    with caplog.at_level(logging.WARNING, logger="patch-a-gate"):
        result = api.review_gate_begin(str(video))
    assert (result["blocking"], result["granted"], result["reason"]) == (False, False, "review_unavailable")
    assert "reason=review_unavailable" in caplog.text


def test_episode_plays_once_after_the_gate_could_not_issue_a_review(tmp_path, monkeypatch):
    # The window launches the episode after review_gate_begin answered review_unavailable;
    # play() lets that one launch through instead of asking for the review again.
    import pudge.web_app as web_app_module
    from test_review_gate_episode_due import _FakeDb, _FakeLightNovels

    from pudge.web_app import WebAppApi as WebApi

    video, subtitle = tmp_path / "ep.mkv", tmp_path / "ep.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n猫。\n", encoding="utf-8")
    api = WebApi.__new__(WebApi)
    api.config = SimpleNamespace(
        ui=SimpleNamespace(
            review_gate_enabled=True, review_gate_count=5, review_gate_all_due_episode_words=True
        ),
        paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
        tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    api.manager = SimpleNamespace(db=_FakeDb(video, subtitle))
    api.manager.db.episode.state, api.manager.db.episode.subtitle_origin = "couldnt_sync", None
    api.light_novels = _FakeLightNovels()
    api.logger = logging.getLogger("patch-a-gate")
    api._play_lock = threading.Lock()
    api._play_state_locked = lambda _key: {"status": "idle"}
    api._personal_schedule_play_admission = lambda _path: {"allowed": True}

    class Launched(Exception):
        pass

    def jiten_down(*_args, **_kwargs):
        raise RuntimeError("Jiten is down")

    def launch(*_args, **_kwargs):
        raise Launched()

    monkeypatch.setattr(web_app_module, "build_episode_due_review_cards", jiten_down)
    monkeypatch.setattr(web_app_module, "resolve_episode_subtitle", launch)
    assert api.play(str(video))["review_gate_required"] is True
    assert api.review_gate_begin(str(video))["reason"] == "review_unavailable"
    with pytest.raises(Launched):
        api.play(str(video))
    assert api.play(str(video))["review_gate_required"] is True  # only that one launch


def test_review_gate_proceeds_when_nothing_can_be_issued(page):
    # Watch → the gate opens (the status says a review is due) → nothing can be issued (Jiten
    # down) → the gate closes and the episode plays.
    path = "/library/Show - 03.mkv"
    result = page.run(
        f"""
        await startPlay({json.dumps(path)});
        await __tick(500);
        return {{gateOpen: Boolean(window.PudgeReviewGate?.isOpen?.())}};
        """,
        api={
            "review_gate_status": {
                "supported": True,
                "blocking": True,
                "granted": False,
                "required": 1,
                "completed": 0,
                "remaining": 1,
                "reason": "episode_due_scan_required",
            },
            "review_gate_begin": {
                "supported": True,
                "blocking": False,
                "granted": False,
                "reason": "review_unavailable",
                "cards": [],
                "message": "Jiten is down",
            },
            "play": {"ok": True},
        },
    )
    assert result.calls_of("review_gate_begin") == [[path]]
    assert result.value["gateOpen"] is False
    assert [call[0] for call in result.calls_of("play")] == [path]


def test_audiobook_chapter_boundary_waits_only_for_a_review_that_can_be_given():
    from pudge.web_app import WebAppApi as WebApi

    statuses = {
        "unavailable": {"granted": False, "blocking": False, "reason": "review_unavailable"},
        "granted": {"granted": True, "blocking": False},
        "blocking": {"granted": False, "blocking": True},
        "unknown": {"granted": False},  # no answer about blocking: keep waiting
    }
    results = {}
    for name, status in statuses.items():
        api = WebApi.__new__(WebApi)
        api.audiobooks = SimpleNamespace(
            _lock=threading.Lock(),
            _review_contexts={7: {"pending_chapter": 3, "ln_book_id": 5, "session": 1}},
        )
        api._content_reviews = lambda status=status: SimpleNamespace(begin=lambda kind, book, chapter: status)
        results[name] = (
            api.content_review_boundary_done(7, 3)["ok"],
            api.audiobooks._review_contexts[7]["pending_chapter"],
        )
    assert results == {
        "unavailable": (True, None),
        "granted": (True, None),
        "blocking": (False, 3),
        "unknown": (False, 3),
    }


def test_content_review_without_jiten_does_not_block(tmp_path):
    from pudge.content_review import ContentReviewService

    ui = SimpleNamespace(review_gate_ln_enabled=True)
    novels = SimpleNamespace(settings=lambda: SimpleNamespace(study_backend="jiten", jiten_api_key=""))
    service = ContentReviewService.__new__(ContentReviewService)
    service.api = SimpleNamespace(config=SimpleNamespace(ui=ui), light_novels=novels)
    result = service.begin("ln", 1, 0)
    assert (
        result["blocking"] is False
        and result["granted"] is False
        and result["reason"] == "review_unavailable"
    )


# ------------------------------------------------------------- 9. manga without OCR

MANGA = """
const settle = async promise => {
  let done = false;
  promise.then(() => { done = true; }, () => { done = true; });
  for (let k = 0; k < 200 && !done; k++) await __tick(10);
};
await settle(PudgeMangaReaderV2.openBook(3));
const opened = $('mangaReaderV2').classList.contains('open');
await __click('#mangaReaderV2 [data-manga-v2-action=next]');           // page 20: the next 20-page part
await __tick(200);
return {opened, page: __text('#mangaReaderV2').includes('21 / 40')};
"""


def _manga(ocr: bool) -> str:
    return (
        "__api.manga_state = async () => ({books: [{id: 3, title: 'Vol 1', series_title: 'Manga', page_count: 40, position: 19}], "
        f"ocr_available: {'true' if ocr else 'false'}}});"
        "__api.manga_page = async (id, index) => ({book_id: id, page_index: index, page_count: 40, name: `p${index}.png`,"
        " spread: false, data_uri: 'data:image/png;base64,AAAA'});"
    )


def test_manga_without_ocr_opens_without_review_gate(page):
    # Manga reviews need OCR text: without an OCR backend nothing could be asked, so the
    # book and its next parts open directly.
    without = page.run(MANGA, before=_manga(False))
    assert without.value == {"opened": True, "page": True}
    assert not without.called("content_review_begin")
    with_ocr = page.run(MANGA, before=_manga(True))
    assert with_ocr.value == {"opened": True, "page": True}
    assert with_ocr.calls_of("content_review_begin") == [["manga", 3, 0], ["manga", 3, 20]]


# ------------------------------------------------------- 11. volume parser + overrides


@pytest.mark.parametrize(
    ("title", "volume", "series"),
    [
        ("俺の妹2020", None, "俺の妹2020"),
        ("タイトル 第十二巻", 12, "タイトル"),
        ("タイトル 二十三巻", 23, "タイトル"),
        ("タイトル（上）", 1, "タイトル"),
        ("タイトル（下）(MF文庫J)", 2, "タイトル"),
        ("タイトル 中", 2, "タイトル"),
        ("天上", None, "天上"),
        ("86", None, "86"),
    ],
)
def test_light_novel_volume_parser(title, volume, series):
    assert _volume_from_text(title) == volume
    assert _series_title(title) == series


def test_manga_volume_parser_reads_kanji_numerals():
    assert _manga_volume("ワンピース 第百二巻") == 102


def test_split_volumes_get_three_when_the_series_has_a_middle_part(tmp_path):
    service = _ln(tmp_path)
    upper = _insert_ln(service, tmp_path, "物語（上）", volume=1)
    middle = _insert_ln(service, tmp_path, "物語（中）", volume=1)
    lower = _insert_ln(service, tmp_path, "物語（下）", volume=1)
    volumes = {int(b["id"]): int(b["volume"]) for b in service.books()}
    assert (volumes[upper], volumes[middle], volumes[lower]) == (1, 2, 3)


def _epub(path: Path, title: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        zf.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f'<dc:title>{title}</dc:title></metadata><manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="c1"/></spine></package>',
        )
        zf.writestr("OEBPS/c1.xhtml", f"<html><body><p>{title}の本文です。</p></body></html>")
    return path


def test_volume_found_in_the_epub_title_beats_the_folder_name(tmp_path):
    # The file name and the EPUB title describe this book; the folder may name a whole series.
    service = _ln(tmp_path)
    folder = tmp_path / "import" / "物語 第5巻"
    by_title = service.import_file(_epub(folder / "book.epub", "物語 第3巻"))
    by_file = service.import_file(_epub(folder / "物語 2.epub", "物語 第3巻"))
    by_folder = service.import_file(_epub(folder / "other.epub", "別の物語"))
    assert (by_title["volume"], by_file["volume"], by_folder["volume"]) == (3, 2, 5)


def test_manual_ln_volume_survives_the_repair_and_series_joins_an_anilist_shelf(tmp_path):
    service = _ln(tmp_path)
    main_1 = _insert_ln(service, tmp_path, "本好きの下剋上 1", volume=1, anilist_id=777)
    main_2 = _insert_ln(service, tmp_path, "本好きの下剋上 2", volume=2, anilist_id=777)
    stray = _insert_ln(service, tmp_path, "Honzuki vol 1", volume=1)
    book = service.set_metadata_override(stray, volume=3, series_title="本好きの下剋上")
    assert book["volume"] == 3 and book["manual_metadata"] is True
    service._volume_repair_due = True
    books = {int(b["id"]): b for b in service.books()}
    assert books[stray]["volume"] == 3
    assert (
        books[stray]["series_key"]
        == books[main_1]["series_key"]
        == books[main_2]["series_key"]
        == "anilist:777"
    )
    assert books[stray]["manual_volume"] == 3 and books[stray]["manual_series_title"] == "本好きの下剋上"
    assert service.book(stray)["series_key"] == "anilist:777"
    # Both fields empty removes the correction.
    service.set_metadata_override(stray, volume=None, series_title="")
    assert service.book(stray).get("manual_metadata") is None
    with pytest.raises(ValueError):
        service.set_metadata_override(stray, volume=0, series_title="")


def test_manual_manga_and_audiobook_metadata(tmp_path, monkeypatch):
    db = Database(tmp_path / "db.sqlite3")
    manga = MangaService(db, cache_dir=tmp_path / "cache")
    now = time.time()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(path,title,page_count,created_at,updated_at) VALUES(?,?,?,?,?)",
            (str(tmp_path / "a.cbz"), "シリーズ", 10, now, now),
        )
        manga_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
    payload = manga.set_metadata_override(manga_id, volume=4, series_title="別シリーズ")
    assert payload["volume"] == 4 and payload["series_title"] == "別シリーズ" and payload["manual_metadata"]
    assert manga.shelf_metadata(manga_id, "シリーズ", str(tmp_path / "a.cbz"))["volume"] == 4
    manga.remove_books([manga_id])
    with db.connect() as conn:
        assert library_overrides.load(conn, "manga") == {}

    audio = AudiobookService(db, ffprobe="ffprobe", ffmpeg="ffmpeg", mpv="mpv", cache_dir=tmp_path / "cache")
    book = _audiobook(audio, tmp_path / "物語.m4b", monkeypatch)
    updated = audio.set_metadata_override(int(book["id"]), volume=2, series_title="")
    assert updated["volume"] == 2 and updated["manual_metadata"] is True
    listed = {int(item["id"]): item for item in audio.search_catalog()}
    assert listed[int(book["id"])]["volume"] == 2


def test_library_set_metadata_api_validates_input():
    from pudge.web_app import WebAppApi as WebApi

    calls = []

    class Service:
        def set_metadata_override(self, item_id, *, volume, series_title):
            calls.append((item_id, volume, series_title))
            if volume == 1000:
                raise ValueError("Volume must be between 1 and 999")
            return {"id": item_id, "volume": volume}

    api = WebApi.__new__(WebApi)
    api.light_novels = api.manga = api.audiobooks = Service()
    assert api.library_set_metadata("ln", 3, "5", " 名前 ") == {"ok": True, "item": {"id": 3, "volume": 5}}
    assert calls[-1] == (3, 5, " 名前 ")
    assert api.library_set_metadata("manga", 3, "", "")["ok"] is True and calls[-1] == (3, None, "")
    assert api.library_set_metadata("ln", 3, "x", "")["ok"] is False
    assert api.library_set_metadata("ln", 3, "1000", "")["error"] == "Volume must be between 1 and 999"
    assert api.library_set_metadata("video", 3, "1", "")["ok"] is False


def test_edit_volume_and_series_menus_and_dialog(tmp_path, monkeypatch, page):
    novels = _ln(tmp_path / "ln")
    _insert_ln(novels, tmp_path, "物語 1")
    db = Database(tmp_path / "db.sqlite3")
    manga = MangaService(db, cache_dir=tmp_path / "cache")
    now = time.time()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO manga_books(path,title,page_count,created_at,updated_at) VALUES(?,?,?,?,?)",
            (str(tmp_path / "a.cbz"), "シリーズ", 10, now, now),
        )
    audio = AudiobookService(db, ffprobe="ffprobe", ffmpeg="ffmpeg", mpv="mpv", cache_dir=tmp_path / "cache")
    _audiobook(audio, tmp_path / "物語.m4b", monkeypatch)
    as_json = lambda value: json.loads(json.dumps(value, default=str))  # noqa: E731
    audio_state = as_json(audio.state(queue_missing=False))
    audio_state["books"][0]["cover_url"] = (
        "https://img.example/audio.jpg"  # the audiobook menu opens from its cover
    )
    answers = {
        "light_novel_state": as_json(novels.state()),
        "manga_state": as_json(manga.state()),
        "audiobook_state": audio_state,
        "library_set_metadata": {"ok": True, "item": {"id": 1}},
    }
    for lang in ("en", "ru"):
        result = page.run(
            """
            const dialog = () => ({open: PudgeLibraryMetadata.isOpen(), title: __text('#pudgeMetaTitle'), book: __text('#pudgeMetaBook'),
                                   volume: $('pudgeMetaVolume')?.placeholder, series: $('pudgeMetaSeries')?.placeholder});
            const edit = async (page, card, action) => {
              setPage(page); await __tick(200);
              await __fire(card, 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent'); await __tick(10);
              await __click(`[${action}="edit-metadata"]`); await __tick(20);
              return dialog();
            };
            const out = {expected: {title: PudgeI18n.t('libraryUi.editVolumeAndSeries'), saved: PudgeI18n.t('ui.volumeAndSeriesSaved')}};
            out.ln = await edit('lightnovels', '[data-ln-book]', 'data-ln-context-action');
            await __input('#pudgeMetaVolume', '3'); await __input('#pudgeMetaSeries', '物語シリーズ');
            await __click('#pudgeLibraryMetaBackdrop [data-pudge-meta=save]'); await __tick(20);
            out.lnSaved = {open: PudgeLibraryMetadata.isOpen(), toast: __toasts().at(-1)};
            out.manga = await edit('manga', '[data-manga-book]', 'data-manga-context-action');
            await __click('#pudgeLibraryMetaBackdrop [data-pudge-meta=save]'); await __tick(20);
            out.audio = await edit('audiobooks', '.audiobook-cover', 'data-audio-context-action');
            await __key('Escape'); await __tick(10);                     // closes the dialog only
            out.escaped = {open: PudgeLibraryMetadata.isOpen(), page: ui.page};
            return out;
            """,
            lang=lang,
            api=answers,
        )
        out = result.value
        expected = out["expected"]
        assert out["ln"] == {
            "open": True,
            "title": expected["title"],
            "book": "物語 1",
            "volume": "1",
            "series": "物語",
        }
        assert out["lnSaved"] == {"open": False, "toast": expected["saved"]}
        assert out["manga"]["open"] is True and out["manga"]["book"] == "シリーズ"
        assert out["audio"]["open"] is True and out["audio"]["book"] == "物語"
        assert out["escaped"] == {"open": False, "page": "audiobooks"}
        assert result.calls_of("library_set_metadata") == [
            ["ln", 1, "3", "物語シリーズ"],
            ["manga", 1, "", ""],
        ]


# ------------------------------------------------------------ 12. stricter LN↔audio


def test_spin_off_with_the_main_title_does_not_auto_link(tmp_path, monkeypatch):
    novels = _ln(tmp_path)
    main = _insert_ln(novels, tmp_path, "とある魔術の禁書目録", volume=1)
    service = _audio(tmp_path)
    book = _audiobook(service, tmp_path / "とある魔術の禁書目録外伝.m4b", monkeypatch)
    assert service.auto_link_audiobook(int(book["id"])) is None
    assert _audiobook_title_match_score("とある魔術の禁書目録外伝", "とある魔術の禁書目録") <= 89
    # The same title still links by itself.
    other = _audiobook(service, tmp_path / "とある魔術の禁書目録.m4b", monkeypatch)
    linked = service.auto_link_audiobook(int(other["id"]))
    assert linked and linked["ln_book_id"] == main


# ------------------------------------------------- 13. LN list without per-read repair


def test_ln_list_uses_stored_chapter_lengths_and_repairs_once(tmp_path, monkeypatch):
    service = _ln(tmp_path)
    book_id = _insert_ln(service, tmp_path, "長い本", chapters=("あいうえお", "かきく"))
    conn = service._connect()
    try:
        assert [
            r[0]
            for r in conn.execute(
                "SELECT char_count FROM ln_chapters WHERE book_id=? ORDER BY chapter_index", (book_id,)
            )
        ] == [5, 3]
        conn.execute(
            "UPDATE ln_chapters SET text='さしすせそたち' WHERE book_id=? AND chapter_index=1", (book_id,)
        )
        conn.commit()
        assert (
            conn.execute(
                "SELECT char_count FROM ln_chapters WHERE book_id=? AND chapter_index=1", (book_id,)
            ).fetchone()[0]
            == 7
        )
    finally:
        conn.close()
    repairs = []
    real = service._repair_missing_volumes
    monkeypatch.setattr(service, "_repair_missing_volumes", lambda: repairs.append(1) or real())
    for _ in range(3):
        books = service.books()
    assert repairs == [1]
    assert books[0]["character_count"] == 12
    # The list adds the stored lengths; it does not measure every chapter's text on each read.
    conn = service._connect()
    try:
        conn.execute(
            "UPDATE ln_chapters SET char_count=char_count+100 WHERE book_id=? AND chapter_index=0", (book_id,)
        )
        conn.commit()
    finally:
        conn.close()
    assert service.books()[0]["character_count"] == 112
