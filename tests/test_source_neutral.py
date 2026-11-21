"""Pudge ships with no release sources; the user adds them in Settings → Sources.

What went wrong before: the app had built-in addresses of a torrent search site, its
two mirrors, a weekly releases feed, a public release feed and a release
recommendations API, and the window, README and config examples named those sites.
Google Translate (an unofficial web endpoint) was used without being asked for.

What happens now:
* The addresses live in ``sources.json`` next to config.toml (pudge/sources.py):
  validated (http/https only, trailing "/" and whitespace removed, at most 10
  mirrors, duplicates dropped, unknown keys ignored), saved atomically, imported
  (all fields or only the empty ones) and exported. An older config.toml's
  ``nyaa.base_url`` moves there once; config.toml no longer gets it.
* With a source the behaviour is the same as before: the same RSS queries, mirror
  failover, weekly feed, release feed and recommendations, all from sources.json.
  The format's RSS extension namespace is accepted on any host.
* Without one an interactive search says "Add a release source in Settings →
  Sources." (no network error), and automatic passes (missing episodes, upgrades,
  Planning discovery, Light Novel volumes, feed polling) skip with one INFO line.
* Settings has a Sources group (fields, Import…, Export…); the release search shows
  a hint when nothing is set.
* Google Translate is a setting: off for new installs, on for installs that stored
  Light Novel settings before. Off means no request to Google, not even as a
  fallback; without the LLM a translation then asks to turn one of them on.
* Guards: no address outside an allow-list and no site name in the shipped files.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import tomllib
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

import source_policy
from sources_support import (
    MIRRORS,
    RECOMMENDATIONS,
    RELEASE_FEED,
    RELEASE_SEARCH,
    WEEKLY_RSS,
    configure_sources,
    release_search_client,
    sample_sources,
)

from pudge.config import AppConfig, load_config, write_config
from pudge.i18n import catalog, err
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, NyaaRelease
from pudge.providers import nyaa
from pudge.providers import seadex as sd
from pudge.providers.nyaa import NoReleaseSourceError, NyaaError
from pudge.sources import (
    MAX_MIRRORS,
    Sources,
    SourcesError,
    import_sources,
    load_sources,
    parse_sources,
    save_sources,
    sources_path,
)

NO_SOURCE = "Add a release source in Settings → Sources."


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = False
    return cfg


def _manager(tmp_path: Path, monkeypatch) -> AnimeManager:
    cfg = _config(tmp_path)
    cfg.nyaa.enabled = True
    cfg.nyaa.auto_download_current = True
    cfg.qbittorrent.enabled = True
    manager = AnimeManager(cfg, log=lambda _message: None)
    manager.db.upsert_anime(
        LibraryAnime(
            media_id=41,
            title="Source Show",
            titles=["Source Show"],
            status="CURRENT",
            media_status="RELEASING",
            progress=0,
            episodes=12,
        )
    )
    # Episode aliases come from AniList; not under test.
    monkeypatch.setattr(manager, "_release_episode_context", lambda _anime, _episode: ((), ()))
    return manager


@contextmanager
def _info(caplog):
    # Creating a manager detaches the app logger from the root logger (its own file).
    logging.getLogger("pudge").propagate = True
    with caplog.at_level(logging.INFO):
        yield


def _logged(caplog, text: str) -> int:
    # One record may reach the capture twice when another test left a handler behind.
    return len({id(record) for record in caplog.records if text in record.getMessage()})


def _no_requests(monkeypatch) -> list[str]:
    """Every HTTP request any release client makes, recorded and refused."""
    seen: list[str] = []

    def refuse(self, url, *args, **kwargs):  # noqa: ANN001
        seen.append(str(url))
        raise AssertionError(f"unexpected request: {url}")

    monkeypatch.setattr(httpx.Client, "get", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(httpx, "get", lambda url, *a, **k: refuse(None, url))
    return seen


# ---------------------------------------------------------------- sources.json


def test_sources_are_validated_and_normalized() -> None:
    sources = parse_sources(
        json.dumps(
            {
                "schema": 1,
                "release_search": {
                    "url": "  https://releases.invalid/  ",
                    "mirrors": [
                        "https://mirror-a.invalid/",
                        "https://mirror-a.invalid",
                        "https://releases.invalid",
                        " ",
                        "http://mirror-b.invalid",
                    ],
                },
                "weekly_rss": "https://weekly.invalid/rss/",
                # A feed is read as given, its trailing "/" included.
                "release_feed": " https://feed.invalid/feeds/site/ ",
                "recommendations": "https://recommendations.invalid/",
                "unknown": "ignored",
            }
        )
    )
    assert sources == Sources(
        release_search="https://releases.invalid",
        mirrors=("https://mirror-a.invalid", "http://mirror-b.invalid"),
        weekly_rss="https://weekly.invalid/rss",
        release_feed="https://feed.invalid/feeds/site/",
        recommendations="https://recommendations.invalid",
    )
    assert sources.release_host == "releases.invalid"
    assert parse_sources({}).empty
    assert parse_sources({"release_search": {"url": "", "mirrors": []}}).empty


@pytest.mark.parametrize(
    "payload,message",
    [
        (
            {"release_search": {"url": "ftp://releases.invalid"}},
            err("sources.invalidUrl", field="release_search.url"),
        ),
        ({"weekly_rss": "javascript:alert(1)"}, err("sources.invalidUrl", field="weekly_rss")),
        ({"release_feed": "https://"}, err("sources.invalidUrl", field="release_feed")),
        ({"recommendations": "https://bad host.invalid"}, err("sources.invalidUrl", field="recommendations")),
        ({"weekly_rss": 5}, err("sources.invalidUrl", field="weekly_rss")),
        (
            {"release_search": {"url": "https://releases.invalid/" + "a" * 600}},
            err("sources.tooLong", field="release_search.url", max=512),
        ),
        (
            {
                "release_search": {
                    "url": RELEASE_SEARCH,
                    "mirrors": [f"https://m{n}.invalid" for n in range(MAX_MIRRORS + 1)],
                }
            },
            err("sources.tooManyMirrors", max=MAX_MIRRORS),
        ),
        ({"schema": 2}, err("sources.unsupportedSchema", schema=2)),
        ([], err("sources.invalidFormat")),
        ({"release_search": 7}, err("sources.invalidFormat")),
    ],
)
def test_invalid_sources_are_refused_with_a_localized_message(payload, message) -> None:
    with pytest.raises(SourcesError) as caught:
        parse_sources(payload)
    assert str(caught.value) == message
    assert "error." not in message  # a real catalog text, not a key


def test_invalid_json_is_refused() -> None:
    with pytest.raises(SourcesError) as caught:
        parse_sources("{not json")
    assert str(caught.value).startswith(err("sources.invalidJson", error="").rstrip())
    # Exactly ten mirrors are fine.
    ok = parse_sources(
        {"release_search": {"url": RELEASE_SEARCH, "mirrors": [f"https://m{n}.invalid" for n in range(10)]}}
    )
    assert len(ok.mirrors) == MAX_MIRRORS


def test_sources_are_saved_atomically_next_to_the_config_and_read_back(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert sources_path(cfg) == tmp_path / "sources.json"
    assert load_sources(cfg) == Sources()
    save_sources(cfg, sample_sources())
    assert json.loads((tmp_path / "sources.json").read_text(encoding="utf-8")) == {
        "schema": 1,
        "release_search": {"url": RELEASE_SEARCH, "mirrors": list(MIRRORS)},
        "weekly_rss": WEEKLY_RSS,
        "release_feed": RELEASE_FEED,
        "recommendations": RECOMMENDATIONS,
    }
    assert sorted(path.name for path in tmp_path.iterdir()) == ["sources.json"]  # no temp file left
    assert load_sources(cfg) == sample_sources()
    save_sources(cfg, sample_sources(weekly_rss=""))
    assert load_sources(cfg).weekly_rss == ""  # a change is read at once


def test_a_broken_sources_file_turns_the_sources_off(tmp_path: Path, caplog) -> None:
    cfg = _config(tmp_path)
    (tmp_path / "sources.json").write_text("{broken", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert load_sources(cfg) == Sources()
    assert "sources.load" in caplog.text


def test_import_replaces_all_fields_or_fills_only_the_empty_ones(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    save_sources(cfg, Sources(release_search="https://own.invalid", weekly_rss=""))
    file_text = json.dumps(sample_sources().to_dict())

    merged = import_sources(cfg, file_text, only_missing=True)
    assert merged.release_search == "https://own.invalid"  # kept
    assert merged.weekly_rss == WEEKLY_RSS and merged.mirrors == MIRRORS  # filled in
    assert load_sources(cfg) == merged

    replaced = import_sources(cfg, file_text, only_missing=False)
    assert replaced == sample_sources() == load_sources(cfg)

    with pytest.raises(SourcesError):
        import_sources(cfg, '{"weekly_rss": "file:///etc/passwd"}', only_missing=False)
    assert load_sources(cfg) == sample_sources()  # nothing saved


def test_an_older_config_moves_its_release_search_into_sources_once(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[nyaa]\nbase_url = "https://releases.invalid/"\nenabled = true\n', encoding="utf-8")
    cfg = load_config(path)
    cfg.library.database_path = tmp_path / "library.sqlite3"
    assert cfg.nyaa.base_url == "https://releases.invalid"

    assert load_sources(cfg) == Sources(release_search=RELEASE_SEARCH)
    assert json.loads((tmp_path / "sources.json").read_text(encoding="utf-8"))["release_search"]["url"] == (
        RELEASE_SEARCH
    )

    # config.toml no longer gets the address; a cleared source stays cleared.
    write_config(cfg, path)
    assert "base_url" not in tomllib.loads(path.read_text(encoding="utf-8"))["nyaa"]
    save_sources(cfg, Sources())
    assert load_sources(load_config(path)) == Sources()


def test_backups_carry_the_sources(tmp_path: Path) -> None:
    import zipfile

    from pudge.backup import create_backup, restore_backup
    from pudge.database import Database

    def live(name: str) -> AppConfig:
        cfg = _config(tmp_path / name)
        cfg.library.database_path.parent.mkdir(parents=True)
        Database(cfg.library.database_path)
        write_config(cfg, cfg.config_path)
        return cfg

    mine, other = live("mine"), live("other")
    save_sources(mine, sample_sources())
    archive = tmp_path / "backup.zip"
    create_backup(
        config_path=mine.config_path,
        database_path=mine.library.database_path,
        cache_dir=mine.paths.cache_dir,
        output=archive,
        version="test",
    )
    assert "sources.json" in zipfile.ZipFile(archive).namelist()
    restore_backup(
        archive_path=archive,
        config_path=other.config_path,
        database_path=other.library.database_path,
        cache_dir=other.paths.cache_dir,
    )
    assert load_sources(other) == sample_sources()


def test_new_configs_have_no_release_search_address(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert cfg.nyaa.base_url == ""
    write_config(cfg, cfg.config_path)
    reloaded = load_config(cfg.config_path)
    assert reloaded.nyaa.base_url == ""
    assert load_sources(reloaded) == Sources()
    assert not (tmp_path / "sources.json").exists()  # nothing to move


# ---------------------------------------------------------------- with a source


def test_release_search_and_mirrors_come_from_sources(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    configure_sources(manager.config)
    client = manager.nyaa_client()
    assert client.search_base_urls == [RELEASE_SEARCH, *MIRRORS]
    requested: list[str] = []

    def get(url: str, _proxy: str | None) -> str:
        requested.append(url)
        if url.startswith(RELEASE_SEARCH):
            raise NyaaError("504")
        return _rss("https://mirror-a.invalid/xmlns/nyaa")

    client._get = get  # type: ignore[method-assign]
    try:
        releases = client.search("Source Show 01")
    finally:
        client.close()
    assert [url.split("/?", 1)[0] for url in requested] == [RELEASE_SEARCH, MIRRORS[0]]
    assert requested[0] == f"{RELEASE_SEARCH}/?page=rss&q=Source+Show+01&c=1_2&f=0"
    assert len(releases) == 1 and releases[0].seeders == 7
    # The mirror that answered goes first; the failed one waits out its backoff.
    assert client.search_base_urls[0] == MIRRORS[0]
    assert nyaa._MIRROR_BACKOFF.get(RELEASE_SEARCH, 0) > 0


def _rss(namespace: str, *, other: str = "") -> str:
    extra = f' xmlns:x="{other}"' if other else ""
    other_seeders = "<x:seeders>99</x:seeders>" if other else ""
    return (
        f'<?xml version="1.0"?><rss version="2.0" xmlns:nyaa="{namespace}"{extra}><channel><item>'
        "<title>[Group] Source Show - 01 [1080p]</title><link>https://releases.invalid/download/1.torrent</link>"
        f"<guid>https://releases.invalid/view/1</guid>{other_seeders}<nyaa:seeders>7</nyaa:seeders>"
        "<nyaa:infoHash>ABC</nyaa:infoHash><nyaa:size>1 GiB</nyaa:size><nyaa:trusted>Yes</nyaa:trusted>"
        "</item></channel></rss>"
    )


def test_the_rss_extension_namespace_is_accepted_on_any_host() -> None:
    for namespace in ("https://releases.invalid/xmlns/nyaa", "https://mirror-b.invalid/xmlns/nyaa/"):
        [release] = nyaa.parse_rss(_rss(namespace))
        assert (release.seeders, release.info_hash, release.trusted) == (7, "abc", True)
    # The format's namespace wins over the same name elsewhere ...
    [release] = nyaa.parse_rss(_rss("https://releases.invalid/xmlns/nyaa", other="https://other.invalid/ns"))
    assert release.seeders == 7
    # ... and a feed with another namespace still reads by local name.
    [release] = nyaa.parse_rss(_rss("https://other.invalid/ext"))
    assert release.seeders == 7


class _Transport:
    """httpx clients made by the feed clients answer from here (never the network)."""

    def __init__(self, monkeypatch, body: str) -> None:
        self.urls: list[str] = []
        real = httpx.Client

        def handler(request: httpx.Request) -> httpx.Response:
            self.urls.append(str(request.url))
            return httpx.Response(200, text=body)

        def client(*args, **kwargs):  # noqa: ANN002, ANN003
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(nyaa.httpx, "Client", client)


WEEKLY_BODY = (
    '<?xml version="1.0"?><rss version="2.0"><channel><item><title>[G] Source Show - 01 (1080p)</title>'
    f"<link>magnet:?xt=urn:btih:{'a' * 40}</link><guid>1</guid></item></channel></rss>"
)


def test_weekly_feed_and_release_feed_addresses_come_from_sources(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    transport = _Transport(monkeypatch, WEEKLY_BODY)
    # Off without addresses: no request, nothing found.
    assert manager._subsplease_client.releases("1080p") == []
    assert manager._shana_client.releases() == []
    assert transport.urls == [] and not manager._release_feeds_configured()

    configure_sources(manager.config)
    assert manager._release_feeds_configured()
    assert len(manager._subsplease_client.releases("1080p")) == 1
    assert len(manager._shana_client.releases()) == 1
    assert transport.urls == [f"{WEEKLY_RSS}/?r=1080", RELEASE_FEED]

    # A changed address is used at once, without restarting.
    configure_sources(manager.config, weekly_rss="https://weekly-2.invalid/rss", release_feed="")
    manager._subsplease_client.releases("720p")
    assert transport.urls[-1] == "https://weekly-2.invalid/rss/?r=720"
    assert manager._shana_client.releases() == [] and len(transport.urls) == 3


def test_recommendations_come_from_sources_and_are_off_without_them(tmp_path: Path, monkeypatch) -> None:
    sample = json.loads((Path(__file__).parent / "fixtures" / "seadex_entry_165790.json").read_text())
    fetched: list[str] = []

    def fetch(url, params, timeout):  # noqa: ANN001
        fetched.append(url)
        return 200, sample

    off = sd.SeaDexClient(tmp_path / "a", fetch=fetch)
    rec = off.recommendations(165790)
    assert (rec.status, rec.error, fetched) == ("unavailable", "not_configured", [])

    on = sd.SeaDexClient(tmp_path / "b", base_url=RECOMMENDATIONS + "/", fetch=fetch)
    rec = on.recommendations(165790)
    assert fetched == [f"{RECOMMENDATIONS}/api/collections/entries/records"]
    assert rec.status == "found" and rec.entry_url == f"{RECOMMENDATIONS}/165790/"
    # A release page link of the format matches on any host (mirrors differ).
    assert rec.match("", "https://mirror-b.invalid/view/1880265") == "alternative"
    assert rec.match("", "https://releases.invalid/?q=1880265") == ""

    # The manager asks only with an address in Settings → Sources.
    monkeypatch.setenv("PUDGE_SEADEX", "1")
    manager = _manager(tmp_path / "m", monkeypatch)
    manager._seadex_client = sd.SeaDexClient(
        manager.config.paths.cache_dir, base_url=manager._recommendations_url, fetch=fetch
    )
    fetched.clear()
    assert manager._seadex_for_search(165790, automatic=False) is None and fetched == []
    configure_sources(manager.config)
    assert manager._seadex_for_search(165790, automatic=False) is not None
    assert fetched == [f"{RECOMMENDATIONS}/api/collections/entries/records"]


# ---------------------------------------------------------------- without a source


def test_interactive_search_without_a_source_says_where_to_add_one(tmp_path: Path, monkeypatch) -> None:
    manager = _manager(tmp_path, monkeypatch)
    requests = _no_requests(monkeypatch)
    with pytest.raises(NoReleaseSourceError) as caught:
        manager.search_releases(41, episode=1, batch=False)
    assert str(caught.value) == err("releases.noSource") == NO_SOURCE
    assert requests == []
    # The plain client says the same instead of a network error.
    with pytest.raises(NoReleaseSourceError):
        nyaa.NyaaClient().search("anything")


def test_light_novel_and_audiobook_searches_without_a_source(tmp_path: Path, monkeypatch) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path)
    try:
        requests = _no_requests(monkeypatch)
        with pytest.raises(NoReleaseSourceError, match="Settings → Sources"):
            api.light_novels.search_nyaa("Some Novel", target_volume=2)
        monkeypatch.setattr(
            api.light_novels,
            "book",
            lambda _book_id: {"id": 1, "volume": 2, "series_title": "Some Novel", "title": "Some Novel 2"},
        )
        with pytest.raises(NoReleaseSourceError, match="Settings → Sources"):
            api.light_novel_search_audiobook_nyaa(1)
        assert requests == []
    finally:
        api.close()


def test_automatic_passes_without_a_source_skip_with_one_info_line(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    _no_requests(monkeypatch)
    messages: list[str] = []
    manager.log = messages.append
    searches: list[int] = []
    real_search = manager.search_releases

    def counted(media_id, **kwargs):  # noqa: ANN001, ANN003
        searches.append(kwargs.get("episode"))
        return real_search(media_id, **kwargs)

    monkeypatch.setattr(manager, "search_releases", counted)
    monkeypatch.setattr(manager, "downloads_configured", lambda: True)
    with _info(caplog):
        assert manager.auto_search_current() == 0
    assert searches == [1]  # stopped after the first answer, not once per episode
    assert messages == []  # no error line per title
    assert _logged(caplog, "SKIP step=nyaa.auto_search reason=no_release_source") == 1
    assert "FALLBACK" not in caplog.text and "WARNING" not in caplog.text

    # A weekly feed alone keeps the automatic pass going (it may find the episode).
    configure_sources(manager.config, release_search="", mirrors=(), release_feed="", recommendations="")
    monkeypatch.setattr(manager._subsplease_client, "releases", lambda _resolution: [])
    searches.clear()
    manager.auto_search_current()
    # The budget (2) bounds older episodes; the newest aired one (12) is searched every pass.
    assert searches == [1, 2, 12]
    # ... while an interactive search still says what is missing.
    with pytest.raises(NoReleaseSourceError):
        manager.search_releases(41, episode=1, batch=False)


def test_upgrade_checks_without_a_source_stop_and_stay_due(tmp_path: Path, monkeypatch, caplog) -> None:
    from test_fix_upgrade_and_watched_import import _upgrade_manager

    manager, client = _upgrade_manager(tmp_path, monkeypatch)
    monkeypatch.setattr(manager, "_release_episode_context", lambda _anime, _episode: ((), ()))
    _no_requests(monkeypatch)
    with _info(caplog):
        assert manager.auto_upgrade_downloaded() == 0
    assert client.added == []
    assert _logged(caplog, "SKIP step=nyaa.upgrade reason=no_release_source") == 1
    assert manager.db.get_state("upgrade_checked:700:5", "") == ""  # checked once a source exists


def test_planning_discovery_without_a_source_keeps_its_schedule(tmp_path: Path, monkeypatch, caplog) -> None:
    import time as _time

    from pudge.planned_release_discovery import _SEARCH_REVISION, PlannedReleaseDiscovery

    manager = _manager(tmp_path, monkeypatch)
    now = _time.time()
    for media_id in (51, 52):
        manager.db.upsert_anime(
            LibraryAnime(
                media_id=media_id,
                title=f"Planned {media_id}",
                status="PLANNING",
                media_status="RELEASING",
                next_airing_episode=2,
                next_airing_at=int(now + 6 * 86400),
                start_date=_time.strftime("%Y-%m-%d", _time.gmtime(now - 86400)),
            )
        )
    discovery = PlannedReleaseDiscovery(manager)
    for media_id in (51, 52):
        discovery._save(
            media_id,
            {
                "phase": "armed",
                "airing_at": now - 3600,
                "airing_source": "weekly_backfill",
                "next_check": now - 60,
                "attempts": 0,
                "search_revision": _SEARCH_REVISION,
            },
        )
    monkeypatch.setattr(
        "pudge.planned_release_discovery._first_airing_candidate",
        lambda _anime: (now - 3600, "weekly_backfill"),
    )
    _no_requests(monkeypatch)
    with _info(caplog):
        result = discovery.tick(now=now)
    assert result["searched"] == 0
    assert _logged(caplog, "SKIP step=planning.release_discovery reason=no_release_source") == 1
    for media_id in (51, 52):
        assert discovery._state(media_id)["attempts"] == 0  # no backoff for a missing source


def test_light_novel_auto_download_without_a_source_skips(tmp_path: Path, monkeypatch, caplog) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path)
    try:
        service = api.light_novels
        api.config.qbittorrent.enabled = True
        service.save_settings({"auto_download_nyaa": True})
        monkeypatch.setattr(
            service,
            "anilist_novels",
            lambda: [
                {"media_id": 1, "title": "Novel A", "status": "CURRENT", "progress_volumes": 0},
                {"media_id": 2, "title": "Novel B", "status": "CURRENT", "progress_volumes": 0},
            ],
        )
        _no_requests(monkeypatch)
        with _info(caplog):
            assert service.auto_download_missing() == []
        assert _logged(caplog, "SKIP step=ln.auto_download reason=no_release_source") == 1
    finally:
        api.close()


# ---------------------------------------------------------------- the window


def test_settings_carry_sources_and_save_them_after_validation(tmp_path: Path) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path)
    try:
        assert api._settings_payload()["sources"] == Sources().to_dict()
        api.save_settings({"sources": sample_sources().to_dict()})
        assert load_sources(api.config) == sample_sources()
        assert api._settings_payload()["sources"] == sample_sources().to_dict()
        bad = sample_sources().to_dict() | {"weekly_rss": "ftp://weekly.invalid"}
        with pytest.raises(SourcesError):
            api.save_settings({"sources": bad, "language": "ru"})
        assert load_sources(api.config) == sample_sources() and api.config.ui.language == "en"
        # Settings saved from elsewhere (no Sources group) keep the sources.
        api.save_settings({"language": "en"})
        assert load_sources(api.config) == sample_sources()
    finally:
        api.close()


class _Window:
    def __init__(self, answer) -> None:  # noqa: ANN001
        self.answer = answer
        self.dialogs: list[tuple] = []

    def create_file_dialog(self, kind, **kwargs):  # noqa: ANN001, ANN003
        self.dialogs.append((kind, kwargs))
        return self.answer


def test_import_and_export_through_file_dialogs(tmp_path: Path, monkeypatch) -> None:
    import types

    from test_web_app import make_api

    fake_webview = types.SimpleNamespace(OPEN_DIALOG="open", SAVE_DIALOG="save")
    monkeypatch.setitem(__import__("sys").modules, "webview", fake_webview)
    api = make_api(tmp_path / "app")
    try:
        chosen = tmp_path / "mine.json"
        chosen.write_text(json.dumps(sample_sources().to_dict()), encoding="utf-8")
        assert api.get_state_fast()["settings"]["sources"] == Sources().to_dict()
        api.window = _Window([str(chosen)])
        result = api.sources_import(False)
        assert result == {"ok": True, "sources": sample_sources().to_dict()}
        assert load_sources(api.config) == sample_sources()
        assert api.window.dialogs[0][0] == "open"
        # The window's next state poll has the imported sources, not a cached copy.
        assert api.get_state_fast()["settings"]["sources"] == sample_sources().to_dict()

        target = tmp_path / "out" / "sources.json"
        target.parent.mkdir()
        api.window = _Window(str(target))
        form = sample_sources(weekly_rss="").to_dict()
        assert api.sources_export(form) == {"ok": True, "path": str(target)}
        assert json.loads(target.read_text(encoding="utf-8")) == form  # what the form shows
        kind, options = api.window.dialogs[0]
        assert kind == "save" and options["save_filename"] == "sources.json"

        api.window = _Window(None)
        assert api.sources_import(False) == {"cancelled": True}
        chosen.write_text('{"release_search": {"url": "gopher://x.invalid"}}', encoding="utf-8")
        api.window = _Window([str(chosen)])
        with pytest.raises(SourcesError):
            api.sources_import(False)
        assert load_sources(api.config) == sample_sources()
    finally:
        api.close()


def test_release_search_test_uses_the_form_sources(tmp_path: Path, monkeypatch) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path)
    try:
        seen: dict = {}

        class Client:
            def __init__(self, base_url, **kwargs):  # noqa: ANN001, ANN003
                seen.update(base_url=base_url, mirrors=tuple(kwargs["mirrors"]))
                self.proxy_url, self.proxy_mode = "", "direct"

            def search(self, _query):  # noqa: ANN001
                return [1, 2]

        monkeypatch.setattr("pudge.web_app.NyaaClient", Client)
        result = api.test_nyaa_proxy({"sources": sample_sources().to_dict()})
        assert seen == {"base_url": RELEASE_SEARCH, "mirrors": MIRRORS}
        assert (result["count"], result["host"]) == (2, "releases.invalid")
    finally:
        api.close()


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_settings_sources_group_saves_imports_and_exports(page, lang) -> None:
    result = page.run(
        r"""
        setPage('settings'); await __tick(20);
        await __click('[role="tab"][data-settings-category="downloads"]'); await __tick(10);
        const out = {shown: __visible('#settingsSources'), heading: __text('#settingsSources h3'),
                     expected: t('settings.sources'), url: $('s_src_release_search').value,
                     mirrors: $('s_src_mirrors').value, intro: document.querySelectorAll('#settingsSources > p').length,
                     label: __text('label[for="s_src_release_search"]'),
                     aside: __text('label[for="s_src_release_search"] .settings-label-aside'),
                     expectedLabel: t('settings.sourceReleaseSearch'), expectedAside: t('settings.sourceReleaseSearchAside'),
                     searchHint: $('s_src_release_search').nextElementSibling?.nextElementSibling?.tagName || ''};
        await __input('#s_src_weekly_rss', 'https://weekly-2.invalid/rss');
        await __click('#saveSettings'); await __tick(20);
        await __click('#exportSources'); await __tick(20);
        out.exportToast = __toasts().at(-1); out.exportExpected = t('toast.sourcesExported', {path: '/out/sources.json'});
        await __click('#importSources'); await __tick(20);
        out.question = __text('#pudgeConfirmMessage'); out.expectedQuestion = t('confirm.replaceSources');
        await __click('[data-pudge-confirm="ok"]'); await __tick(50);
        out.afterImport = $('s_src_release_search').value; out.afterImportMirrors = $('s_src_mirrors').value;
        out.importToast = __toasts().at(-1); out.importExpected = t('toast.sourcesImported');
        return out;
        """,
        lang=lang,
        state={
            "settings": {
                "sources": {
                    "schema": 1,
                    "release_search": {"url": RELEASE_SEARCH, "mirrors": list(MIRRORS)},
                    "weekly_rss": "",
                    "release_feed": "",
                    "recommendations": "",
                }
            }
        },
        before=r"""
        __api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});
        __api.sources_export = async () => ({ok: true, path: '/out/sources.json'});
        __api.sources_import = async () => ({ok: true, sources: {schema: 1,
            release_search: {url: 'https://imported.invalid', mirrors: ['https://m1.invalid', 'https://m2.invalid']},
            weekly_rss: '', release_feed: '', recommendations: ''}});
        """,
    )
    out = result.value
    assert out["shown"] is True and out["heading"] == out["expected"]
    # No intro line and no hint under Release search; a gray aside next to its name instead.
    assert out["intro"] == 0 and out["searchHint"] != "P"
    assert out["aside"] == out["expectedAside"] and out["label"].startswith(out["expectedLabel"])
    assert out["url"] == RELEASE_SEARCH and out["mirrors"] == "\n".join(MIRRORS)
    [[values]] = result.calls_of("save_settings")
    assert values["sources"] == {
        "schema": 1,
        "release_search": {"url": RELEASE_SEARCH, "mirrors": list(MIRRORS)},
        "weekly_rss": "https://weekly-2.invalid/rss",
        "release_feed": "",
        "recommendations": "",
    }
    [[exported]] = result.calls_of("sources_export")
    assert exported == values["sources"] and out["exportToast"] == out["exportExpected"]
    assert out["question"] == out["expectedQuestion"]
    assert result.calls_of("sources_import") == [[False]]
    assert out["afterImport"] == "https://imported.invalid"
    assert out["afterImportMirrors"] == "https://m1.invalid\nhttps://m2.invalid"
    assert out["importToast"] == out["importExpected"]


def test_import_into_empty_sources_does_not_ask(page) -> None:
    result = page.run(
        r"""
        setPage('settings'); await __tick(20);
        await __click('[role="tab"][data-settings-category="downloads"]'); await __tick(10);
        await __click('#importSources'); await __tick(50);
        return {asked: __visible('#pudgeConfirmMessage'), url: $('s_src_release_search').value};
        """,
        before=r"""__api.sources_import = async () => ({ok: true, sources: {schema: 1,
            release_search: {url: 'https://imported.invalid', mirrors: []}, weekly_rss: '', release_feed: '',
            recommendations: ''}});""",
    )
    assert result.value == {"asked": False, "url": "https://imported.invalid"}


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_release_search_shows_a_hint_without_a_source(page, lang) -> None:
    empty = Sources().to_dict()
    result = page.run(
        r"""
        await openRelease(41, 1, false); await __tick(10);
        const out = {body: __text('#modalBody'), expected: t('error.releases.noSource')};
        await __click('[data-action="open-sources-settings"]'); await __tick(30);
        out.page = ui.page; out.open = $('modalBackdrop').classList.contains('open');
        return out;
        """,
        lang=lang,
        state={"settings": {"sources": empty}},
    )
    out = result.value
    assert out["expected"] in out["body"] and not result.called("search_releases")
    assert out["page"] == "settings" and out["open"] is False
    # With a source the search runs as before.
    result = page.run(
        "await openRelease(41, 1, false); await __tick(10); return true;",
        state={"settings": {"sources": sample_sources().to_dict()}},
        api={"search_releases": []},
    )
    assert result.calls_of("search_releases") == [[41, 1, False]]


# ---------------------------------------------------------------- Google Translate


def _ln(tmp_path: Path, *, stored: dict[str, str] | None = None):
    from pudge.light_novels import LightNovelService

    cfg = _config(tmp_path)
    cfg.llm.enabled = False
    service = LightNovelService(cfg)
    if stored:
        with service._connection() as conn:
            for key, value in stored.items():
                conn.execute("INSERT INTO ln_settings(key,value) VALUES(?,?)", (key, value))
    return service


def _google(monkeypatch) -> list[str]:
    calls: list[str] = []

    def fake_get(url: str, **kwargs):  # noqa: ANN003
        calls.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url), json=[[["Hello.", "こんにちは。"]]])

    monkeypatch.setattr(httpx, "get", fake_get)
    return calls


def test_google_translate_is_off_for_new_installs_and_never_asked(tmp_path: Path, monkeypatch) -> None:
    from pudge.light_novels import LightNovelError

    service = _ln(tmp_path)
    calls = _google(monkeypatch)
    assert service.settings().google_translate is False
    assert service.settings_payload()["google_translate"] is False
    with pytest.raises(LightNovelError) as caught:
        service.translate_selection("こんにちは。")
    assert str(caught.value) == err("ln.translationUnavailable")
    assert calls == []
    # A literature snapshot row is not a stored setting.
    with service._connection() as conn:
        conn.execute("INSERT INTO ln_settings(key,value) VALUES('anilist_literature_snapshot:v1','{}')")
    assert service.settings().google_translate is False


def test_installs_with_stored_settings_keep_google_translate(tmp_path: Path, monkeypatch) -> None:
    service = _ln(tmp_path, stored={"reader_font": "gothic"})
    calls = _google(monkeypatch)
    assert service.settings().google_translate is True
    assert service.translate_selection("こんにちは。")["provider"] == "google"
    assert calls == ["https://translate.googleapis.com/translate_a/single"]
    # Turned off: saved, and nothing goes to Google any more.
    service.save_settings({"google_translate": False})
    assert service.settings().google_translate is False
    service.save_settings({"google_translate": True})
    assert service.settings().google_translate is True


def test_google_fallback_after_an_llm_failure_needs_the_switch(tmp_path: Path, monkeypatch) -> None:
    from pudge.light_novels import LightNovelError

    service = _ln(tmp_path)
    service.config.llm.enabled = True
    calls = _google(monkeypatch)
    monkeypatch.setattr(
        service, "_translate_local_llm", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    assert service.settings().translation_google_fallback is True
    with pytest.raises(LightNovelError) as caught:
        service.translate_selection("こんにちは。")
    assert str(caught.value) == err("ln.localTranslationFailed", error="down")
    assert calls == []
    service.save_settings({"google_translate": True})
    assert service.translate_selection("こんにちは。")["provider"] == "google" and len(calls) == 1


def test_player_translation_respects_the_switch(tmp_path: Path, monkeypatch) -> None:
    from pudge.mpv_study import SubtitleStudyApi

    calls = _google(monkeypatch)
    api = SubtitleStudyApi(_config(tmp_path))
    with pytest.raises(Exception) as caught:
        api.translate("こんにちは。")
    assert str(caught.value) == err("ln.translationUnavailable") and calls == []


def test_window_saves_the_google_translate_switch(tmp_path: Path) -> None:
    from test_web_app import make_api

    api = make_api(tmp_path)
    try:
        assert api._settings_payload()["light_novels"]["google_translate"] is False
        api.save_settings({"ln_google_translate": True})
        assert api.light_novels.settings().google_translate is True
    finally:
        api.close()


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_settings_offer_the_google_translate_switch(page, lang) -> None:
    result = page.run(
        r"""
        setPage('settings'); await __tick(20);
        const box = $('s_ln_google_translate');
        const out = {checked: box.checked, disabled: box.disabled, label: __text('#settingsGoogleTranslate'),
                     expected: t('settings.googleTranslate')};
        await __input('#s_ln_google_translate', true);
        await __click('#saveSettings'); await __tick(20);
        return out;
        """,
        lang=lang,
        state={"settings": {"llm_enabled": False, "light_novels": {"google_translate": False}}},
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    out = result.value
    # Usable with the LLM off: that is when Google would translate.
    assert out == {"checked": False, "disabled": False, "label": out["expected"], "expected": out["expected"]}
    [[values]] = result.calls_of("save_settings")
    assert values["ln_google_translate"] is True


# ---------------------------------------------------------------- guards

# Hosts that remain in the shipped files on purpose.
ALLOWED_HOSTS = {
    "127.0.0.1",
    "localhost",
    "anilist.co",
    "docs.anilist.co",
    "graphql.anilist.co",
    "s4.anilist.co",
    "jimaku.cc",
    "jiten.moe",
    "api.jiten.moe",
    "jpdb.io",
    "jpdb.stoplight.io",
    "github.com",
    "api.github.com",
    "raw.githubusercontent.com",
    "brew.sh",
    "translate.googleapis.com",
    "api.openai.com",
    "openrouter.ai",
    "developer.apple.com",
    "www.apple.com",
    "www.w3.org",
    "www.idpf.org",
    "purl.org",
    "mozilla.org",
    "www.nayuki.io",
    "ffmpeg.org",
    "huggingface.co",  # the STT worker downloads the configured Whisper model from there
}
# sha256[:16] of hosts and site names that must not come back (kept unreadable here).
BLOCKED_HOSTS = {
    "991590f923fdaa0c",
    "4a2bbb40a85dc48f",
    "3c1ca56f2284a503",
    "82d002deb1f32fa3",
    "d642c07711bc81e1",
    "1b80e961a4f98582",
    "5d3810cf52f85d50",
    "c060b47693d1df9c",
    "66973cc392213d5d",
    "9931fac579b1a0a1",
    "6bafb16088d59096",
}
BLOCKED_WORDS = {
    "94c0cc6dc8f49e58",
    "68495affe8a03823",
    "648aa6cc5f157873",
    "4637a3c11127763e",
    "408866a42e0ad9eb",
}
SHIPPED = (
    "pudge/**/*.py",
    "pudge/**/*.js",
    "pudge/**/*.html",
    "pudge/**/*.toml",
    "pudge/**/*.lua",
    "pudge/**/*.md",
    "pudge/**/*.css",
    "README.md",
    "docs/**/*.md",
    "config.example.toml",
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()[:16]


def _shipped() -> dict[str, str]:
    files: dict[str, str] = {}
    for pattern in SHIPPED:
        files.update(source_policy.sources(pattern))
    assert (
        "pudge/providers/nyaa.py" in files and "README.md" in files and "pudge/config.example.toml" in files
    )
    # Regex and Lua pattern escapes ("\." / "%.") must not hide a host.
    return {name: text.replace("\\.", ".").replace("%.", ".") for name, text in files.items()}


def _allowed(host: str) -> bool:
    return (
        host in ALLOWED_HOSTS
        or host.endswith((".invalid", ".test", ".example"))
        or host.startswith("example.")
    )


def test_shipped_files_name_no_address_outside_the_allow_list() -> None:
    url = re.compile(r"https?://([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*)")
    found = {
        (name, match.group(1).lower())
        for name, text in _shipped().items()
        for match in url.finditer(text)
        if not _allowed(match.group(1).lower())
    }
    assert found == set()


def test_shipped_files_name_no_tracker_host_even_without_a_scheme() -> None:
    token = re.compile(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
    found = set()
    for name, text in _shipped().items():
        for match in token.finditer(text):
            parts = match.group(0).lower().split(".")
            if any(_digest(".".join(parts[index:])) in BLOCKED_HOSTS for index in range(len(parts) - 1)):
                found.add((name, match.start()))
    assert found == set()


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.casefold()))


def test_interface_texts_readme_and_docs_name_no_site() -> None:
    texts: dict[str, str] = {}
    for lang in ("en", "ru"):
        for key, value in catalog(lang).items():
            for item in value.values() if isinstance(value, dict) else [value]:
                texts[f"{lang}:{key}"] = texts.get(f"{lang}:{key}", "") + " " + str(item)
    texts.update(
        {name: text for name, text in _shipped().items() if name == "README.md" or name.startswith("docs/")}
    )
    found = {
        name for name, text in texts.items() if any(_digest(word) in BLOCKED_WORDS for word in _words(text))
    }
    assert found == set()


def test_the_guards_see_what_they_guard() -> None:
    # The digests match their inputs (checked here with neutral stand-ins).
    assert _digest("Example") == hashlib.sha256(b"example").hexdigest()[:16]
    assert not _allowed("tracker.example.org") and _allowed("releases.invalid")
    shipped = _shipped()
    assert "https://translate.googleapis.com" in shipped["pudge/light_novels.py"]
    assert "jiten.moe/decks" in shipped["pudge/light_novels.py"]  # "\." was unescaped
