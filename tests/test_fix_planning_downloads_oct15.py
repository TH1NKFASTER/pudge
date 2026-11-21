"""Planning "search released episodes" for Shouwa Genroku Rakugo Shinjuu (runtime log 2026-10-15).

1. The first season stuck at 0 %. The aria2 race gave the 9-seed HorribleSubs
   batch about 6 s, far too little for a magnet's metadata, and then declared
   a 2-seed pack the winner 2 s after adding it because metadata plus a peer
   connection counted as "alive". Its peers never sent a byte and nothing ever
   moved on. Now only data counts as alive, a candidate without metadata gets
   40 s and one with metadata 20 s; when nothing sends data the candidate with
   peers and the most seeders is kept (not nothing), the others are recorded,
   and a chosen download still at 0 bytes after 10 minutes is replaced by the
   next recorded candidate (never a manual choice, never once a byte exists).
2. The second season was never found. Its only real pack, "[Cleo] ...
   Sukeroku Futatabi-hen | ... [10bit 1080p][HEVC-x265]", has no Batch/01-12
   marker and is under 4 GiB, so it counted as "not a pack"; every episode
   search then found only that pack. Now an unmarked release whose size covers
   every episode of a finished entry, with an exact title of that entry and no
   episode number or special marker, is a season pack for the batch step only.
   Season 1's "(01-13)" batch still never counts for season 2, nor the season 2
   pack for season 1.
3. Every SeaDex lookup failed with "HTTP 301": the address redirects and the
   fetch did not follow it. Now https redirects are followed (a few hops) and
   a redirect to plain http is refused, keeping the stale-cache fallback.
Also: an expired Nyaa search deadline is now logged (step=nyaa.search_deadline).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import DownloadItem, LibraryAnime, NyaaRelease
from pudge.providers import nyaa, seadex
from pudge.providers.nyaa import release_is_safe_batch_candidate, release_title_is_plausible, score_release

MIB = 1024**2
GIB = 1024**3


# --- 1. aria2 race and stall fallback ------------------------------------------------


def _snap(*, total: int = 0, connections: int = 0, downloaded: int = 0, speed: int = 0) -> dict:
    return {
        "status": "active",
        "dlspeed": speed,
        "downloaded": downloaded,
        "progress": downloaded / total if total else 0.0,
        "total_size": total,
        "connections": connections,
    }


def _dead(_age: float) -> dict:
    return _snap()


def _peers_without_data(_age: float) -> dict:
    return _snap(total=GIB, connections=3)


def _slow_magnet(age: float) -> dict:
    """The HorribleSubs batch: metadata after 12 s, data after 18 s."""
    if age < 12:
        return _snap(connections=1 if age > 5 else 0)
    if age < 18:
        return _snap(total=GIB, connections=4)
    return _snap(total=GIB, connections=6, downloaded=2 * MIB, speed=500_000)


def _moving(_age: float) -> dict:
    return _snap(total=GIB, connections=5, downloaded=4 * MIB, speed=800_000)


class _SwarmAria2:
    """aria2 for the race: each hash follows a script of its status by seconds since its add."""

    backend_name = "aria2"

    def __init__(self, clock: dict, scripts: dict) -> None:
        self.clock = clock
        self.scripts = scripts
        self.items: dict[str, DownloadItem] = {}
        self.added_at: dict[str, float] = {}
        self.added: list[str] = []
        self.deleted: list[str] = []

    def close(self) -> None:
        pass

    def ensure_running(self) -> None:
        pass

    def add_release(self, release, *, save_path, category, tags, paused):
        self.added.append(release.info_hash)
        self.added_at[release.info_hash] = self.clock["now"]
        media_id = next((int(tag.split(":", 1)[1]) for tag in tags if tag.startswith("anilist:")), None)
        self.items[release.info_hash] = DownloadItem(
            release.info_hash,
            release.title,
            "active",
            0,
            str(save_path),
            str(save_path),
            media_id=media_id,
            is_batch="series pack" in tags,
            added_on=int(time.time()),
            raw={"backend": "aria2", "_media_id_source": "aria2_metadata"},
        )
        return release.info_hash

    def _status(self, hash_: str) -> dict:
        return self.scripts[hash_](self.clock["now"] - self.added_at[hash_])

    def torrent_status(self, hash_: str):
        return self._status(hash_) if hash_ in self.items else None

    def torrents(self, *, category=""):
        for hash_, item in self.items.items():
            snapshot = self._status(hash_)
            item.progress = snapshot["progress"]
            item.raw.update(
                downloaded=snapshot["downloaded"],
                download_speed=snapshot["dlspeed"],
                total_size=snapshot["total_size"],
                num_connections=snapshot["connections"],
            )
        return list(self.items.values())

    def start(self, _hash: str) -> None:
        pass

    def prioritize(self, _hash: str) -> bool:
        return True

    def set_location(self, *_args) -> None:
        pass

    def set_metadata(self, *_args, **_kwargs) -> None:
        pass

    def delete(self, hash_: str, *, delete_files: bool) -> None:
        self.deleted.append(hash_)
        self.items.pop(hash_, None)


def _release(title: str, info_hash: str, seeders: int, score: float) -> NyaaRelease:
    return NyaaRelease(
        title, "", "", info_hash, "300 MiB", 300 * MIB, seeders, 0, 0, True, False, score=score, is_batch=True
    )


def _aria2_manager(tmp_path: Path, monkeypatch, scripts: dict) -> tuple[AnimeManager, _SwarmAria2, dict]:
    from pudge import manager as manager_module

    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.paths.cache_dir = tmp_path / "cache"
    config.anilist.enabled = False
    config.qbittorrent.enabled = False
    config.aria2.enabled = True
    config.nyaa.torrents_enabled = True
    manager = AnimeManager(config, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(
            20972,
            "Shouwa Genroku Rakugo Shinjuu",
            episodes=13,
            format="TV",
            status="PLANNING",
            media_status="FINISHED",
        )
    )
    clock = {"now": 0.0}  # the race's clock; sleeping advances it
    backend = _SwarmAria2(clock, scripts)
    monkeypatch.setattr(manager, "qbt_client", lambda: backend)
    monkeypatch.setattr(manager, "_ensure_download_space", lambda _size: None)
    monkeypatch.setattr(
        manager_module,
        "time",
        SimpleNamespace(
            **{
                **vars(time),
                "monotonic": lambda: clock["now"],
                "sleep": lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
            }
        ),
    )
    return manager, backend, clock


def test_metadata_and_peers_without_data_is_not_a_race_winner() -> None:
    starting = _snap(total=20 * GIB, connections=3)
    assert AnimeManager._torrent_race_is_alive(starting) is False
    assert AnimeManager._torrent_race_is_starting(starting) is True
    assert AnimeManager._torrent_race_is_alive(_snap(total=GIB, connections=1, speed=1)) is True
    assert AnimeManager._torrent_race_is_alive(_snap(total=GIB, downloaded=300 * 1024)) is True


def test_aria2_race_waits_for_slow_metadata_and_skips_peers_without_data(tmp_path, monkeypatch) -> None:
    horrible = _release(
        "[HorribleSubs] Shouwa Genroku Rakugo Shinjuu (01-13) [1080p] (Batch)", "aaa", 9, 309.9
    )
    bd = _release("Shouwa Genroku Rakugo Shinjuu BD 1080p", "bbb", 2, 296.5)
    manager, backend, _clock = _aria2_manager(
        tmp_path, monkeypatch, {"aaa": _slow_magnet, "bbb": _peers_without_data}
    )

    winner = manager._race_add_candidates(20972, [horrible, bd], episode=None, batch=True)

    # Before: aaa was dropped after the short window and bbb "won" at once.
    assert winner is not None and winner.info_hash == "aaa"
    assert backend.added == ["aaa"] and backend.deleted == []
    intent = manager.download_intents.get(20972, None, True)
    assert intent["state"] == "downloading" and intent["selected_hash"] == "aaa"
    # The untried candidate stays recorded for a later stall.
    assert [item["info_hash"] for item in intent["stall_fallback"]["candidates"]] == ["bbb"]


def test_aria2_race_keeps_best_candidate_with_peers_then_replaces_it_when_it_stalls(
    tmp_path, monkeypatch
) -> None:
    first = _release("[A] Shouwa Genroku Rakugo Shinjuu (01-13) [1080p] (Batch)", "aaa", 9, 300)
    dead = _release("[B] Shouwa Genroku Rakugo Shinjuu (01-13) [1080p] (Batch)", "bbb", 5, 290)
    third = _release("[C] Shouwa Genroku Rakugo Shinjuu (01-13) [720p] (Batch)", "ccc", 2, 280)
    scripts = {"aaa": _peers_without_data, "bbb": _dead, "ccc": _peers_without_data}
    manager, backend, _clock = _aria2_manager(tmp_path, monkeypatch, scripts)

    winner = manager._race_add_candidates(20972, [first, dead, third], episode=None, batch=True)

    # Nobody sent data: the one with peers and the most seeders comes back
    # (before: aaa "won" at once and nothing else was ever recorded).
    assert winner is not None and winner.info_hash == "aaa"
    assert backend.added == ["aaa", "bbb", "ccc", "aaa"]
    assert backend.deleted == ["aaa", "bbb", "ccc"]
    intent = manager.download_intents.get(20972, None, True)
    assert intent["state"] == "downloading" and intent["selected_hash"] == "aaa"
    assert [item["info_hash"] for item in intent["stall_fallback"]["candidates"]] == ["ccc"]

    # Not yet ten minutes, or bytes arrived: nothing is touched.
    assert manager.replace_stalled_aria2_downloads(now=time.time() + 60) == 0
    scripts["aaa"] = _moving
    assert manager.replace_stalled_aria2_downloads(now=time.time() + 700) == 0
    assert backend.deleted == ["aaa", "bbb", "ccc"]

    # Still 0 bytes after ten minutes: the next recorded candidate takes over.
    scripts["aaa"] = _peers_without_data
    scripts["ccc"] = _moving
    assert manager.replace_stalled_aria2_downloads(now=time.time() + 700) == 1
    assert backend.deleted == ["aaa", "bbb", "ccc", "aaa"] and backend.added[-1] == "ccc"
    assert set(backend.items) == {"ccc"}
    intent = manager.download_intents.get(20972, None, True)
    assert intent["state"] == "downloading" and intent["selected_hash"] == "ccc"
    # The stalled release is never recorded again: no ping-pong.
    assert "stall_fallback" not in intent


def test_stall_fallback_never_replaces_a_manual_choice(tmp_path, monkeypatch) -> None:
    first = _release("[A] Shouwa Genroku Rakugo Shinjuu (01-13) [1080p] (Batch)", "aaa", 9, 300)
    third = _release("[C] Shouwa Genroku Rakugo Shinjuu (01-13) [720p] (Batch)", "ccc", 2, 280)
    manual = _release("[M] Shouwa Genroku Rakugo Shinjuu (01-13) [1080p] (Batch)", "mmm", 1, 100)
    scripts = {"aaa": _peers_without_data, "ccc": _peers_without_data, "mmm": _peers_without_data}
    manager, backend, _clock = _aria2_manager(tmp_path, monkeypatch, scripts)
    manager._race_add_candidates(20972, [first, third], episode=None, batch=True)
    assert manager.download_intents.get(20972, None, True)["stall_fallback"]

    assert manager.add_release(20972, manual, episode=None, batch=True, manual=True) is True

    assert "stall_fallback" not in manager.download_intents.get(20972, None, True)
    deleted = list(backend.deleted)
    assert manager.replace_stalled_aria2_downloads(now=time.time() + 700) == 0
    assert backend.deleted == deleted and {"aaa", "mmm"} <= set(backend.items)


# --- 2. season pack without an explicit marker ---------------------------------------

SEASON_1_TITLE = "Shouwa Genroku Rakugo Shinjuu"
SEASON_2_TITLE = "Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi-hen"
CLEO = (
    "[Cleo] Shouwa Genroku Rakugo Shinjuu: Sukeroku Futatabi-hen | "
    "Descending Stories: Showa Genroku Rakugo Shinju [10bit 1080p][HEVC-x265]"
)
HORRIBLE_S1 = "[HorribleSubs] Shouwa Genroku Rakugo Shinjuu (01-13) [1080p] (Batch)"


def _season_2(**changes) -> LibraryAnime:
    values = dict(
        media_id=21733,
        title=SEASON_2_TITLE,
        titles=["Descending Stories: Showa Genroku Rakugo Shinju 2"],
        episodes=12,
        format="TV",
        media_status="FINISHED",
        relations=[{"relation_type": "PREQUEL", "media_id": 20972, "title": SEASON_1_TITLE}],
    )
    values.update(changes)
    return LibraryAnime(**values)


def _season_1() -> LibraryAnime:
    return LibraryAnime(
        20972,
        SEASON_1_TITLE,
        titles=["Descending Stories: Showa Genroku Rakugo Shinju"],
        episodes=13,
        format="TV",
        media_status="FINISHED",
        relations=[{"relation_type": "SEQUEL", "media_id": 21733, "title": SEASON_2_TITLE}],
    )


def _nyaa_release(title: str, size_bytes: int, seeders: int = 31) -> NyaaRelease:
    return NyaaRelease(
        title,
        "https://releases.invalid/view/1",
        "",
        "c" * 40,
        f"{size_bytes / GIB:.1f} GiB",
        size_bytes,
        seeders,
        2,
        900,
        False,
        False,
        published="Mon, 01 Jan 2018 00:00:00 -0000",
        is_batch=bool(nyaa.BATCH_RE.search(title)),
        group=title[1:].split("]", 1)[0] if title.startswith("[") else "",
    )


def _score(release: NyaaRelease, anime: LibraryAnime, *, episode: int | None = None) -> NyaaRelease:
    cfg = AppConfig().nyaa
    return score_release(
        release,
        anime,
        episode=episode,
        batch=episode is None,
        trusted_groups=cfg.trusted_groups,
        preferred_groups=cfg.preferred_groups,
        blocked_groups=cfg.blocked_groups,
        preferred_resolution=cfg.preferred_resolution,
        min_seeders=cfg.min_seeders,
        target_episode_min_bytes=cfg.episode_min_size_mb * MIB,
        target_episode_max_bytes=cfg.episode_max_size_mb * MIB,
        preferred_video_codecs=cfg.preferred_video_codecs,
        preferred_sources=cfg.preferred_sources,
        alternative_episodes=(14,) if episode == 1 else (),
    )


def test_unmarked_finished_season_pack_is_offered_by_the_batch_step_only() -> None:
    anime = _season_2()
    pack = _nyaa_release(CLEO, int(3.3 * GIB))
    assert release_title_is_plausible(anime, CLEO, (), (SEASON_1_TITLE,), ())

    ranked = _score(pack, anime)

    # Before: "not-a-pack", far below the score floor, and rejected as a batch.
    assert "season-sized-pack" in ranked.reasons and "not-a-pack" not in ranked.reasons
    assert ranked.score >= AppConfig().nyaa.min_release_score
    assert release_is_safe_batch_candidate(anime, ranked, negative_titles=(SEASON_1_TITLE,))
    # Episode searches still never pick the whole pack (it is added once, by the batch step).
    per_episode = _score(pack, anime, episode=1)
    assert "episode-not-specified" in per_episode.reasons
    assert not object.__new__(AnimeManager)._release_has_safe_episode_identity(per_episode)


def test_season_sized_pack_needs_all_the_evidence() -> None:
    anime = _season_2()

    def safe(title: str, size: int, entry: LibraryAnime = anime) -> bool:
        return release_is_safe_batch_candidate(
            entry, _score(_nyaa_release(title, size), entry), negative_titles=(SEASON_1_TITLE,)
        )

    assert safe(CLEO, int(3.3 * GIB))
    assert not safe(CLEO, int(2.5 * GIB))  # under 12 x the per-episode minimum
    assert not safe(CLEO, int(3.3 * GIB), _season_2(media_status="RELEASING"))
    assert not safe(CLEO.replace("[10bit", "Recap [10bit"), int(3.3 * GIB))
    assert not safe(CLEO.replace("[10bit", "- 05 [10bit"), int(3.3 * GIB))


def test_season_packs_stay_with_their_own_season() -> None:
    season_2 = _season_2()
    season_1 = _season_1()
    s1_batch = _score(_nyaa_release(HORRIBLE_S1, int(3.3 * GIB), 9), season_2)
    # Season 1's "(01-13)" batch is not a pack for season 2 (12 episodes, offset 13)...
    assert not release_title_is_plausible(season_2, HORRIBLE_S1, (), (SEASON_1_TITLE,), ())
    assert not release_is_safe_batch_candidate(season_2, s1_batch, negative_titles=(SEASON_1_TITLE,))
    # ...and the unmarked season 2 pack is not one for season 1.
    s2_pack = _score(_nyaa_release(CLEO, int(3.3 * GIB)), season_1)
    assert not release_title_is_plausible(season_1, CLEO, (), (SEASON_2_TITLE,), ())
    assert not release_is_safe_batch_candidate(season_1, s2_pack, negative_titles=(SEASON_2_TITLE,))


# --- 3. SeaDex redirect ---------------------------------------------------------------


def _seadex_payload(anilist_id: int) -> dict:
    return {
        "items": [
            {
                "id": "abc",
                "alID": anilist_id,
                "expand": {
                    "trs": [
                        {
                            "tracker": "Nyaa",
                            "infoHash": "d" * 40,
                            "url": "https://nyaa.si/view/123",
                            "releaseGroup": "Cleo",
                            "isBest": True,
                        }
                    ]
                },
            }
        ]
    }


def test_seadex_lookup_follows_an_https_redirect(tmp_path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "releases.moe":
            moved = request.url.copy_with(host="sea.releases.moe")
            return httpx.Response(301, headers={"Location": str(moved)})
        return httpx.Response(200, json=_seadex_payload(21733))

    transport = httpx.MockTransport(handler)
    client = seadex.SeaDexClient(
        tmp_path,
        base_url="https://releases.moe",
        fetch=lambda url, params, timeout: seadex._http_fetch(url, params, timeout, transport=transport),
    )

    rec = client.recommendations(21733)

    # Before: status=unavailable error='RuntimeError: HTTP 301'.
    assert rec.status == "found" and not rec.stale and rec.error == ""
    assert rec.preferred_hashes == frozenset({"d" * 40})
    assert [httpx.URL(url).host for url in seen] == ["releases.moe", "sea.releases.moe"]
    assert "alID%3D21733" in seen[1] or "alID=21733" in seen[1]


def test_seadex_refuses_a_redirect_to_http_and_keeps_the_stale_cache(tmp_path) -> None:
    now = {"t": 1_000_000.0}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.scheme == "http":
            return httpx.Response(200, json=_seadex_payload(21733))
        return httpx.Response(301, headers={"Location": str(request.url.copy_with(scheme="http"))})

    transport = httpx.MockTransport(handler)
    client = seadex.SeaDexClient(
        tmp_path,
        base_url="https://releases.moe",
        fetch=lambda url, params, timeout: seadex._http_fetch(url, params, timeout, transport=transport),
        now=lambda: now["t"],
    )
    cached = seadex.parse_entries(21733, _seadex_payload(21733), fetched_at=now["t"] - 2 * 86400)
    client.root.mkdir(parents=True)
    (client.root / "21733.json").write_text(json.dumps(seadex._to_json(cached)), encoding="utf-8")

    rec = client.recommendations(21733)

    assert rec.status == "found" and rec.stale and "redirect" in rec.error
    assert all(httpx.URL(url).scheme == "https" for url in seen)


# --- Nyaa search deadline --------------------------------------------------------------


def test_expired_nyaa_search_deadline_is_logged(monkeypatch) -> None:
    release = threading.Event()
    messages: list[str] = []

    class Client:
        base_url = "https://releases.invalid"
        category = "1_2"
        proxy_mode = "direct"
        proxy_url = ""

        def search(self, _query: str):
            release.wait(5)
            return []

    monkeypatch.setattr(nyaa, "MANUAL_SEARCH_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(
        nyaa, "_LOGGER", SimpleNamespace(warning=lambda message, *args: messages.append(message % args))
    )
    try:
        _found, _errors, exhausted = nyaa._manual_parallel_search(
            Client(), [f"deadline probe {index}" for index in range(5)]
        )
    finally:
        release.set()

    assert exhausted
    assert messages == ["WARN step=nyaa.search_deadline queries=5 cached=0 done=0 pending=5 deadline_s=0"]
