"""An episode whose earlier attempt still owns unconfirmed torrent candidates must not
abort the automatic pass, and the candidates must not stay unconfirmed forever."""

from __future__ import annotations

import json
import time

import pytest

from pudge.config import AppConfig
from pudge.download_intents import UnconfirmedCandidatesError
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers.aria2 import Aria2Client
from pudge.providers.qbittorrent import QBittorrentError


def _manager(tmp_path, *, backend="aria2") -> AnimeManager:
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.anilist.enabled = False
    config.nyaa.enabled = True
    config.nyaa.auto_download_current = True
    config.qbittorrent.enabled = backend == "qbittorrent"
    config.aria2.enabled = backend == "aria2"
    manager = AnimeManager(config, log=lambda message: None)
    for media_id, title in ((195516, "Kusuriya no Hitorigoto 3rd Season"), (300, "Other Show")):
        manager.db.upsert_anime(
            LibraryAnime(
                media_id,
                title,
                status="CURRENT",
                media_status="RELEASING",
                progress=1,
                episodes=12,
                next_airing_episode=3,
            )
        )
    return manager


def _release(title: str, hash_: str) -> NyaaRelease:
    return NyaaRelease(
        f"[Group] {title} - 02 [1080p]", "", "", hash_, "1 GiB", 1024**3, 100, 0, 0, True, False
    )


class StoppedAria2:
    """aria2 with its RPC stopped (torrents off): no status, only its own records."""

    def __init__(self, known: set[str] | None = None) -> None:
        self.known = known or set()

    def torrent_status(self, hash_):
        raise QBittorrentError("aria2 RPC is not running")

    def known_offline(self, hash_):
        return hash_ in self.known

    def close(self):
        pass


class StoppedQbittorrent(StoppedAria2):
    known_offline = None  # qBittorrent cannot tell what it holds while it is down


def _stuck(
    manager: AnimeManager, *, age: float = 0.0, state: str = "waiting", backend: str = "aria2"
) -> None:
    """The user's state: an earlier race journaled a candidate that was never confirmed."""
    manager.download_intents.update(
        195516,
        2,
        False,
        state=state,
        backend=backend,
        owned_candidates=[
            {"info_hash": "a" * 40, "title": "AnoZu 02", "backend": backend, "stage": "adding"}
        ],
    )
    if age:
        key = manager.download_intents.key(195516, 2, False)
        payload = json.loads(manager.db.get_state(key))
        payload["unconfirmed_since"] = time.time() - age
        manager.db.set_state(key, json.dumps(payload))


def _auto_pass(manager: AnimeManager, monkeypatch, client) -> tuple[list[tuple[int, int]], list[str]]:
    searched: list[tuple[int, int]] = []
    logged: list[str] = []
    real_info = manager.logger.info

    def info(message, *args, **kwargs):
        logged.append(message % args if args else message)
        return real_info(message, *args, **kwargs)

    monkeypatch.setattr(manager.logger, "info", info)
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    monkeypatch.setattr(manager, "downloads_enabled", lambda: False)  # torrents are off
    monkeypatch.setattr(
        manager,
        "search_releases",
        lambda media_id, *, episode, batch, automatic=False: (
            searched.append((media_id, episode)) or [_release(str(media_id), f"{media_id:040d}")]
        ),
    )
    monkeypatch.setattr(manager, "automatic_release_candidates", lambda releases: list(releases))
    manager.auto_search_current()
    return searched, logged


def test_unconfirmed_intent_skips_only_that_title_while_torrents_are_off(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    _stuck(manager)
    searched, logged = _auto_pass(manager, monkeypatch, StoppedAria2(known={"a" * 40}))
    assert searched == [(300, 2)], "the other title is still searched; the stuck one skips without a search"
    assert "SKIP step=auto_search reason=intent_unconfirmed media_id=195516 episode=2" in logged
    assert manager.download_intents.get(300, 2, False)["state"] == "waiting"
    # The old attempt is left for reconciliation when torrents come back.
    assert manager.download_intents.get(195516, 2, False)["owned_candidates"]


def test_a_concurrent_journal_never_aborts_the_pass(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    original = manager.download_intents.begin

    def begin(media_id, *args, **kwargs):
        if media_id == 195516:
            raise UnconfirmedCandidatesError("another pass owns candidates")
        return original(media_id, *args, **kwargs)

    monkeypatch.setattr(manager.download_intents, "begin", begin)
    searched, logged = _auto_pass(manager, monkeypatch, StoppedAria2())
    assert (300, 2) in searched and manager.download_intents.get(300, 2, False)["state"] == "waiting"
    assert "SKIP step=auto_search reason=intent_unconfirmed media_id=195516 episode=2" in logged


def test_absent_candidate_is_released_after_the_timeout_while_aria2_is_stopped(tmp_path, monkeypatch) -> None:
    manager = _manager(tmp_path)
    _stuck(manager, age=2 * 3600)
    searched, _ = _auto_pass(manager, monkeypatch, StoppedAria2(known=set()))
    assert (195516, 2) in searched, "released: the episode is handled again"
    intent = manager.download_intents.get(195516, 2, False)
    assert not intent.get("owned_candidates") and intent["state"] == "waiting"


@pytest.mark.parametrize(
    "age,client",
    [
        (10 * 60, StoppedAria2(known=set())),  # too recent
        (2 * 3600, StoppedAria2(known={"a" * 40})),  # aria2 still holds it
        (2 * 3600, StoppedQbittorrent()),  # unknown: qBittorrent is down
    ],
)
def test_candidate_that_may_exist_is_kept(tmp_path, monkeypatch, age, client) -> None:
    manager = _manager(tmp_path, backend="qbittorrent" if isinstance(client, StoppedQbittorrent) else "aria2")
    _stuck(manager, age=age, backend="qbittorrent" if isinstance(client, StoppedQbittorrent) else "aria2")
    monkeypatch.setattr(manager, "qbt_client", lambda: client)
    manager.recover_acquisition_attempts()
    assert manager.download_intents.get(195516, 2, False)["owned_candidates"]


def test_reachable_client_confirms_absence_for_an_intent_stuck_in_waiting(tmp_path, monkeypatch) -> None:
    # "waiting" (or "downloading") with owned candidates used to be skipped by recovery.
    manager = _manager(tmp_path)
    _stuck(manager, state="waiting")

    class Running(StoppedAria2):
        def torrent_status(self, hash_):
            return None  # aria2 answers: not there

    monkeypatch.setattr(manager, "qbt_client", lambda: Running())
    manager.recover_acquisition_attempts()
    assert manager.download_intents.get(195516, 2, False)["owned_candidates"] == []
    manager.download_intents.begin(195516, 2, False, [_release("x", "b" * 40)])  # a retry is allowed again


def test_aria2_knows_its_torrents_without_the_rpc(tmp_path) -> None:
    client = Aria2Client(enabled=True, state_dir=tmp_path)
    (tmp_path / "metadata.json").write_text(json.dumps({"abcd": {"info_hash": "F" * 40}}), encoding="utf-8")
    assert client.known_offline("f" * 40) is True
    assert client.known_offline("abcd") is True
    assert client.known_offline("e" * 40) is False
