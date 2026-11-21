"""Torrent On resumes what Pudge itself paused while torrents were off, finished torrents too.

Before: Torrent On resumed only unfinished downloads, so a finished pack paused by Torrent
Off stayed stopped for good, never reached its ratio/time goal, and cleanup (which never
takes Pudge's own pause for "seeding ended") kept every watched episode. Now Torrent On
starts it again; once the client's own goal stops it, watched episodes go one by one.
The window's Pause and Stop all are the user's choice and stay paused. Invented data only.
"""

from __future__ import annotations

import logging
import types

import pytest
from test_fix_packdel import HASH, FakeClient, QuietQbt, _make_due, _manager, _pack

from pudge import web_app


class StartableClient(FakeClient):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started: list[str] = []

    def start(self, torrent_hash):
        if self.unreachable:
            raise RuntimeError("client is not answering")
        self.started.append(torrent_hash)
        self.state = "active" if self.aria2 else "uploading"


def _api(manager, monkeypatch, quiet=None):
    if quiet is not None:
        monkeypatch.setattr(web_app, "QBittorrentClient", lambda *args, **kwargs: quiet)
    api = types.SimpleNamespace(config=manager.config, manager=manager, logger=logging.getLogger("t"))
    api._pudge_owned_qbittorrent_row = web_app.WebAppApi._pudge_owned_qbittorrent_row
    for name in (
        "_note_pudge_torrent_pause",
        "_note_pudge_torrent_resume",
        "_resume_unfinished_torrents",
        "_resume_torrents_paused_by_pudge",
        "_resume_incomplete_torrent_jobs_impl",
    ):
        setattr(api, name, types.MethodType(getattr(web_app.WebAppApi, name), api))
    return api


def test_torrent_off_then_on_seeds_a_finished_pack_then_deletes_watched_episodes_one_by_one(
    tmp_path, monkeypatch
):
    client = StartableClient({}, state="uploading")
    manager = _manager(tmp_path, monkeypatch, client)
    _anime_folder, folder, videos = _pack(manager)
    client.payloads[HASH] = folder
    quiet = QuietQbt(client)
    api = _api(manager, monkeypatch, quiet)

    # Torrent Off stops the finished pack: Pudge's own, temporary stop.
    assert web_app.WebAppApi._quiesce_qbittorrent(api, reason="toggle_off") == 1
    assert client.state == "stoppedUP"
    assert manager.cleanup() == 0
    assert all(video.exists() for video in videos.values())

    # Torrent On: the finished pack seeds again and the record is gone.
    assert api._resume_incomplete_torrent_jobs_impl() == 1
    assert client.started == [HASH] and client.state == "uploading"
    assert manager.torrent_paused_by_pudge(HASH) == ""
    assert manager.cleanup() == 0  # still seeding: every file stays
    assert client.deleted == [] and all(video.exists() for video in videos.values())

    # The client's own ratio goal stops it: watched episodes go one by one.
    client.state = "pausedUP"
    assert manager.cleanup() == 1
    assert client.deleted == [(HASH, False)]
    assert not videos["1"].exists() and videos["2"].exists() and videos["3"].exists()
    assert videos["ova"].exists()
    _make_due(manager, videos["2"])
    assert manager.cleanup() == 1
    assert not videos["2"].exists() and videos["3"].exists() and videos["ova"].exists()


def test_torrent_on_resumes_an_aria2_pack_paused_while_torrents_were_off(tmp_path, monkeypatch):
    client = StartableClient({}, state="paused", aria2=True)
    manager = _manager(tmp_path, monkeypatch, client, backend="aria2")
    _anime_folder, folder, _videos = _pack(manager, backend="aria2")
    client.payloads[HASH] = folder
    manager.note_torrents_paused_by_pudge([HASH], reason="torrents_off")
    api = _api(manager, monkeypatch)

    assert api._resume_incomplete_torrent_jobs_impl() == 1
    assert client.started == [HASH] and client.state == "active"
    assert manager.torrent_paused_by_pudge(HASH) == ""


@pytest.mark.parametrize("reason", ["window_pause", "stop_all"])
def test_the_users_own_pause_in_the_window_stays_paused(tmp_path, monkeypatch, reason):
    client = StartableClient({}, state="stoppedUP")
    manager = _manager(tmp_path, monkeypatch, client)
    _anime_folder, folder, videos = _pack(manager)
    client.payloads[HASH] = folder
    manager.note_torrents_paused_by_pudge([HASH], reason=reason)
    api = _api(manager, monkeypatch)

    assert api._resume_incomplete_torrent_jobs_impl() == 0
    assert client.started == [] and client.state == "stoppedUP"
    assert manager.torrent_paused_by_pudge(HASH).startswith(reason + "|")
    assert manager.cleanup() == 0
    assert all(video.exists() for video in videos.values())


def test_unreachable_client_keeps_the_record_for_the_next_torrent_on(tmp_path, monkeypatch):
    client = StartableClient({}, state="stoppedUP", unreachable=True)
    manager = _manager(tmp_path, monkeypatch, client)
    _anime_folder, folder, _videos = _pack(manager)
    client.payloads[HASH] = folder
    manager.note_torrents_paused_by_pudge([HASH], reason="quiet_toggle_off")
    api = _api(manager, monkeypatch)

    assert api._resume_incomplete_torrent_jobs_impl() == 0
    assert manager.torrent_paused_by_pudge(HASH).startswith("quiet_toggle_off|")


def test_record_of_a_torrent_no_client_has_is_cleared(tmp_path, monkeypatch):
    client = StartableClient({}, state="stoppedUP")
    manager = _manager(tmp_path, monkeypatch, client)
    other = "d" * 40
    manager.note_torrents_paused_by_pudge([other], reason="quiet_startup")
    api = _api(manager, monkeypatch)

    assert api._resume_incomplete_torrent_jobs_impl() == 0
    assert client.started == []
    assert manager.torrent_paused_by_pudge(other) == ""
