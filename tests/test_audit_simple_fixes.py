"""Behaviour tests for the 2026-10-04 audit fixes (no source-text checks)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pudge.database import Database


def test_volume_suffix_never_cuts_a_longer_number():
    from pudge.light_novels import _volume_from_text

    assert _volume_from_text("俺の妹2020") is None
    assert _volume_from_text("俺の妹２０２０") is None
    assert _volume_from_text("狼と香辛料2") == 2
    assert _volume_from_text("あそびのかんけい２") == 2


def test_audiobook_volume_digits_and_roman_numerals():
    from pudge.audiobooks import _audiobook_series_title, _audiobook_volume

    assert _audiobook_volume("俺の妹2020") is None
    assert _audiobook_volume("狼と香辛料2") == 2
    assert _audiobook_volume("狼と香辛料II") == 2
    # Ordinary words ending in roman letters are not volumes.
    assert _audiobook_volume("オッドタクシー Taxi") is None
    assert _audiobook_volume("ナビ Navi") is None
    assert _audiobook_series_title("俺の妹2020", volume=20) == "俺の妹2020"
    assert _audiobook_series_title("狼と香辛料II", volume=2) == "狼と香辛料"


def test_finishing_an_earlier_volume_never_rolls_anilist_back(monkeypatch, tmp_path):
    from pudge.light_novels import LightNovelService

    service = LightNovelService.__new__(LightNovelService)
    sent = []
    book = {"volume": 2, "anilist_progress_volumes": 5, "anilist_id": 7, "anilist_total_volumes": 5}
    monkeypatch.setattr(service, "book", lambda _id: dict(book), raising=False)
    monkeypatch.setattr(
        service,
        "_save_anilist_volume",
        lambda media, progress, status: sent.append((progress, status)) or {"status": status},
        raising=False,
    )
    service.config = SimpleNamespace(anilist=SimpleNamespace(enabled=True, access_token="t"))

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, *_args):
            return None

    monkeypatch.setattr(service, "_connection", lambda: Connection(), raising=False)
    service.finish_volume(1)
    assert sent == [(5, "COMPLETED")]


def test_qbittorrent_upload_states_are_finished_downloads():
    from pudge.torrent_observation import normalize_torrent_state

    assert normalize_torrent_state("stalledUP") == "seeding"
    assert normalize_torrent_state("queuedUP") == "seeding"
    assert normalize_torrent_state("stalledDL") == "stalled"
    assert normalize_torrent_state("queuedDL") == "queued"


def test_diagnostics_for_unknown_anime_do_not_create_library_rows(tmp_path):
    from pudge.diagnostics import DiagnosticRecorder

    db = Database(tmp_path / "db.sqlite")
    store = DiagnosticRecorder(db)
    store.record("c1", "test", "event", media_id=424242, payload={"x": 1})
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM anime WHERE media_id=424242").fetchone()[0] == 0
    rows = store.recent(media_id=424242)
    assert len(rows) == 1 and rows[0]["payload"] == {"x": 1, "unlinked_media_id": 424242}


def test_damaged_download_row_does_not_break_the_list(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO downloads(torrent_hash,name,state,raw_json,updated_at) VALUES('a','A','downloading','{bad',0)"
        )
        conn.execute(
            "INSERT INTO downloads(torrent_hash,name,state,raw_json,updated_at) VALUES('b','B','downloading','{\"k\":1}',0)"
        )
    rows = {row.torrent_hash: row.raw for row in db.downloads()}
    assert rows == {"a": {}, "b": {"k": 1}}


def test_open_url_only_opens_web_links(monkeypatch):
    from pudge import web_app

    opened = []
    monkeypatch.setattr(web_app.webbrowser, "open", opened.append)
    api = web_app.WebAppApi.__new__(web_app.WebAppApi)
    api.logger = SimpleNamespace(warning=lambda *a, **k: None)
    for bad in [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "x-apple.systempreferences:",
        "/Applications",
        "https:",
    ]:
        assert api.open_url(bad)["ok"] is False
    assert api.open_url("https://anilist.co/anime/1")["ok"] is True
    assert opened == ["https://anilist.co/anime/1"]


def test_gui_entry_uses_the_shared_config_location(monkeypatch):
    import sys
    import pudge.app_entry as entry
    from pudge.config import DEFAULT_CONFIG_PATH

    seen = []
    monkeypatch.delenv("PUDGE_CONFIG", raising=False)
    monkeypatch.setattr(entry, "_set_macos_app_icon", lambda: None)
    monkeypatch.setattr(entry, "maybe_handle_notification_helper", lambda args: None)
    monkeypatch.setitem(
        sys.modules, "pudge.app_ui", SimpleNamespace(launch_app=lambda path: seen.append(path) or 0)
    )
    assert entry.main([]) == 0
    assert seen == [DEFAULT_CONFIG_PATH]


def test_hot_query_indexes_exist(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    with db.connect() as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert {
        "idx_episodes_torrent_hash",
        "idx_subtitle_jobs_state_next",
        "idx_subtitle_jobs_media_episode",
    } <= names
    # Reopening the current schema keeps working (integrity check outside the write lock).
    Database(tmp_path / "db.sqlite")


def test_jimaku_download_is_atomic_and_rejects_truncated_files(tmp_path):
    import httpx
    from pudge.models import JimakuFile
    from pudge.providers.jimaku import JimakuClient, JimakuError

    payload = {"body": b"1\n00:00:01,000 --> 00:00:02,000\nx\n"}

    def handler(request):
        return httpx.Response(200, content=payload["body"])

    client = JimakuClient.__new__(JimakuClient)
    client.client = httpx.Client(transport=httpx.MockTransport(handler))
    item = JimakuFile(
        url="https://jimaku.cc/f/1", name="ep.srt", size=len(payload["body"]) + 10, last_modified=""
    )
    with pytest.raises(JimakuError):
        client.download(item, tmp_path)
    assert not list((tmp_path / "jimaku").rglob("*.srt"))
    item = JimakuFile(url="https://jimaku.cc/f/1", name="ep.srt", size=len(payload["body"]), last_modified="")
    path = client.download(item, tmp_path)
    assert path.read_bytes() == payload["body"] and not list(path.parent.glob("*.part"))


def test_ln_reading_statistics_record_character_positions_not_a_fraction(page):
    # The reader reports where the reader is for the statistics every 5 s; the chapter
    # position is a 0..1 fraction of its scroll, the statistics count characters (it used
    # to send the fraction itself: offset 0.25 instead of 250 of 1000 characters).
    result = page.run(
        r"""
        __api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 1, current_offset: 0,
          chapters: [{chapter_index: 0, title: 'One'}, {chapter_index: 1, title: 'Two'}]});
        __api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, chapter_key: 'k',
          character_count: 1000, paragraphs: ['猫'.repeat(400), '犬'.repeat(600)], tokens: [[], []], vocabulary: []});
        await __settle(openLightNovel(3)); await __tick(300);
        const scroll = $('lnReaderScroll');
        scroll.scrollHeight = 1100; scroll.clientHeight = 100; scroll.scrollTop = 250;     // a quarter in
        await __tick(5000);
        scroll.scrollTop = 500;                                                            // half way
        await __tick(5000);
        """
    )
    [[kind, book, seconds, before, after, payload]] = result.calls_of("consumption_reader_observation")
    assert (kind, book, seconds) == ("light_novel", 3, 5)
    assert before == {"chapter_index": 1, "chapter_key": "k", "character_offset": 250}
    assert after == {"chapter_index": 1, "chapter_key": "k", "character_offset": 500}
    assert payload["character_count"] == 1000
