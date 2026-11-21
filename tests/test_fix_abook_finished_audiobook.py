"""A finished audiobook says so, on the Mac and the iPhone, and offers to mark its light novel read.

One rule (pudge/audiobook_finish.py, PudgeCore ``AudiobookFinish``): Finished is set by the
Finished button / end of playback or by a saved place in the last 2% (or last 5 s), and
cleared only by Mark unfinished. A place at the end saved through the iPhone protocol (the
phone's periodic save, the Mac player while iPhone access is on) used to leave the book
unfinished: only the "finish" reason set it there.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from test_iphone_audio_repository import archived, checkpoint
from test_iphone_audio_repository import service as service_fixture
from test_iphone_desktop_integration import desktop_book

from pudge import audiobook_finish
from pudge.audiobooks import AudiobookService
from pudge.database import Database

service = service_fixture


def _mac_book(service, book):
    with service.db.connect() as conn:
        row = conn.execute("SELECT position,finished FROM audiobooks WHERE id=?", (book,)).fetchone()
    return float(row["position"]), bool(row["finished"])


def test_the_shared_rule() -> None:
    assert audiobook_finish.reached_end(3528, 3600)  # the last 2%
    assert not audiobook_finish.reached_end(3500, 3600)
    assert audiobook_finish.reached_end(96, 100)  # the last 5 s of a short book
    assert not audiobook_finish.reached_end(0, 0)
    assert audiobook_finish.finished_after_save(False, 3600, 3600, "periodic") is True
    assert audiobook_finish.finished_after_save(True, 10, 3600, "seek") is True  # re-listening keeps it
    assert audiobook_finish.finished_after_save(True, 0, 3600, "reset") is True
    assert audiobook_finish.finished_after_save(False, 0, 3600, "reset") is False
    assert audiobook_finish.finished_after_save(True, 3600, 3600, "unfinish") is False
    assert audiobook_finish.finished_after_save(False, 1, 3600, "finish") is True


def test_a_phone_place_at_the_end_finishes_the_book_everywhere(service, tmp_path) -> None:
    device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    asset = manifest["assetId"]
    end = manifest["durationMs"]
    assert (
        service.record_checkpoint(device, checkpoint(manifest, device, position=1000))["status"] == "accepted"
    )
    assert service.progress(asset)["finished"] is False
    # The app was closed in the last seconds: a periodic save, no "finish".
    at_end = checkpoint(manifest, device, base=1, position=end - 1000, sequence=2, reason="periodic")
    assert service.record_checkpoint(device, at_end)["status"] == "accepted"
    assert service.progress(asset)["finished"] is True
    assert _mac_book(service, book)[1] is True
    # Mark unfinished clears it; a place back in the book keeps it cleared.
    unfinish = checkpoint(manifest, device, base=2, position=0, reason="unfinish")
    assert service.record_checkpoint(device, unfinish)["status"] == "accepted"
    assert service.progress(asset)["finished"] is False
    again = checkpoint(manifest, device, base=3, position=2000, reason="periodic")
    assert service.record_checkpoint(device, again)["status"] == "accepted"
    assert service.progress(asset)["finished"] is False


def test_the_mac_player_at_the_end_finishes_the_book_with_iphone_access_on(service, tmp_path) -> None:
    _device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    asset = manifest["assetId"]
    service.begin_mac_session(asset, "mac-session")
    service.record_mac_position(asset, "mac-session", 30_000)
    assert service.progress(asset)["finished"] is False
    service.record_mac_position(asset, "mac-session", manifest["durationMs"])
    assert service.progress(asset)["finished"] is True
    assert _mac_book(service, book)[1] is True


def _novel_tables(db: Database) -> None:
    with db.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ln_books(id INTEGER PRIMARY KEY,title TEXT,volume INT,anilist_id INT,"
            "cover_url TEXT,current_chapter INT DEFAULT 0,current_offset REAL DEFAULT 0,finished INT DEFAULT 0,"
            "updated_at REAL DEFAULT 0)"
        )
        conn.execute("INSERT INTO ln_books(id,title,volume) VALUES(7,'Invented Tale 2',2)")


def test_the_mac_card_knows_whether_the_linked_light_novel_is_read(tmp_path) -> None:
    db = Database(tmp_path / "db.sqlite3")
    audio = AudiobookService(db, ffprobe="ffprobe", mpv="mpv", cache_dir=tmp_path / "cache")
    audio.transcription_status = lambda _book: {"status": "unknown", "ready": False}  # type: ignore[method-assign]
    path = tmp_path / "tale.mp3"
    path.write_bytes(b"audio")
    _novel_tables(db)
    with db.connect() as conn:
        book = conn.execute(
            "INSERT INTO audiobooks(path,title,duration,position,finished,created_at,updated_at) "
            "VALUES(?,?,600,600,1,0,0)",
            (str(path), "Invented Tale 2"),
        ).lastrowid
        conn.execute(
            "INSERT INTO reading_audio_links(ln_book_id,audiobook_id,created_at,updated_at) VALUES(7,?,?,?)",
            (book, time.time(), time.time()),
        )
    payload = audio.book(int(book), include_transcription=False)
    assert payload["finished"] is True
    assert payload["linked_light_novel"]["id"] == 7 and payload["linked_light_novel"]["finished"] is False
    with db.connect() as conn:
        conn.execute("UPDATE ln_books SET finished=1 WHERE id=7")
    assert audio.book(int(book), include_transcription=False)["linked_light_novel"]["finished"] is True
    # Mark unfinished from the very end starts over (the end would finish it again).
    audio.is_playing = lambda _book: False  # type: ignore[method-assign]
    result = audio.mark_finished(int(book), False)
    assert result["book"]["finished"] is False and result["book"]["position"] == 0.0


def test_the_phone_light_novel_list_names_the_linked_audiobook_assets(service, tmp_path) -> None:
    _device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    _novel_tables(service.db)
    with service.db.connect() as conn:
        conn.execute("INSERT INTO ln_books(id,title,volume) VALUES(8,'Invented Tale 3',3)")
        conn.execute(
            "INSERT INTO reading_audio_links(ln_book_id,audiobook_id,created_at,updated_at) VALUES(7,?,1,1)",
            (book,),
        )
    service.api = SimpleNamespace(light_novels=None)
    items = {item["id"]: item for item in service.media.light_novels()["items"]}
    assert items[7]["audioAssetIds"] == [manifest["assetId"]] and items[7]["hasAudio"] is True
    assert items[8]["audioAssetIds"] == [] and items[8]["finished"] is False


BOOK = {
    "id": 5,
    "title": "Invented Tale 2",
    "duration": 600,
    "position": 600,
    "finished": True,
    "speed": 1.0,
    "chapters": [],
    "linked_light_novel": {"id": 7, "title": "Invented Tale", "volume": 2, "finished": False},
}

CARD = r"""
setPage('audiobooks', true);
await __tick(500);
const card = document.querySelector('.audiobook-card[data-audiobook-id="5"]');
const offer = card?.querySelector('.audiobook-ln-finish-offer');
return {
  finished: card?.classList.contains('finished'),
  badge: card?.querySelector('.audiobook-finished-badge')?.textContent || null,
  offer: offer ? offer.querySelector('span').textContent : null,
  button: offer?.querySelector('[data-media-action="finish-linked-ln"]')?.textContent || null,
};
"""


def test_the_mac_card_says_finished_and_offers_the_linked_light_novel(page) -> None:
    ru = page.run(CARD, lang="ru", api={"audiobook_state": {"books": [BOOK]}}).value
    assert ru["finished"] is True and ru["badge"] == "✓Прослушано"
    assert ru["offer"] == "Ранобэ «Invented Tale · Том 2» ещё не отмечено прочитанным."
    assert ru["button"] == "Отметить ранобэ прочитанным"
    en = page.run(CARD, lang="en", api={"audiobook_state": {"books": [BOOK]}}).value
    assert en["badge"] == "✓Finished" and en["button"] == "Mark light novel read"
    # Not finished, no linked volume, or the volume already read: no offer (and no badge unfinished).
    for book in (
        {**BOOK, "finished": False, "position": 100},
        {**BOOK, "linked_light_novel": None},
        {**BOOK, "linked_light_novel": {**BOOK["linked_light_novel"], "finished": True}},
    ):
        out = page.run(CARD, lang="en", api={"audiobook_state": {"books": [book]}}).value
        assert out["offer"] is None
        assert (out["badge"] is not None) is bool(book["finished"])


def test_mark_read_finishes_the_light_novel_and_not_now_hides_the_offer(page) -> None:
    result = page.run(
        r"""
        setPage('audiobooks', true);
        await __tick(500);
        await __click('[data-media-action="finish-linked-ln"]');
        await __tick(50);
        const call = __apiCalls.find(item => item.name === 'light_novel_set_finished');
        return {args: call?.args, offer: document.querySelectorAll('.audiobook-ln-finish-offer').length};
        """,
        api={"audiobook_state": {"books": [BOOK]}, "light_novel_set_finished": {"ok": True}},
    ).value
    assert result == {"args": [7, True], "offer": 0}
    dismissed = page.run(
        r"""
        setPage('audiobooks', true);
        await __tick(500);
        await __click('[data-media-action="dismiss-linked-ln"]');
        await __tick(50);
        return {
          calls: __apiCalls.filter(item => item.name === 'light_novel_set_finished').length,
          offer: document.querySelectorAll('.audiobook-ln-finish-offer').length,
          badge: document.querySelectorAll('.audiobook-finished-badge').length,
        };
        """,
        api={"audiobook_state": {"books": [BOOK]}},
    ).value
    assert dismissed == {"calls": 0, "offer": 0, "badge": 1}
