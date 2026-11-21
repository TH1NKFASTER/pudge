"""Light novels: the "read up to here" marker, stored on the Mac and shared with every paired
iPhone/iPad (pudge/ln_read_marker.py).

Maintainer's request: "Read up to here" shows a small red marker exactly at that character; the
automatic progress bookmark (the Mac's dwell save, a phone's own place, the paired audiobook)
moves the same marker, forward only and only when it is far enough (300 characters), snapped to
its sentence start. The marker (book, chapter, character, kind, time, device) syncs both ways
with the existing v2 media routes; an automatic update from one device never takes a newer
manual marker of another device unless it is further ahead and newer; older phone builds keep
working (they send no `paragraph` and get the answers they knew).

The window side is tests/test_fix_ln_read_marker_page.py; the iPhone/iPad side (Swift) is
tests/test_fix_ios_reader_guards.py.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import threading
import time
from contextlib import contextmanager

import pytest

from pudge import ln_read_marker
from pudge.iphone_service import IPhoneService
from test_fix_iphone_multi_device import _second_device
from test_web_app import make_api

INTRO = "第一章"
# Chapter 0 (code points): INTRO, then a paragraph with a quote, a long one, one with an
# astral kanji (two UTF-16 units) and an illustration between.
P1 = "彼は走った。「待って！」と叫んだ。"
P2 = "あ" * 400 + "。" + "い" * 399 + "。"
P3 = "𠮷野家の前で止まった。そして歩いた。"
IMAGE = "[[PUDGE_LN_IMAGE_URL:covers/x.png]]"
CHAPTER0 = f"{INTRO}\n　{P1}\n\n{P2}\n{IMAGE}\n{P3}"
CHAPTER1 = "二章の始まり。" + "う" * 500 + "。"
CHAPTER2 = "三章。" + "え" * 300


def _api(tmp_path):
    api = make_api(tmp_path)
    with api.manager.db.connect() as conn:
        book = conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,created_at,updated_at) VALUES('本',?,'epub',0,0)",
            (str(tmp_path / "book.epub"),),
        ).lastrowid
        for index, text in enumerate((CHAPTER0, CHAPTER1, CHAPTER2)):
            conn.execute(
                "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash,char_count) VALUES(?,?,?,?,?,?)",
                (book, index, f"第{index + 1}章", text, hashlib.sha256(text.encode()).hexdigest(), len(text)),
            )
    return api, int(book)


def _phone(tmp_path, api, name="Maksim iPhone"):
    mobile = IPhoneService(api.manager.db, tmp_path / "iphone")
    mobile.api = api
    return mobile, _second_device(mobile, name)


def _place(api, book):
    with api.manager.db.connect() as conn:
        row = conn.execute(
            "SELECT current_chapter,current_offset FROM ln_books WHERE id=?", (book,)
        ).fetchone()
    return int(row[0]), float(row[1])


def at(text: str, needle: str, nth: int = 0) -> int:
    """Code-point index of the `nth` occurrence of `needle`."""
    index = -1
    for _ in range(nth + 1):
        index = text.index(needle, index + 1)
    return index


# --------------------------------------------------------------------------- the rules
def test_sentence_start_snapping() -> None:
    text = CHAPTER0
    # Inside a sentence: its start; a quote's own "！" ends nothing for the words after it.
    assert ln_read_marker.sentence_start(text, at(text, "叫")) == at(text, "「")
    assert ln_read_marker.sentence_start(text, at(text, "待")) == at(text, "「")
    assert ln_read_marker.sentence_start(text, at(text, "は走")) == at(text, "彼")  # the indent is skipped
    assert ln_read_marker.sentence_start(text, at(text, "歩")) == at(text, "そして")
    # A line break or the paragraph's indent: the next text.
    assert ln_read_marker.sentence_start(text, at(text, "　")) == at(text, "彼")
    # Sentence start farther back than 80 characters: the place sent (the start of its line).
    deep = at(text, "あ") + 150
    assert ln_read_marker.sentence_start(text, deep) == deep
    assert ln_read_marker.sentence_start(text, at(text, "あ") + 60) == at(text, "あ")


def test_distance_counts_through_the_chapters_between() -> None:
    lengths = {0: 100, 1: 50, 2: 80}
    assert ln_read_marker.distance(lengths, (0, 10), (0, 40)) == 30
    assert ln_read_marker.distance(lengths, (0, 90), (2, 5)) == 10 + 50 + 5
    assert ln_read_marker.distance(lengths, (2, 5), (0, 90)) == -65


def test_phone_places_round_trip_through_utf16() -> None:
    text = CHAPTER0
    rows = ln_read_marker.paragraphs(text)
    assert [value for _start, value in rows] == [INTRO, P1, P2, IMAGE, P3]
    # 野 comes after 𠮷 (one character, two UTF-16 units).
    char = at(text, "野")
    assert ln_read_marker.phone_place(text, char) == (4, 2)
    assert ln_read_marker.char_from_phone(text, 4, 2) == char
    assert ln_read_marker.char_from_phone(text, 4, 1) == at(text, "𠮷")  # inside the pair: its start
    # Between paragraphs (indent, line breaks) and on an illustration: the next text.
    assert ln_read_marker.phone_place(text, at(text, "　")) == (1, 0)
    assert ln_read_marker.phone_place(text, at(text, IMAGE) + 3) == (4, 0)
    # An old phone's fraction names a paragraph start, counted as the phone counts.
    sizes = [len(INTRO), len(P1), len(P2), 1, len(P3)]
    fraction = sum(sizes[:2]) / sum(sizes)
    assert ln_read_marker.phone_paragraph_at_fraction(text, fraction) == 2


# ------------------------------------------------------------------- Mac reader (API)
def test_read_up_to_here_sits_exactly_at_the_character_and_auto_follows_forward_only(tmp_path) -> None:
    api, book = _api(tmp_path)
    word = at(CHAPTER0, "待")
    first = api.light_novel_bookmark(book, 0, 0.1, "manual", word)
    assert first["marker_moved"] is True and first["bookmark_source"] == "manual"
    marker = first["read_marker"]
    assert (marker["chapter_index"], marker["char_offset"], marker["source"]) == (0, word, "manual")
    assert marker["device_id"] == "mac" and marker["auto_min_chars"] == 300
    assert api.light_novel_open(book)["read_marker"]["char_offset"] == word
    # The dwell save: the place always moves, the marker only 300+ characters ahead.
    near = at(CHAPTER0, "あ") + 100
    result = api.light_novel_bookmark(book, 0, 0.3, "auto", near)
    assert result["marker_moved"] is False and result["read_marker"]["char_offset"] == word
    assert _place(api, book) == (0, 0.3)
    behind = api.light_novel_bookmark(book, 0, 0.0, "auto", 0)
    assert behind["marker_moved"] is False and _place(api, book) == (0, 0.0)
    # Far enough: the start of its sentence (the second sentence of the long paragraph).
    far = at(CHAPTER0, "い") + 30
    moved = api.light_novel_bookmark(book, 0, 0.6, "auto", far)
    assert moved["marker_moved"] is True
    assert moved["read_marker"]["char_offset"] == at(CHAPTER0, "い")
    assert moved["read_marker"]["source"] == "auto"
    # Never back automatically, even from an earlier chapter's dwell...
    assert api.light_novel_bookmark(book, 0, 0.1, "auto", at(CHAPTER0, "彼"))["marker_moved"] is False
    # ...but a deliberate marker may go back.
    back = api.light_novel_bookmark(book, 0, 0.1, "manual", at(CHAPTER0, "叫"))
    assert back["marker_moved"] is True and back["read_marker"]["char_offset"] == at(CHAPTER0, "叫")


def test_the_threshold_counts_through_chapters_and_the_audio_moves_only_the_marker(tmp_path) -> None:
    api, book = _api(tmp_path)
    end = len(CHAPTER0) - 100
    api.light_novel_bookmark(book, 0, 0.9, "manual", end)
    # 100 characters left in chapter 0 + 150 into chapter 1: 250, not enough.
    short = api.light_novel_move_read_marker(book, 1, 150, "auto")
    assert short["marker_moved"] is False and short["read_marker"]["chapter_index"] == 0
    # 100 + 250 = 350: the marker goes to chapter 1, at its sentence start when that is close.
    far = api.light_novel_move_read_marker(book, 1, 250, "auto")
    assert far["marker_moved"] is True
    assert (far["read_marker"]["chapter_index"], far["read_marker"]["char_offset"]) == (1, 250)
    # Only the marker: the reading place stays where the reader saved it.
    assert _place(api, book) == (0, 0.9)
    assert api.light_novel_read_marker(book)["read_marker"]["char_offset"] == 250
    # The legacy call (older windows) saves the place and moves the marker by the same rules.
    api.light_novels.update_position(book, 2, 0.5)
    assert _place(api, book) == (2, 0.5)
    marker = api.light_novel_read_marker(book)["read_marker"]
    assert marker["chapter_index"] == 2 and marker["source"] == "auto"


def test_reset_removes_the_marker(tmp_path) -> None:
    api, book = _api(tmp_path)
    api.light_novel_bookmark(book, 1, 0.5, "manual", 100)
    assert api.light_novel_read_marker(book)["read_marker"]["char_offset"] == 100
    api.light_novel_reset_position(book)
    assert api.light_novel_read_marker(book)["read_marker"] is None


# ----------------------------------------------------------------------- phone sync
def test_the_mac_marker_reaches_the_phone_and_the_phones_reaches_the_mac(tmp_path) -> None:
    api, book = _api(tmp_path)
    mobile, phone = _phone(tmp_path, api)
    word = at(CHAPTER0, "野")
    api.light_novel_bookmark(book, 0, 0.5, "manual", word)
    detail = mobile.media.handle("GET", f"/api/v2/media/ln/{book}", {}, dict, device=phone)
    marker = detail["marker"]
    assert {k: marker[k] for k in ("chapter", "paragraph", "paragraphOffset", "char", "kind")} == {
        "chapter": 0,
        "paragraph": 4,
        "paragraphOffset": 2,
        "char": word,
        "kind": "manual",
    }
    assert marker["device"] == "mac" and marker["deviceName"] == "Mac" and marker["here"] is False
    # "Read up to here" on the phone (offline a minute ago): exact, with its kind, time and device.
    observed = time.time() - 60
    body = {"chapter": 0, "paragraph": 1, "paragraphOffset": 7, "kind": "manual", "observedAt": observed}
    answer = mobile.media.handle("POST", f"/api/v2/media/ln/{book}/marker", {}, lambda: body, device=phone)
    assert answer["ok"] is True and answer["moved"] is False  # the Mac's marker is newer and further ahead
    body["observedAt"] = time.time()
    answer = mobile.media.handle("POST", f"/api/v2/media/ln/{book}/marker", {}, lambda: body, device=phone)
    assert answer["moved"] is True and answer["marker"]["here"] is True
    mac = api.light_novel_read_marker(book)["read_marker"]
    assert mac["char_offset"] == at(CHAPTER0, "待") and mac["source"] == "manual"
    assert mac["device_id"] == phone and mac["device_name"] == "Maksim iPhone"
    assert abs(mac["updated_at"] - body["observedAt"]) < 1


def test_a_phones_reading_moves_the_marker_forward_and_old_apps_keep_their_answers(tmp_path) -> None:
    api, book = _api(tmp_path)
    mobile, phone = _phone(tmp_path, api)
    media = mobile.media
    # A new app sends the paragraph at the top: the answer carries the marker.
    answer = media.ln_position(
        book, {"chapter": 0, "offset": 0.0, "paragraph": 1, "observedAt": time.time()}, phone
    )
    assert answer["ok"] is True and answer["marker"]["paragraph"] == 1 and answer["marker"]["kind"] == "auto"
    assert answer["marker"]["paragraphOffset"] == 0
    # A screen further (not 300 characters): the place moves, the marker does not.
    answer = media.ln_position(
        book, {"chapter": 0, "offset": 0.05, "paragraph": 2, "observedAt": time.time()}, phone
    )
    assert answer["marker"]["paragraph"] == 1
    # An old app (no paragraph): the answer it always got; the marker follows the paragraph of its
    # fraction when that is far enough.
    sizes = [len(INTRO), len(P1), len(P2), 1, len(P3)]
    fraction = sum(sizes[:4]) / sum(sizes)
    assert media.ln_position(book, {"chapter": 0, "offset": fraction, "observedAt": time.time()}, phone) == {
        "ok": True
    }
    marker = api.light_novel_read_marker(book)["read_marker"]
    assert marker["char_offset"] == at(CHAPTER0, "𠮷") and marker["device_id"] == phone
    assert _place(api, book) == (0, pytest.approx(fraction))


def test_an_automatic_update_never_takes_a_newer_manual_marker_unless_ahead_and_newer(tmp_path) -> None:
    api, book = _api(tmp_path)
    mobile, phone = _phone(tmp_path, api)
    _second = _second_device(mobile, "Maksim iPad")
    api.light_novel_bookmark(book, 1, 0.5, "manual", 10)  # set on the Mac now
    set_at = api.light_novel_read_marker(book)["read_marker"]["updated_at"]
    # The iPad read chapter 2 offline before that (sent late): further ahead but older.
    late = {"chapter": 2, "offset": 0.0, "paragraph": 0, "observedAt": set_at - 120}
    answer = mobile.media.ln_position(book, late, _second)
    assert answer["marker"]["chapter"] == 1 and answer["marker"]["kind"] == "manual"
    # Newer but not far enough ahead: kept too.
    close = {"chapter": 1, "paragraph": 0, "paragraphOffset": 100, "kind": "auto", "observedAt": time.time()}
    assert mobile.media.ln_marker(book, close, phone)["moved"] is False
    # Newer and far enough ahead: the automatic marker takes over.
    ahead = {"chapter": 2, "offset": 0.0, "paragraph": 0, "observedAt": time.time()}
    answer = mobile.media.ln_position(book, ahead, _second)
    assert answer["marker"]["chapter"] == 2 and answer["marker"]["kind"] == "auto"
    assert answer["marker"]["deviceName"] == "Maksim iPad" and answer["marker"]["here"] is True
    # An older manual marker sent late still counts when it is further ahead (as places do)...
    early = {"chapter": 2, "paragraph": 0, "paragraphOffset": 50, "kind": "manual", "observedAt": set_at - 60}
    assert mobile.media.ln_marker(book, early, phone)["moved"] is True
    # ...and one that is behind does not.
    behind = {"chapter": 0, "paragraph": 1, "paragraphOffset": 0, "kind": "manual", "observedAt": set_at - 90}
    assert mobile.media.ln_marker(book, behind, phone)["moved"] is False
    assert api.light_novel_read_marker(book)["read_marker"]["char_offset"] == 50


def test_the_marker_route_gets_the_device_from_its_token(tmp_path) -> None:
    from pudge.iphone_http import IPhoneHTTPServer

    seen = []

    class Media:
        def handle(self, command, route, query, body, device=""):
            seen.append((command, route, body(), device))
            return {"ok": True}

    class Service:
        server_id = "server"
        media = Media()

        def authenticate(self, token):
            if token != "token":
                raise PermissionError("bad token")
            return "the-phone"

    server = IPhoneHTTPServer(("127.0.0.1", 0), Service())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        body = json.dumps({"chapter": 0, "paragraph": 1, "paragraphOffset": 2, "kind": "manual"})
        connection.request("POST", "/api/v2/media/ln/3/marker", body, {"Authorization": "Bearer token"})
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read()) == {"ok": True}
        connection.close()
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)
    assert seen == [("POST", "/api/v2/media/ln/3/marker", json.loads(body), "the-phone")]


@contextmanager
def _no_reader(mobile):
    api = mobile.api
    mobile.api = type("Bare", (), {"light_novel_position": lambda self, *a: None})()
    try:
        yield
    finally:
        mobile.api = api


def test_without_the_reader_service_places_still_save(tmp_path) -> None:
    api, book = _api(tmp_path)
    mobile, phone = _phone(tmp_path, api)
    with _no_reader(mobile):
        assert mobile.media.ln_position(book, {"chapter": 0, "offset": 0.2, "paragraph": 1}, phone) == {
            "ok": True
        }
