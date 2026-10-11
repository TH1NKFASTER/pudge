"""Audit decisions, patch B: numbering, releases, providers and the background agent."""

from __future__ import annotations

import dataclasses
import json
import logging
import subprocess
import sys
import time
from types import SimpleNamespace

import httpx
import pytest

from pudge import episode_numbering as en
from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers import anilist, nyaa
from pudge.providers.aria2 import Aria2Client
from pudge.providers.jimaku import JimakuClient, JimakuFile
from pudge.providers.qbittorrent import QBittorrentClient
from pudge.release_parser import parse_release_name

LOG = logging.getLogger("patch-b")


def _manager(tmp_path, *, backend="off"):
    from test_v0730_acquisition_safety import manager_for

    return manager_for(tmp_path, backend=backend)


def _stage(manager, media_id=300001):
    anime = LibraryAnime(
        media_id,
        "Kimi no Nagai Monogatari - 2nd STAGE",
        titles=["Kimi no Nagai Monogatari"],
        episodes=11,
        format="ONA",
        season_year=2026,
    )
    manager.db.upsert_anime(anime)
    graph = {
        "root_id": media_id,
        "nodes": [
            {
                "media_id": media_id - 1,
                "title": "Kimi no Nagai Monogatari - 1st STAGE",
                "episodes": 1,
                "format": "ONA",
                "season_year": 2026,
            },
            {
                "media_id": media_id,
                "title": anime.title,
                "episodes": 11,
                "format": "ONA",
                "season_year": 2026,
            },
        ],
        "edges": [{"source": media_id - 1, "target": media_id, "relation_type": "SEQUEL"}],
    }
    manager.db.store_relation_graph(graph, refreshed_at=1, next_refresh_at=9999999999)
    return anime


def _release(title, *, trusted=False, group="NewSubs", seeders=10):
    return NyaaRelease(
        title, "", "", title[-12:], "1 GiB", 1024**3, seeders, 0, 0, trusted, False, group=group
    )


# ------------------------------------------------------------ 14. learned stage rules


def test_manual_choice_learns_the_uploaders_stage_numbering(tmp_path):
    manager = _manager(tmp_path)
    anime = _stage(manager)
    title = "[NewSubs] Kimi no Nagai Monogatari - 03 [1080p]"
    context = en.resolve_episode_numbering(anime, 2, manager.config, LOG, db=manager.db, allow_network=False)
    match = en.match_release_episode(anime, parse_release_name(title), 2, context, trusted_source=False)
    assert match.status == "ambiguous" and match.proposed_media_episode == 2 and match.proposed_offset == 1
    # Automatic paths never learn.
    with pytest.raises(Exception):
        manager._validated_release_identity(anime, _release(title), 2, False)
    validated = manager._validated_release_identity(anime, _release(title), 2, False, manual=True)
    assert validated.mapped_media_episode == 2 and validated.numbering_status == "resolved"
    assert [
        (row["release_group"], row["season_key"], row["episode_offset"])
        for row in manager.db.release_numbering_rules(anime.media_id)
    ] == [("newsubs", "bare", 1)]
    # The next episode from the same uploader resolves automatically.
    context = en.resolve_episode_numbering(anime, 3, manager.config, LOG, db=manager.db, allow_network=False)
    later = en.match_release_episode(
        anime,
        parse_release_name("[NewSubs] Kimi no Nagai Monogatari - 04 [1080p]"),
        3,
        context,
        trusted_source=False,
    )
    assert later.status == "resolved" and later.mapped_media_episode == 3
    # Another uploader is still a proposal; forgetting the rule restores ambiguity.
    other = en.match_release_episode(
        anime,
        parse_release_name("[Other] Kimi no Nagai Monogatari - 04 [1080p]"),
        3,
        context,
        trusted_source=True,
    )
    assert other.status == "ambiguous"
    assert manager.db.forget_release_numbering_rules(anime.media_id) == 1
    context = en.resolve_episode_numbering(anime, 3, manager.config, LOG, db=manager.db, allow_network=False)
    assert (
        en.match_release_episode(
            anime,
            parse_release_name("[NewSubs] Kimi no Nagai Monogatari - 04 [1080p]"),
            3,
            context,
            trusted_source=False,
        ).status
        == "ambiguous"
    )


def test_manual_download_reports_the_learned_rule_and_rules_can_be_forgotten(tmp_path):
    from pudge.web_app import WebAppApi

    manager = _manager(tmp_path)
    anime = _stage(manager)
    api = WebAppApi.__new__(WebAppApi)
    api.manager = manager
    api.logger = LOG
    api._torrent_changed = lambda *_args: None
    # The real learning step of an add, without a torrent client.
    manager.add_release = lambda media_id, release, *, episode, batch, manual=False: bool(
        manager._validated_release_identity(anime, release, episode, batch, manual=manual)
    )
    release = json.loads(
        json.dumps(dataclasses.asdict(_release("[NewSubs] Kimi no Nagai Monogatari - 03 [1080p]")))
    )
    first = api.add_release(anime.media_id, 2, False, release)
    assert first["learned_numbering_rule"] == {
        "group": "newsubs",
        "season": "bare",
        "offset": 1,
    }  # a window choice is manual
    again = api.add_release(anime.media_id, 2, False, release)
    assert again["ok"] is True and "learned_numbering_rule" not in again  # nothing new learned
    assert [rule["group"] for rule in api.release_numbering_rules(anime.media_id)] == ["newsubs"]
    assert api.forget_release_numbering_rules(anime.media_id) == {"ok": True, "removed": 1}
    assert api.release_numbering_rules(anime.media_id) == []


STAGE = {
    "media_id": 300001,
    "title": "Kimi no Nagai Monogatari - 2nd STAGE",
    "list_status": "PLANNING",
    "media_status": "FINISHED",
    "episodes": 11,
    "progress": 0,
}
PLAIN = {
    "media_id": 5,
    "title": "Ordinary Show",
    "list_status": "PLANNING",
    "media_status": "FINISHED",
    "episodes": 12,
    "progress": 0,
}
RULE = {
    "group": "newsubs",
    "season": "bare",
    "offset": 1,
    "example": "[NewSubs] Kimi no Nagai Monogatari - 03 [1080p]",
}


def test_forget_rules_menu_and_learned_toast(page):
    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('planned');
            await __tick(20);
            const menu = async id => {
              await __fire(`#plannedContent > [data-media-id="${id}"] .anime-title`, 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
              return [...document.querySelectorAll('#contextMenu [data-context-action]')].map(b => b.dataset.contextAction);
            };
            const out = {plain: (await menu(5)).includes('forget-numbering-rules')};
            out.stage = (await menu(300001)).includes('forget-numbering-rules');
            out.label = __text('#contextMenu [data-context-action="forget-numbering-rules"]') === PudgeI18n.t('ui.forgetReleaseNumberingRules');
            await __click('#contextMenu [data-context-action="forget-numbering-rules"]');
            await __tick(10);
            out.question = $('pudgeConfirmMessage').textContent === t('uiC.numbering.forgetConfirm', {rules: '• ' + numberingRuleText(RULE)});
            await __click('#pudgeConfirmBackdrop [data-pudge-confirm=ok]');
            await __tick(10);
            out.forgotten = __toasts().at(-1) === PudgeI18n.t('ui.releaseNumberingRulesForgotten');
            await menu(300001);                                                      // nothing remembered any more
            await __click('#contextMenu [data-context-action="forget-numbering-rules"]');
            await __tick(10);
            out.none = __toasts().at(-1) === PudgeI18n.t('ui.noReleaseNumberingRulesAre');
            // A release chosen by hand teaches a rule: the window says so after the "added" toast.
            await openRelease(300001, 3, false);
            await __click('#modalBody [data-action="download-release"]');
            await __tick(3000);
            out.learned = __toasts().includes(t('uiC.numbering.remembered', {rule: numberingRuleText(RULE)}));
            return out;
            """.replace("RULE", json.dumps(RULE)),
            lang=lang,
            state={"planned": [STAGE, PLAIN]},
            api={
                "forget_release_numbering_rules": {"ok": True, "removed": 1},
                "search_releases": [{"title": RULE["example"]}],
                "add_release": {
                    "ok": True,
                    "added": True,
                    "already_downloading": False,
                    "learned_numbering_rule": RULE,
                },
            },
            before=f"let asked = 0; __api.release_numbering_rules = async () => (asked++ ? [] : [{json.dumps(RULE)}]);",
        )
        assert result.value == {
            "plain": False,
            "stage": True,
            "label": True,
            "question": True,
            "forgotten": True,
            "none": True,
            "learned": True,
        }
        assert result.calls_of("forget_release_numbering_rules") == [[300001]]


# --------------------------------------------------------------- 15. conflict


def test_conflict_keeps_an_exact_season_local_match():
    anime = LibraryAnime(5001, "Show Part 2", episodes=12, format="TV")
    context = en.EpisodeNumbering(3, 3, 0, (), (), (5001,), "local", status="conflict")
    exact = en.match_release_episode(
        anime, parse_release_name("[Group] Show Part 2 - 03 [1080p]"), 3, context
    )
    assert exact.status == "resolved" and exact.mapped_media_episode == 3
    alias = en.match_release_episode(
        anime, parse_release_name("[Group] Show Part 2 - 15 [1080p]"), 3, context, alternative_episodes=(15,)
    )
    assert alias.status == "conflict"


# ------------------------------------------------------------ 16. offset-0 cache


def test_found_offset_zero_is_reused_for_a_day(tmp_path, monkeypatch):
    config = AppConfig()
    config.paths.cache_dir = tmp_path / "cache"
    config.library.database_path = tmp_path / "db.sqlite3"
    config.anilist.enabled = True
    anime = LibraryAnime(4242, "Fresh Show", episodes=12, format="TV")
    calls = []

    class Client:
        def __init__(self, *_a, **_k):
            pass

        def absolute_episode_number(self, _anime, episode):
            calls.append(episode)
            return episode, [
                SimpleNamespace(id=4242, titles=["Fresh Show"], synonyms=[], format="TV", episodes=12)
            ]

        def close(self):
            pass

    monkeypatch.setattr(en, "AniListClient", Client)
    first = en.resolve_episode_numbering(anime, 3, config, LOG, allow_network=True)
    second = en.resolve_episode_numbering(anime, 4, config, LOG, allow_network=True)
    assert first.offset == second.offset == 0 and calls == [3]
    stale = time.time() - 25 * 3600
    for path in en._cache_paths(config, 4242):
        import os

        os.utime(path, (stale, stale))
    en.resolve_episode_numbering(anime, 5, config, LOG, allow_network=True)
    assert calls == [3, 5]


def test_numbering_failures_are_logged(caplog, tmp_path):
    config = AppConfig()
    config.paths.cache_dir = tmp_path

    class BrokenDb:
        def relation_graph_for_media(self, _media_id):
            raise RuntimeError("database is locked")

    with caplog.at_level(logging.WARNING):
        assert en._graph_from_db(config, 1, BrokenDb(), LOG) is None
    assert "step=episode_numbering.graph" in caplog.text


# ---------------------------------------------------------------- 18. seeders


def test_mirror_without_seeders_is_unknown_not_dead():
    rss = """<?xml version="1.0"?><rss xmlns:nyaa="https://nyaa.net/xmlns/nyaa"><channel>
      <item><title>[Group] Show - 03 [1080p].mkv</title><link>https://nyaa.net/download/1.torrent</link><guid>https://nyaa.net/view/1</guid>
      <nyaa:infoHash>abc</nyaa:infoHash><nyaa:size>1.0 GiB</nyaa:size></item>
      <item><title>[Group] Show - 03 [1080p] v2.mkv</title><link>https://nyaa.net/download/2.torrent</link><guid>https://nyaa.net/view/2</guid>
      <nyaa:infoHash>def</nyaa:infoHash><nyaa:size>1.0 GiB</nyaa:size><nyaa:seeders>0</nyaa:seeders></item>
    </channel></rss>"""
    unknown, dead = nyaa.parse_rss(rss)
    assert unknown.seeders_known is False and dead.seeders_known is True
    anime = LibraryAnime(1, "Show", episodes=12, format="TV")
    kwargs = dict(
        episode=3,
        batch=False,
        trusted_groups=[],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=1,
        target_episode_max_bytes=4 * 1024**3,
    )
    scored_unknown = nyaa.score_release(unknown, anime, **kwargs)
    scored_dead = nyaa.score_release(dead, anime, **kwargs)
    assert "seeders-unknown" in scored_unknown.reasons and "no-seeders" in scored_dead.reasons
    assert scored_unknown.score > scored_dead.score + 40
    manager = SimpleNamespace(
        config=SimpleNamespace(nyaa=SimpleNamespace(min_seeders=1, trusted_groups=[])), logger=LOG
    )
    from pudge.manager import AnimeManager

    assert AnimeManager._release_seed_requirement_met(manager, scored_unknown)
    assert not AnimeManager._release_seed_requirement_met(manager, scored_dead)


# ------------------------------------------------------- 19. manual Nyaa search


def test_manual_search_runs_queries_in_parallel_and_caches_them(monkeypatch):
    nyaa._MANUAL_QUERY_CACHE.clear()
    client = nyaa.NyaaClient("https://nyaa.si")
    seen = []
    active = {"now": 0, "max": 0}
    import threading

    lock = threading.Lock()

    def search(query, **_kwargs):
        with lock:
            seen.append(query)
            active["now"] += 1
            active["max"] = max(active["max"], active["now"])
        time.sleep(0.05)
        with lock:
            active["now"] -= 1
        return [_release(f"[Group] Show - 03 [1080p] {query}")] if query.endswith(" 03") else []

    monkeypatch.setattr(client, "search", search)
    queries = [f"Show {suffix}" for suffix in ("03", "3", "E03", "S01E03", "A", "B")]
    found, errors, exhausted = nyaa._manual_parallel_search(client, queries)
    assert not errors and not exhausted and active["max"] == nyaa.MANUAL_SEARCH_WORKERS
    assert sorted(found) == sorted(queries) and len(found["Show 03"]) == 1
    seen.clear()
    again, _errors, _ = nyaa._manual_parallel_search(client, queries)
    assert seen == [] and again["Show 03"][0].title == found["Show 03"][0].title
    nyaa._MANUAL_QUERY_CACHE.clear()


def test_manual_search_stops_at_the_deadline(monkeypatch):
    nyaa._MANUAL_QUERY_CACHE.clear()
    client = nyaa.NyaaClient("https://nyaa.si")
    monkeypatch.setattr(nyaa, "MANUAL_SEARCH_DEADLINE_SECONDS", 0.2)
    monkeypatch.setattr(client, "search", lambda query, **_k: time.sleep(0.5) or [])
    started = time.monotonic()
    _found, _errors, exhausted = nyaa._manual_parallel_search(client, [f"q{n}" for n in range(9)])
    assert exhausted and time.monotonic() - started < 0.45
    nyaa._MANUAL_QUERY_CACHE.clear()


# ---------------------------------------------------------- 20. AniList 429


def test_anilist_429_waits_retry_after_and_is_shared_on_disk(tmp_path, monkeypatch):
    anilist.configure_rate_limit_dir(tmp_path)
    monkeypatch.setattr(anilist, "_PROCESS_COOLDOWN_UNTIL", 0.0)
    sleeps = []
    monkeypatch.setattr(anilist.time, "sleep", lambda seconds: sleeps.append(round(seconds)))
    responses = iter(
        [
            httpx.Response(
                429, headers={"Retry-After": "7"}, json={"errors": [{"message": "Too Many Requests."}]}
            ),
            httpx.Response(
                200,
                headers={"X-RateLimit-Limit": "90", "X-RateLimit-Remaining": "60"},
                json={"data": {"Viewer": {"id": 1}}},
            ),
        ]
    )
    client = anilist.AniListClient("https://graphql.anilist.co")
    client.client = httpx.Client(transport=httpx.MockTransport(lambda request: next(responses)))
    try:
        assert client.viewer() == {"id": 1}
        assert 7 in sleeps
        marker = json.loads((tmp_path / "anilist-api" / "rate-limit.json").read_text())
        assert marker["status"] == 429 and marker["until"] > time.time() - 1
        # Another process sees a long cool-down and fails fast instead of hammering.
        (tmp_path / "anilist-api" / "rate-limit.json").write_text(json.dumps({"until": time.time() + 600}))
        with pytest.raises(anilist.AniListHTTPError, match="rate limited"):
            client.viewer()
    finally:
        anilist.configure_rate_limit_dir(None)
        client.close()


def test_anilist_slows_down_when_few_requests_are_left(monkeypatch):
    sleeps = []
    monkeypatch.setattr(anilist.time, "sleep", lambda seconds: sleeps.append(seconds))
    anilist._pace_after(SimpleNamespace(headers={"X-RateLimit-Limit": "30", "X-RateLimit-Remaining": "3"}))
    anilist._pace_after(SimpleNamespace(headers={"X-RateLimit-Limit": "90", "X-RateLimit-Remaining": "40"}))
    assert sleeps == [2.0]


# ------------------------------------------------------------- 21. Jimaku


def test_jimaku_full_list_first_server_filter_only_when_sparse():
    def file(name):
        return JimakuFile(url=f"https://jimaku.test/{name}", name=name, size=1, last_modified="")

    class Client:
        def __init__(self, entry):
            self.entry, self.calls = entry, []

        def files(self, _entry_id, episode):
            self.calls.append(episode)
            return list(self.entry) if episode is None else []

    rich = Client([file(f"Show - 05 [{source}].srt") for source in ("A", "B", "C")] + [file("Show - 06.srt")])
    assert len(JimakuClient.files_for_episode(rich, 1, 5)) == 3 and rich.calls == [None]
    sparse = Client([file("Show - 05.srt"), file("Show - 06.srt")])
    assert [item.name for item in JimakuClient.files_for_episode(sparse, 1, 5)] == ["Show - 05.srt"]
    assert sparse.calls == [None, 5]


# ----------------------------------------------------------- 22/23. qBittorrent


def test_qbittorrent_session_relogs_in_once_after_403():
    state = {"logins": 0, "sid": "old"}

    def handler(request):
        if request.url.path == "/api/v2/auth/login":
            state["logins"] += 1
            state["sid"] = f"sid{state['logins']}"
            return httpx.Response(200, text="Ok.", headers={"set-cookie": f"SID={state['sid']}; path=/"})
        if (
            request.headers.get("cookie") != f"SID={state['sid']}"
            or state["sid"] == "sid1"
            and state.get("expire")
        ):
            state.pop("expire", None)
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, text="v5.0.0")

    client = QBittorrentClient("http://127.0.0.1:1", "user", "pass", auto_start_app=False)
    client.client.close()
    client.client = client._new_client(client.base_url)
    client.client._transport = httpx.MockTransport(handler)
    client.login()
    assert client.client.get("/api/v2/app/version").text == "v5.0.0"
    state["expire"] = True  # the Web UI forgot the session
    assert client.client.get("/api/v2/app/version").text == "v5.0.0"
    assert state["logins"] == 2
    client.close_shared()


def test_manager_reuses_one_qbittorrent_client_and_only_add_paths_may_launch_it(tmp_path):
    manager = _manager(tmp_path, backend="qbittorrent")
    first = manager.qbt_client()
    first.close()  # callers' close() keeps the shared session
    assert manager.qbt_client() is first and manager.torrent_clients()[0][1] is first
    assert first.start_policy() is False
    with manager.allow_torrent_app_start():
        assert first.start_policy() is True

    # Every way of adding a torrent may launch a closed qBittorrent while it runs (and only then).
    class Stop(BaseException):
        pass

    seen = {}

    def probe(name):
        def record(*_args, **_kwargs):
            seen[name] = first.start_policy()
            raise Stop

        return record

    manager.downloads_enabled = probe("add_release")
    manager.ensure_torrent_backend_ready = probe("journaled single add")
    manager.release_telemetry = SimpleNamespace(deadlines=probe("candidate race"))
    manager.download_intents = SimpleNamespace(recoverable=probe("recovery"))
    release = _release("[Group] Example Show - 01 [1080p]")
    for add in (
        lambda: manager.add_release(77, release, episode=1, batch=False),
        lambda: manager._add_journaled_single_candidate(
            77, release, episode=1, batch=False, backend="qbittorrent", intent_revision=1
        ),
        lambda: manager._race_add_candidates_impl(77, [release], episode=1, batch=False),
        manager.recover_acquisition_attempts,
    ):
        with pytest.raises(Stop):
            add()
    assert seen == {
        "add_release": True,
        "journaled single add": True,
        "candidate race": True,
        "recovery": True,
    }
    assert first.start_policy() is False
    manager.config.qbittorrent.base_url = "http://127.0.0.1:9"
    assert manager.qbt_client() is not first


# ------------------------------------------------------------- 24. agent


def test_agent_skips_manager_and_keychain_when_nothing_is_due(tmp_path, monkeypatch):
    from pudge import agent

    config = AppConfig()
    config.library.database_path = tmp_path / "db.sqlite3"
    config.anilist.enabled = False
    Database(config.library.database_path).set_state("agent_last_run", str(time.time()))
    calls = []
    monkeypatch.setattr(agent, "load_config", lambda path, **kwargs: calls.append(kwargs) or config)
    monkeypatch.setattr(
        agent, "AnimeManager", lambda _config: (_ for _ in ()).throw(AssertionError("no manager"))
    )
    assert agent.main(["--scheduled", "--config", str(tmp_path / "config.toml")]) == 0
    assert calls == [{"resolve_secrets": False}]
    # A scheduled run with nothing due does not even load the manager (a heavy import):
    # the same run in a fresh interpreter.
    config_path = tmp_path / "config.toml"
    write_config(config, config_path)
    code = (
        "import sys; from pudge import agent; "
        f"code = agent.main(['--scheduled', '--config', {str(config_path)!r}]); "
        "print(code, 'pudge.manager' in sys.modules)"
    )
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert run.stdout.split()[-2:] == ["0", "False"], run.stderr[-2000:]


# --------------------------------------------- planning after noon, aria2 control file


def test_entry_added_on_premiere_day_after_noon_utc_is_searched(tmp_path):
    from test_planned_release_discovery import service

    anime = LibraryAnime(11, "Day one", status="PLANNING", start_date="2030-01-01")
    svc, manager = service(tmp_path, anime)
    noon = 1893499200.0  # 2030-01-01 12:00 UTC, the date-only premiere guess
    result = svc.tick(now=noon + 5 * 3600)
    assert result["armed"] == 1 and result["searched"] == 1
    # A premiere older than a day is still grandfathered.
    old = LibraryAnime(12, "Old", status="PLANNING", start_date="2029-12-30")
    svc, manager = service(tmp_path, old)
    assert svc.tick(now=noon + 5 * 3600) == {"armed": 0, "searched": 0, "found": 0}


def test_aria2_orphan_with_control_file_is_not_complete(tmp_path):
    save = tmp_path / "dl"
    save.mkdir()
    video = save / "[Group] Show - 03.mkv"
    video.write_bytes(b"x" * 1000)
    client = Aria2Client(state_dir=tmp_path / "state", auto_start=False)
    meta = {
        "save_path": str(save),
        "title": video.name,
        "media_id": 7,
        "episode": 3,
        "expected_size_bytes": 1000,
        "info_hash": "abc",
    }
    assert client._orphaned_completed_item("gid", meta) is not None
    (save / (video.name + ".aria2")).write_bytes(b"")
    assert client._orphaned_completed_item("gid", meta) is None
