"""Nyaa: a 429 "Retry-After" holds for every search, and an automatic search stops once it has its pick.

The circuit breaker lived in each NyaaClient, and every search makes a new one, so it
never tripped across searches; a 429 was treated like any other error and its
Retry-After ignored. One automatic pass over 10 shows x 2 episodes still sent 20
requests after nyaa answered 429 with Retry-After: 600. An automatic episode search
also sent all of its alias queries after the first one had returned a trusted
release of exactly that episode.
"""

from __future__ import annotations

import httpx
import pytest
from test_fix_number_dash_episode_not_pack import FeedClient, rss

from pudge.manager_models import LibraryAnime
from pudge.providers import nyaa

ANIME = LibraryAnime(
    media_id=171018,
    title="Dandadan",
    titles=["Dandadan", "DAN DA DAN", "ダンダダン"],
    synonyms=["Dan Da Dan"],
    episodes=12,
    format="TV",
    relations=[],
)


def _client(handler) -> nyaa.NyaaClient:
    client = nyaa.NyaaClient("https://nyaa.example", proxy_mode="direct")
    client._clients[""] = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_retry_after_holds_for_later_clients():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(429, headers={"Retry-After": "600"}, text="Too Many Requests")

    for _search in range(20):
        client = _client(handler)
        with pytest.raises(nyaa.NyaaError):
            client.search("Dandadan 05")
        client.close()
    assert len(requests) == 1
    assert 590 <= nyaa._rate_limit_wait("https://nyaa.example/?page=rss") <= 600
    message = ""
    try:
        _client(handler).search("Dandadan 06")
    except nyaa.NyaaError as exc:
        message = str(exc)
    assert "asked to slow down" in message


def test_breaker_is_shared_by_the_clients_of_one_host():
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(502, text="Bad Gateway")

    for _search in range(6):
        client = _client(handler)
        with pytest.raises(nyaa.NyaaError):
            client.search("Dandadan 05")
        client.close()
    # Three failures open the host's breaker for everyone (it used to be 6 requests).
    assert len(requests) == 3


def test_retry_after_http_date_and_cap():
    assert nyaa._retry_after_seconds("120") == 120.0
    assert nyaa._retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    assert nyaa._retry_after_seconds("") is None
    request = httpx.Request("GET", "https://cap.example/")
    response = httpx.Response(429, headers={"Retry-After": "86400"}, request=request)
    nyaa._note_rate_limit(
        "https://cap.example/", httpx.HTTPStatusError("429", request=request, response=response)
    )
    assert nyaa._rate_limit_wait("https://cap.example/x") <= nyaa.RATE_LIMIT_MAX_SECONDS


def _search(client, *, automatic: bool):
    return nyaa.search_ranked(
        client,
        ANIME,
        episode=5,
        batch=False,
        trusted_groups=["SubsPlease", "Erai-raws"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
        max_queries=5,
        query_budget_seconds=18.0 if automatic else None,
    )


def test_automatic_search_stops_after_a_trusted_exact_episode():
    title = "[SubsPlease] Dandadan - 05 (1080p) [ABCD1234].mkv"
    client = FeedClient(rss([(title, "1.4 GiB", 300, True)]))
    found = _search(client, automatic=True)
    assert [item.title for item in found] == [title]
    assert len(client.queries) == 1

    # Only a 720p or an untrusted release: the other aliases are still asked.
    client = FeedClient(rss([("[Grp] Dandadan - 05 (720p).mkv", "700 MiB", 30, False)]))
    _search(client, automatic=True)
    assert len(client.queries) > 1
    # A wrong episode does not stop it either.
    client = FeedClient(rss([("[SubsPlease] Dandadan - 04 (1080p) [ABCD1234].mkv", "1.4 GiB", 300, True)]))
    _search(client, automatic=True)
    assert len(client.queries) > 1


def test_failing_direct_route_does_not_pause_the_proxy():
    """direct_then_proxy: direct keeps failing; the proxy is down at first, then back."""
    proxy_up = {"value": False}
    requests: list[str] = []

    class Routes(nyaa.NyaaClient):
        def _client_for_proxy(self, proxy):
            def handler(request: httpx.Request) -> httpx.Response:
                route = "proxy" if proxy else "direct"
                requests.append(route)
                if route == "proxy" and proxy_up["value"]:
                    return httpx.Response(
                        200, text=rss([("[SubsPlease] Dandadan - 05 (1080p)", "1.4 GiB", 300, True)])
                    )
                return httpx.Response(502, text="Bad Gateway")

            return httpx.Client(transport=httpx.MockTransport(handler))

    def search():
        client = Routes(
            "https://nyaa.example", proxy_mode="direct_then_proxy", proxy_url="socks5://127.0.0.1:1080"
        )
        try:
            return client.search("Dandadan 05")
        finally:
            client.close()

    for _search in range(4):
        with pytest.raises(nyaa.NyaaError):
            search()
    proxy_up["value"] = True
    # The proxy route's own breaker is open after its 3 failures: it recovers like any breaker.
    for breaker in nyaa._HOST_BREAKERS.values():
        breaker.opened_at = breaker.opened_at - 31 if breaker.opened_at else 0.0
    requests.clear()
    found = search()
    assert [item.title for item in found] == ["[SubsPlease] Dandadan - 05 (1080p)"]
    assert "proxy" in requests
    # The direct route kept failing all along; it never paused the proxy route.
    assert set(nyaa._HOST_BREAKERS) == {
        ("https://nyaa.example", "direct"),
        ("https://nyaa.example", "socks5://127.0.0.1:1080"),
    }


def test_direct_failures_alone_never_refuse_the_proxy():
    calls: list[str] = []

    class Routes(nyaa.NyaaClient):
        def _client_for_proxy(self, proxy):
            def handler(request: httpx.Request) -> httpx.Response:
                calls.append("proxy" if proxy else "direct")
                if proxy:
                    return httpx.Response(
                        200, text=rss([("[SubsPlease] Dandadan - 05 (1080p)", "1.4 GiB", 300, True)])
                    )
                return httpx.Response(502, text="Bad Gateway")

            return httpx.Client(transport=httpx.MockTransport(handler))

    for _search in range(6):
        client = Routes(
            "https://nyaa.example", proxy_mode="direct_then_proxy", proxy_url="socks5://127.0.0.1:1080"
        )
        assert client.search("Dandadan 05")
        client.close()
    assert calls.count("proxy") == 6 and calls.count("direct") == 3


APOTHECARY = LibraryAnime(
    media_id=161645,
    title="Kusuriya no Hitorigoto",
    titles=["Kusuriya no Hitorigoto", "The Apothecary Diaries"],
    episodes=24,
    format="TV",
    relations=[],
)
ERAI = "[Erai-raws] Kusuriya no Hitorigoto - 05 [1080p CR WEB-DL AVC AAC][MultiSub]"
SUBSPLEASE = "[SubsPlease] Kusuriya no Hitorigoto - 05 (1080p) [ABCD1234].mkv"
TOONSHUB = "[ToonsHub] The Apothecary Diaries S01E05 1080p CR WEB-DL AAC2.0 H.264"


class _ByQuery(nyaa.NyaaClient):
    def __init__(self, answers):
        super().__init__("https://nyaa.example")
        self.answers = answers
        self.queries: list[str] = []

    def search(self, query, **_kwargs):
        self.queries.append(query)
        titles = [title for prefix, title in self.answers if query.startswith(prefix)]
        return nyaa.parse_rss(rss([(title, "1.4 GiB", 300, True) for title in titles]))


def _automatic(client, **groups):
    return nyaa.search_ranked(
        client,
        APOTHECARY,
        episode=5,
        batch=False,
        trusted_groups=["SubsPlease", "Erai-raws"],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=250 * 1024**2,
        target_episode_max_bytes=3500 * 1024**2,
        max_queries=5,
        query_budget_seconds=18.0,
        **groups,
    )


def test_blocked_group_never_stops_the_plan_and_the_preferred_one_wins():
    client = _ByQuery([("Kusuriya", ERAI), ("The Apothecary", TOONSHUB)])
    found = _automatic(client, preferred_groups=["ToonsHub"], blocked_groups=["Erai-raws"])
    assert any(query.startswith("The Apothecary") for query in client.queries)
    assert found[0].title == TOONSHUB


def test_with_preferred_groups_only_a_preferred_hit_stops_the_plan():
    client = _ByQuery([("Kusuriya", SUBSPLEASE), ("The Apothecary", TOONSHUB)])
    found = _automatic(client, preferred_groups=["ToonsHub"], blocked_groups=[])
    assert any(query.startswith("The Apothecary") for query in client.queries)
    assert {item.title for item in found} == {SUBSPLEASE, TOONSHUB}
    # No preferred groups: the first trusted exact hit is enough.
    client = _ByQuery([("Kusuriya", SUBSPLEASE), ("The Apothecary", TOONSHUB)])
    _automatic(client, preferred_groups=[], blocked_groups=[])
    assert len(client.queries) == 1
