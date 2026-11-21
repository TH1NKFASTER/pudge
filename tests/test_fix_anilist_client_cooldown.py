"""One AniList client behaviour for every caller: cool-downs, User-Agent, diagnosis.

During the AniList outage every 403 was answered by asking again (the light-novel
client, the Settings check and cover downloads did not even share the 429
cool-down and sent httpx's default User-Agent), a click in the window could sleep
for two minutes on a 429, Settings called the refusal an invalid token (and a
really expired token "unreachable"), a relation graph cost one request per
franchise level, and a failed episode-numbering lookup was repeated every pass.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from pudge.providers import anilist as al
from pudge.providers.anilist import AniListAuthError, AniListClient, AniListHTTPError

UA = al.ANILIST_USER_AGENT


@pytest.fixture
def mocked(tmp_path, monkeypatch):
    al.configure_rate_limit_dir(tmp_path / "rl")
    sleeps: list[float] = []
    monkeypatch.setattr(al.time, "sleep", lambda seconds: sleeps.append(float(seconds)))

    def make(handler, token: str = "tok"):
        client = AniListClient("https://graphql.anilist.co", token)
        headers = client.client.headers
        client.client.close()
        client.client = httpx.Client(transport=httpx.MockTransport(handler), headers=headers)
        return client

    yield make, sleeps
    al.configure_rate_limit_dir(None)


def _query(request: httpx.Request) -> str:
    return json.loads(request.content)["query"]


def _viewer(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"data": {"Viewer": {"id": 7, "name": "me"}}})


DISABLED = {"errors": [{"message": "The AniList API has been temporarily disabled", "status": 403}]}


def test_user_agent_names_pudge_its_version_and_project():
    from pudge import __version__

    assert UA == f"Pudge/{__version__} (+https://github.com/TH1NKFASTER/pudge)"


def test_403_starts_a_cooldown_every_later_call_honours(mocked, caplog):
    make, sleeps = mocked
    hits: list[str] = []

    def handler(request):
        hits.append(request.headers.get("user-agent"))
        return httpx.Response(403, json=DISABLED, headers={"server": "cloudflare", "cf-ray": "8abc-WAW"})

    client = make(handler)
    with caplog.at_level(logging.WARNING, logger="pudge.providers.anilist"):
        with pytest.raises(AniListHTTPError) as first:
            client.viewer()
    assert first.value.status_code == 403
    assert "temporarily disabled" in str(first.value)
    assert "cloudflare" in caplog.text and "8abc-WAW" in caplog.text
    for _ in range(4):
        with pytest.raises(AniListHTTPError) as later:
            client.viewer()
        assert later.value.status_code == 403 and "temporarily disabled" in str(later.value)
    assert hits == [UA]  # one request; the rest waited for the cool-down
    assert sleeps == []
    refusal = al.anilist_refusal()
    assert refusal is not None and refusal["minutes"] == 15


def test_403_cooldown_grows_and_ends_when_anilist_answers(mocked, monkeypatch):
    make, _sleeps = mocked
    answers = [httpx.Response(403, json=DISABLED), httpx.Response(403, json=DISABLED)]
    client = make(lambda request: answers.pop(0) if answers else _viewer(request))
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(al.time, "time", lambda: clock["now"])

    with pytest.raises(AniListHTTPError):
        client.viewer()
    assert al.anilist_refusal()["minutes"] == 15
    clock["now"] += 16 * 60
    with pytest.raises(AniListHTTPError):
        client.viewer()
    assert al.anilist_refusal()["minutes"] == 30
    clock["now"] += 31 * 60
    assert client.viewer()["id"] == 7
    assert al.anilist_refusal() is None
    assert al._cooldown_state()[3] == 0  # the streak is over


def test_light_novel_client_and_covers_honour_the_cooldown_and_send_the_user_agent(
    tmp_path, mocked, monkeypatch
):
    make, _sleeps = mocked
    from pudge.config import AppConfig
    from pudge.light_novels import LightNovelError, LightNovelService
    from pudge.manager import AnimeManager
    from pudge.manager_models import LibraryAnime

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "tok"
    service = LightNovelService(cfg)
    manager = AnimeManager(cfg, log=lambda _message: None)
    anime = LibraryAnime(
        media_id=5, title="Cover", status="CURRENT", cover_url="https://s4.anilist.co/c/5.jpg"
    )
    posts: list[dict] = []
    gets: list[dict] = []

    def post(url, headers=None, **_kwargs):
        posts.append(dict(headers or {}))
        return httpx.Response(200, json={"data": {"Viewer": {"id": 1}}}, request=httpx.Request("POST", url))

    def get(url, headers=None, **_kwargs):
        gets.append(dict(headers or {}))
        return httpx.Response(200, content=b"jpg", request=httpx.Request("GET", url))

    monkeypatch.setattr("pudge.light_novels.httpx.post", post)
    monkeypatch.setattr("pudge.manager.httpx.get", get)
    assert service._anilist_post("query { Viewer { id } }", {}) == {"Viewer": {"id": 1}}
    assert manager.cover_path(anime) is not None
    assert posts[0]["User-Agent"] == UA and gets[0]["User-Agent"] == UA

    with pytest.raises(AniListHTTPError):
        make(lambda request: httpx.Response(403, json=DISABLED)).viewer()
    with pytest.raises(LightNovelError, match="temporarily disabled"):
        service._anilist_post("query { Viewer { id } }", {})
    other = LibraryAnime(
        media_id=6, title="Cover", status="CURRENT", cover_url="https://s4.anilist.co/c/6.jpg"
    )
    assert manager.cover_path(other) is None
    assert len(posts) == 1 and len(gets) == 1  # nothing sent during the cool-down

    # A 403 seen by the light-novel client starts the shared cool-down too.
    al._PROCESS_COOLDOWN_UNTIL = 0.0
    al._write_marker("rate-limit.json", None)
    monkeypatch.setattr(
        "pudge.light_novels.httpx.post",
        lambda url, **_k: httpx.Response(403, json=DISABLED, request=httpx.Request("POST", url)),
    )
    with pytest.raises(httpx.HTTPStatusError):
        service._anilist_post("query { Viewer { id } }", {})
    assert al.anilist_refusal() is not None


def test_a_user_probe_passes_the_403_cooldown_once(mocked):
    make, _sleeps = mocked
    hits: list[str] = []
    refusing = {"on": True}

    def handler(request):
        hits.append(_query(request))
        if refusing["on"]:
            return httpx.Response(403, json=DISABLED)
        return _viewer(request)

    client = make(handler)
    with pytest.raises(AniListHTTPError):
        client.viewer()  # starts the cool-down
    state = al._cooldown_state()
    with pytest.raises(AniListHTTPError):
        client.viewer()  # background work: refused locally
    assert len(hits) == 1

    with al.probe_requests():
        with pytest.raises(AniListHTTPError):
            client.viewer()  # the probe reaches AniList, which still refuses
        with pytest.raises(AniListHTTPError):
            client.viewer()  # only one probe per user action
    assert len(hits) == 2
    after = al._cooldown_state()
    assert after[1] == 403 and after[3] == state[3] and abs(after[0] - state[0]) < 5  # unchanged

    refusing["on"] = False
    with al.probe_requests():
        assert client.viewer()["id"] == 7
    assert al.anilist_refusal() is None and len(hits) == 3
    assert client.viewer()["id"] == 7  # background work goes on normally
    assert len(hits) == 4


def test_manual_sync_probes_while_background_sync_honours_the_cooldown(tmp_path, mocked, monkeypatch):
    make, _sleeps = mocked
    from pudge.manager_models import LibraryAnime
    from test_web_app import make_api

    api = make_api(tmp_path)
    al.configure_rate_limit_dir(tmp_path / "rl")
    for config in (api.config, api.manager.config):
        config.anilist.enabled = True
        config.anilist.access_token = "tok"
    api.manager.db.upsert_anime(LibraryAnime(media_id=5, title="Watching", status="CURRENT", progress=1))
    hits: list[str] = []
    refusing = {"on": True}

    def handler(request):
        query = _query(request)
        hits.append(query)
        if refusing["on"]:
            return httpx.Response(403, json=DISABLED)
        if "Viewer" in query:
            return _viewer(request)
        lists = [{"status": "CURRENT", "entries": []}]
        return httpx.Response(200, json={"data": {"MediaListCollection": {"lists": lists}}})

    monkeypatch.setattr("pudge.manager.AniListClient", lambda *_a, **_k: make(handler))
    assert api.manager.sync_anilist().used_cache is True  # the 403 starts the cool-down
    hits.clear()
    assert api.manager.sync_anilist().used_cache is True  # background: nothing sent
    assert hits == []

    assert api.sync_anilist()["stats"]["cached"] is True  # Sync button: exactly one probe
    assert len(hits) == 1 and al.anilist_refusal() is not None

    refusing["on"] = False
    hits.clear()
    assert api.sync_anilist()["stats"]["cached"] is False
    assert al.anilist_refusal() is None and len(hits) == 2  # Viewer + list


def test_a_click_in_the_window_never_sleeps_for_the_rate_limit(mocked):
    make, sleeps = mocked
    hits: list[int] = []

    def limited(request):
        hits.append(1)
        return httpx.Response(429, headers={"Retry-After": "60"})

    client = make(limited)
    client.interactive = True
    with pytest.raises(AniListHTTPError) as caught:
        client.set_list_status(1, "CURRENT")
    assert caught.value.status_code == 429
    assert hits == [1] and sleeps == []  # no inline wait and no retry
    with al.interactive_requests():
        with pytest.raises(AniListHTTPError):
            make(limited).viewer()  # the shared cool-down is not slept either
    assert hits == [1] and sleeps == []


def test_background_bulk_requests_are_paced(mocked, monkeypatch):
    make, _sleeps = mocked
    clock = {"now": 5000.0}
    paced: list[float] = []

    def sleep(seconds):
        paced.append(float(seconds))
        clock["now"] += float(seconds)

    monkeypatch.setattr(al.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(al.time, "sleep", sleep)
    client = make(_viewer)
    with al.bulk_requests():
        for _ in range(3):
            client.viewer()
    assert paced == [al.ANILIST_BULK_INTERVAL_SECONDS] * 2  # at most 30 requests a minute
    paced.clear()
    client.viewer()  # outside bulk work: not paced
    assert paced == []


def test_viewer_id_is_asked_once_per_token(mocked):
    make, _sleeps = mocked
    queries: list[str] = []

    def handler(request):
        query = _query(request)
        queries.append("Viewer" if "Viewer" in query else "List")
        if "Viewer" in query:
            return _viewer(request)
        return httpx.Response(200, json={"data": {"MediaListCollection": {"lists": []}}})

    make(handler).library()
    make(handler).library()
    assert queries == ["Viewer", "List", "List"]
    al._VIEWER_IDS.clear()  # a new process reads it from the cache directory
    make(handler).library()
    assert queries[-1] == "List" and queries.count("Viewer") == 1


def test_invalid_token_is_detected_and_shown_until_a_request_works(tmp_path, mocked):
    make, _sleeps = mocked
    from test_web_app import make_api

    api = make_api(tmp_path)
    al.configure_rate_limit_dir(tmp_path / "rl")
    api.config.anilist.enabled = True
    api.config.anilist.access_token = "tok"
    invalid = {"errors": [{"message": "Invalid token", "status": 400}]}
    with pytest.raises(AniListAuthError):
        make(lambda request: httpx.Response(400, json=invalid)).viewer()
    assert al.token_marked_invalid("tok") and not al.token_marked_invalid("other")
    assert api.get_state()["anilist_status"]["token_invalid"] is True
    make(_viewer).viewer()
    assert not al.token_marked_invalid("tok")


def _check(tmp_path, monkeypatch, handler):
    import pudge.web_app as web_app_module
    from test_web_app import make_api

    api = make_api(tmp_path)
    al.configure_rate_limit_dir(tmp_path.parent / "rl")  # shared by the checks of one test
    api.config.anilist.access_token = "tok"
    seen: list[str] = []
    real = web_app_module.AniListClient

    def factory(*args, **kwargs):
        client = real(*args, **kwargs)
        headers = client.client.headers
        client.client.close()

        def recording(request):
            seen.append(request.headers.get("user-agent"))
            return handler(request)

        client.client = httpx.Client(transport=httpx.MockTransport(recording), headers=headers)
        return client

    monkeypatch.setattr(web_app_module, "AniListClient", factory)
    return api.test_saved_credentials(["anilist"])[0], seen


def test_credentials_check_tells_a_bad_token_from_anilist_refusing(tmp_path, monkeypatch):
    monkeypatch.setattr(al.time, "sleep", lambda _seconds: None)
    ok, seen = _check(tmp_path / "ok", monkeypatch, _viewer)
    assert ok["status"] == "ok" and seen == [UA]

    invalid, _ = _check(
        tmp_path / "invalid",
        monkeypatch,
        lambda request: httpx.Response(400, json={"errors": [{"message": "Invalid token", "status": 400}]}),
    )
    assert invalid["status"] == "invalid"

    refused, _ = _check(tmp_path / "refused", monkeypatch, lambda request: httpx.Response(403, json=DISABLED))
    assert refused["status"] == "refused" and "temporarily disabled" in refused["detail"]
    minutes = al.anilist_refusal()["minutes"]
    # During the cool-down the user's check still asks AniList once.
    still, seen = _check(tmp_path / "still", monkeypatch, lambda request: httpx.Response(403, json=DISABLED))
    assert still["status"] == "refused" and seen == [UA]
    assert al.anilist_refusal()["minutes"] == minutes  # the cool-down runs on unchanged
    back, seen = _check(tmp_path / "back", monkeypatch, _viewer)
    assert back["status"] == "ok" and seen == [UA]
    assert al.anilist_refusal() is None  # AniList answered: the cool-down is over at once

    al._PROCESS_COOLDOWN_UNTIL = 0.0

    def offline(request):
        raise httpx.ConnectError("no route", request=request)

    unreachable, _ = _check(tmp_path / "offline", monkeypatch, offline)
    assert unreachable["status"] == "unreachable"


def _chain_handler(
    length: int, requests: list[str], *, completed: set[int] = frozenset(), reject_nested=False
):
    def leaf(media_id):
        return {"id": media_id, "type": "ANIME", "format": "MOVIE", "title": {"romaji": f"Conan {media_id}"}}

    def edges(media_id, full):
        result = []
        for relation, other in (("PREQUEL", media_id - 1), ("SEQUEL", media_id + 1)):
            if 1 <= other <= length:
                result.append(
                    {"relationType": relation, "node": node(other, nested=False) if full else leaf(other)}
                )
        return {"edges": result}

    def node(media_id, *, nested=True):
        value = {**leaf(media_id), "episodes": 1, "seasonYear": 2000 + media_id, "mediaListEntry": None}
        value["relations"] = edges(media_id, nested)
        return value

    def handler(request):
        query = _query(request)
        ids = json.loads(request.content)["variables"]["ids"]
        if "mediaListEntry { status progress }\n    }" in query and "relations" not in query:
            requests.append("entries")
            media = [
                {
                    "id": media_id,
                    "mediaListEntry": {"status": "COMPLETED", "progress": 1}
                    if media_id in completed
                    else None,
                }
                for media_id in ids
            ]
            return httpx.Response(200, json={"data": {"Page": {"media": media}}})
        nested = "fragment GraphNode" in query
        requests.append("nested" if nested else "flat")
        if nested and reject_nested:
            return httpx.Response(400, json={"errors": [{"message": "Max query complexity", "status": 400}]})
        return httpx.Response(200, json={"data": {"Page": {"media": [node(i, nested=nested) for i in ids]}}})

    return handler


def test_relation_graph_covers_two_levels_per_request(mocked):
    make, _sleeps = mocked
    requests: list[str] = []
    graph = make(_chain_handler(60, requests, completed={31})).full_relation_graph(30)

    assert len(graph["nodes"]) == 60
    assert requests.count("nested") <= 16  # was 31: one request per level
    assert requests.count("entries") == 1
    by_id = {node["media_id"]: node for node in graph["nodes"]}
    assert by_id[31]["list_status"] == "COMPLETED" and by_id[31]["watched"] is True
    assert len([edge for edge in graph["edges"] if edge["relation_type"] == "SEQUEL"]) == 59


def test_relation_graph_falls_back_when_anilist_rejects_two_levels(mocked):
    make, _sleeps = mocked
    requests: list[str] = []
    graph = make(_chain_handler(6, requests, reject_nested=True)).full_relation_graph(3)
    assert len(graph["nodes"]) == 6
    assert requests[0] == "nested" and set(requests[1:]) == {"flat"}


def test_failed_numbering_lookup_is_not_repeated_by_automatic_searches(tmp_path, monkeypatch):
    from pudge import episode_numbering as en
    from pudge.config import AppConfig
    from pudge.providers.anilist import AniListError

    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.enabled = True
    calls: list[int] = []
    answers = {"fail": True}

    class Lookup:
        def __init__(self, *_args, **_kwargs):
            pass

        def absolute_episode_number(self, anime, episode, **_kwargs):
            calls.append(episode)
            if answers["fail"]:
                raise AniListError("AniList refuses requests")
            return episode, []

        def close(self):
            pass

    monkeypatch.setattr(en, "AniListClient", Lookup)
    anime = SimpleNamespace(
        media_id=77, title="Show Season 2", titles=["Show Season 2"], episodes=12, format="TV"
    )
    log = logging.getLogger("t")
    marker = Path(cfg.paths.cache_dir) / "anilist-numbering-failures" / "77.json"
    with en.automatic_lookups():
        assert en.resolve_episode_numbering(anime, 3, cfg, log).release_episode == 3
        assert en.resolve_episode_numbering(anime, 4, cfg, log).release_episode == 4
    assert len(calls) == 1 and marker.is_file()
    en.resolve_episode_numbering(anime, 4, cfg, log)  # a search the user started still asks
    assert len(calls) == 2

    old = marker.stat().st_mtime - 4 * 3600
    os.utime(marker, (old, old))
    answers["fail"] = False
    with en.automatic_lookups():
        en.resolve_episode_numbering(anime, 5, cfg, log)  # hours later: asked, answered
    assert len(calls) == 3 and not marker.exists()

    answers["fail"] = True
    other = SimpleNamespace(
        media_id=78, title="Other Season 2", titles=["Other Season 2"], episodes=12, format="TV"
    )
    with en.automatic_lookups():
        en.resolve_episode_numbering(other, 6, cfg, log)
    other_marker = marker.with_name("78.json")
    assert other_marker.is_file()
    en.clear_lookup_failures(cfg)  # an answered AniList list refresh
    assert not other_marker.exists()


def test_nested_graph_query_falls_back_on_5xx_and_timeouts(mocked):
    make, _sleeps = mocked
    for failure in ("5xx", "timeout"):
        al.AniListClient._nested_graph_query = True
        requests: list[str] = []
        inner = _chain_handler(4, requests)

        def handler(request, failure=failure, inner=inner, requests=requests):
            if "fragment GraphNode" in _query(request):
                requests.append("nested-failed")
                if failure == "timeout":
                    raise httpx.ReadTimeout("read timed out", request=request)
                return httpx.Response(500, text="Internal Server Error")
            return inner(request)

        graph = make(handler).full_relation_graph(2)
        assert len(graph["nodes"]) == 4, failure
        assert requests[:2] == ["nested-failed", "nested-failed"]  # the request and its one retry
        assert "flat" in requests and al.AniListClient._nested_graph_query is False


def test_any_other_answer_ends_the_cooldown_so_a_probe_is_not_wasted(tmp_path, monkeypatch, mocked):
    make, _sleeps = mocked
    from pudge.anilist_tracking import pending_statuses, queue_pending_status
    from pudge.manager_models import LibraryAnime
    from test_web_app import make_api

    api = make_api(tmp_path)
    al.configure_rate_limit_dir(tmp_path / "rl")
    for config in (api.config, api.manager.config):
        config.anilist.enabled = True
        config.anilist.access_token = "tok"
    db = api.manager.db
    db.upsert_anime(LibraryAnime(media_id=5, title="Watching", status="CURRENT", progress=1, episodes=12))
    refusing = {"on": True}

    def handler(request):
        body = json.loads(request.content)
        if refusing["on"]:
            return httpx.Response(403, json=DISABLED)
        if (body.get("variables") or {}).get("mediaId") == 99:
            return httpx.Response(404, json={"errors": [{"message": "Not Found.", "status": 404}]})
        if "Viewer" in body["query"]:
            return _viewer(request)
        return httpx.Response(200, json={"data": {"MediaListCollection": {"lists": []}}})

    monkeypatch.setattr("pudge.manager.AniListClient", lambda *_a, **_k: make(handler))
    assert api.manager.sync_anilist().used_cache is True
    queue_pending_status(db, 99, "PLANNING", previous="")  # a sequel added during the outage
    refusing["on"] = False
    stats = api.sync_anilist()["stats"]  # the probe is the 404 for media 99
    assert stats["cached"] is False and al.anilist_refusal() is None
    assert pending_statuses(db) == {}


def test_a_403_for_one_request_does_not_stop_anilist_for_everyone(mocked):
    make, _sleeps = mocked
    hits: list[str] = []

    def handler(request):
        query = _query(request)
        hits.append(query)
        if "SaveMediaListEntry" in query:
            return httpx.Response(403, json={"errors": [{"message": "Forbidden.", "status": 403}]})
        return _viewer(request)

    client = make(handler)
    with pytest.raises(AniListHTTPError) as caught:
        client.set_list_status(1, "CURRENT")
    assert caught.value.status_code == 403 and caught.value.refusal is False
    assert al.entry_error_reason(caught.value) == "rejected"
    assert al.anilist_refusal() is None
    assert client.viewer()["id"] == 7 and len(hits) == 2

    for body in ({"errors": [{"message": "Forbidden.", "status": 403}]}, None):
        hits.clear()
        al._PROCESS_COOLDOWN_UNTIL = 0.0
        al._write_marker("rate-limit.json", None)
        blocked = make(
            lambda request, body=body: (
                httpx.Response(403, json=body)
                if body
                else httpx.Response(
                    403,
                    text="<html>Attention Required! | Cloudflare</html>",
                    headers={"content-type": "text/html"},
                )
            )
        )
        with pytest.raises(AniListHTTPError) as caught:
            blocked.viewer()  # the Viewer query, or a Cloudflare page: AniList refuses everyone
        assert caught.value.refusal is True and al.anilist_refusal() is not None
