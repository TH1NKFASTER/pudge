"""Review fixes: review providers, Nyaa, aria2, qBittorrent and LLM replies.

1. A Jiten/JPDB grade whose connection dropped after the request was sent
   (RemoteProtocolError) or failed in a proxy escaped as a plain error, so
   the user pressed the grade again and the card was graded twice. Now every
   transport failure of the submit is "outcome unknown" and is not retried.
2. One network error on one Jiten history read (or study-batch round) raised a
   raw httpx error, which the review pool does not handle, and threw away the
   whole candidate list. Now it is a provider error and only that card is
   skipped (a failed later study-batch round keeps the cards already found).
3. One Nyaa item with an absurd size ("999...9 GiB") raised OverflowError out
   of the RSS parser, failing the whole search and skipping the mirror
   fallback. Now the size reads as 0 and a bad item is skipped on its own.
4. .torrent downloads read the whole body before the 10/12 MB check, with
   only per-read timeouts. Now they stream with a byte cap and stop as soon
   as the cap is passed or 60 s have gone by.
5. aria2 started without a secret accepts any token, so someone else's aria2
   on Pudge's port looked like Pudge's own: Pudge changed its options and
   shut it down. Now only an aria2 that rejects a wrong token (or the process
   this client started) is treated as Pudge's; another one is a port conflict.
6. The aria2 RPC secret was on the command line (visible via ps) and its file
   was briefly world-readable. Now the secret file is created 0600 and the
   secret reaches aria2 through a 0600 --conf-path file.
7. qBittorrent discovery counted any HTTP 403 as "qBittorrent found" and sent
   the Bearer API key to every candidate port, so login details could go to
   an unrelated local service. Now probes carry no credentials and only a
   real version answer or qBittorrent's own login page identifies it.
8. An OpenAI-compatible reply with an empty "choices" list raised IndexError
   in json_chat/chat_text. Now it is handled as an empty answer (None).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import pudge.providers.base as base_module
from pudge.config import LLMConfig
from pudge.llm import OllamaClient
from pudge.providers import aria2 as aria2_module
from pudge.providers import nyaa as nyaa_module
from pudge.providers.aria2 import Aria2Client, Aria2Error
from pudge.providers.nyaa import NyaaClient, NyaaError, parse_rss, parse_size
from pudge.providers.qbittorrent import QBittorrentClient
from pudge.review_providers import (
    JitenReviewProvider,
    JpdbReviewProvider,
    ReviewOutcomeUnknown,
    ReviewProviderError,
)

REAL_HTTPX_CLIENT = httpx.Client


def _patch_httpx_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Every httpx.Client created by the code under test talks to ``handler``."""

    def make(*args, **kwargs):
        kwargs.pop("proxy", None)
        return REAL_HTTPX_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", make)


# 1. Submit failures after sending are "outcome unknown".


@pytest.mark.parametrize(
    "error",
    [
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
        httpx.ProxyError("proxy closed the tunnel"),
    ],
)
def test_dropped_review_submit_is_outcome_unknown(monkeypatch: pytest.MonkeyPatch, error) -> None:
    calls: list[str] = []

    def post(url, **_kwargs):
        calls.append(url)
        raise error

    monkeypatch.setattr("pudge.review_providers.httpx.post", post)
    with pytest.raises(ReviewOutcomeUnknown) as jiten:
        JitenReviewProvider("jiten-token").submit_review(1, 0, "good", attempt_id="attempt-1")
    with pytest.raises(ReviewOutcomeUnknown) as jpdb:
        JpdbReviewProvider("jpdb-token").submit_review(5, 7, "okay", attempt_id="attempt-2")
    assert (jiten.value.provider, jiten.value.attempt_id) == ("jiten", "attempt-1")
    assert (jpdb.value.provider, jpdb.value.attempt_id) == ("jpdb", "attempt-2")
    assert len(calls) == 2  # sent once each, never retried


# 2. One network error skips one card, not the whole review pool.


class _JsonResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def json(self):
        return self._payload


def test_one_failed_history_read_skips_only_that_card(monkeypatch: pytest.MonkeyPatch) -> None:
    def get(url, **_kwargs):
        if url.endswith("/srs/study-batch"):
            cards = [{"wordId": word, "readingIndex": 0, "isNewCard": False} for word in (1, 2, 3)]
            return _JsonResponse({"sessionId": "s", "cards": cards, "reviewsRemaining": 0})
        if "/review-history/2/" in url:
            raise httpx.ConnectError("connection refused")
        return _JsonResponse({"card": {"state": 2}, "reviews": [{"rating": 3}]})

    monkeypatch.setattr("pudge.review_providers.httpx.get", get)
    provider = JitenReviewProvider("jiten-token")

    with pytest.raises(ReviewProviderError):
        provider.lookup_state(2, 0)
    pool = provider.list_strict_review_candidates(required=3)
    assert [card["pudgeCardKey"] for card in pool["cards"]] == ["1:0", "3:0"]


def test_failed_later_study_batch_round_keeps_found_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    rounds = {"count": 0}

    def get(url, **_kwargs):
        if url.endswith("/srs/study-batch"):
            rounds["count"] += 1
            if rounds["count"] > 1:
                raise httpx.ReadError("connection reset")
            cards = [{"wordId": 7, "readingIndex": 0, "isNewCard": False}]
            return _JsonResponse({"sessionId": "s", "cards": cards, "reviewsRemaining": 5})
        return _JsonResponse({"card": {"state": 2}, "reviews": [{"rating": 3}]})

    monkeypatch.setattr("pudge.review_providers.httpx.get", get)
    provider = JitenReviewProvider("jiten-token")
    pool = provider.list_strict_review_candidates(required=6)
    assert [card["pudgeCardKey"] for card in pool["cards"]] == ["7:0"]

    rounds["count"] = 1  # the very first round fails: a provider error, not a raw httpx one
    with pytest.raises(ReviewProviderError):
        provider.list_review_candidates(limit=5)


# 3. One bad Nyaa item does not break the search.

RSS = """<?xml version="1.0"?>
<rss version="2.0" xmlns:nyaa="https://releases.invalid/xmlns/nyaa">
<channel>
{items}
</channel>
</rss>"""

ITEM = """<item>
  <title>{title}</title>
  <link>https://nyaa.example/download/{n}.torrent</link>
  <guid>https://nyaa.example/view/{n}</guid>
  <pubDate>Mon, 01 Jan 2024 00:00:00 -0000</pubDate>
  <nyaa:seeders>10</nyaa:seeders>
  <nyaa:leechers>1</nyaa:leechers>
  <nyaa:downloads>5</nyaa:downloads>
  <nyaa:infoHash>{n:040x}</nyaa:infoHash>
  <nyaa:categoryId>1_2</nyaa:categoryId>
  <nyaa:size>{size}</nyaa:size>
  <nyaa:trusted>No</nyaa:trusted>
  <nyaa:remake>No</nyaa:remake>
</item>"""


def _rss(*items: tuple[str, str]) -> str:
    return RSS.format(
        items="\n".join(ITEM.format(title=title, n=n, size=size) for n, (title, size) in enumerate(items, 1))
    )


def test_huge_nyaa_size_does_not_break_the_search(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_size("9" * 400 + " GiB") == 0
    assert parse_size("1.5 GiB") == int(1.5 * 1024**3)

    feed = _rss(
        ("[Group] Show - 01 [1080p].mkv", "9" * 400 + " GiB"), ("[Group] Show - 02 [1080p].mkv", "1.4 GiB")
    )
    client = NyaaClient("https://nyaa.example", proxy_mode="direct")
    client._get = lambda _url, _proxy: feed  # type: ignore[method-assign]
    try:
        releases = client.search("Show")
    finally:
        client.close()
    assert [release.size_bytes for release in releases] == [0, int(1.4 * 1024**3)]

    # Any other failure inside one item skips only that item.
    real_group = nyaa_module.release_group

    def group(title: str) -> str:
        if "01" in title:
            raise ValueError("broken title")
        return real_group(title)

    monkeypatch.setattr(nyaa_module, "release_group", group)
    assert [release.title for release in parse_rss(feed)] == ["[Group] Show - 02 [1080p].mkv"]


# 4. .torrent downloads are capped in size and time.


class _Body:
    """A response body that counts how much of it was read."""

    def __init__(self, chunks: int, size: int, on_chunk=None) -> None:
        self.chunks, self.size, self.pulled, self.on_chunk = chunks, size, 0, on_chunk

    def __iter__(self):
        for _ in range(self.chunks):
            self.pulled += 1
            if self.on_chunk:
                self.on_chunk()
            yield b"d" + b"0" * (self.size - 1)


def test_torrent_download_stops_at_the_size_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mib = 1024 * 1024
    aria2_body, nyaa_body = _Body(30, mib), _Body(30, mib)
    bodies = {"aria2.example": aria2_body, "nyaa.example": nyaa_body}
    _patch_httpx_client(monkeypatch, lambda request: httpx.Response(200, content=bodies[request.url.host]))

    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False, source_proxy_mode="direct")
    try:
        assert client._torrent_payload("https://aria2.example/x.torrent") is None
    finally:
        client.close()
    nyaa = NyaaClient("https://nyaa.example", proxy_mode="direct")
    try:
        with pytest.raises(NyaaError):
            nyaa.fetch_torrent_payload("https://nyaa.example/download/1.torrent")
    finally:
        nyaa.close()
    assert aria2_body.pulled <= 11  # 10 MB cap: stopped right after it, not at 30 MB
    assert nyaa_body.pulled <= 13  # 12 MB cap


def test_slow_torrent_download_stops_at_the_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"now": 1000.0}

    def tick() -> None:
        clock["now"] += 10.0  # every small chunk takes 10 s

    monkeypatch.setattr(base_module, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    bodies: list[_Body] = []

    def handler(_request):
        bodies.append(_Body(12, 1024, on_chunk=tick))
        return httpx.Response(200, content=bodies[-1])

    _patch_httpx_client(monkeypatch, handler)
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False, source_proxy_mode="direct")
    try:
        assert client._torrent_payload("https://aria2.example/x.torrent") is None
    finally:
        client.close()
    nyaa = NyaaClient("https://nyaa.example", proxy_mode="direct")
    try:
        with pytest.raises(NyaaError):
            nyaa.fetch_torrent_payload("https://nyaa.example/download/1.torrent")
    finally:
        nyaa.close()
    assert [body.pulled for body in bodies] == [7, 7]  # 60 s deadline, not all 120 s


# 5./6. aria2 ownership and secret handling.


class _FakeAria2:
    """aria2c's JSON-RPC; ``secret=None`` is an aria2 started without --rpc-secret."""

    def __init__(self, secret: str | None, session: str = "session-1") -> None:
        self.secret = secret
        self.session = session
        self.calls: list[str] = []
        self.wrong_token_calls = 0
        self.running = True

    def handle(self, request: httpx.Request) -> httpx.Response:
        if not self.running:
            raise httpx.ConnectError("connection refused", request=request)
        body = json.loads(request.content)
        method = body["method"]
        if self.secret is not None and body["params"][:1] != [f"token:{self.secret}"]:
            self.wrong_token_calls += 1
            error = {"code": 1, "message": "Unauthorized"}
            return httpx.Response(400, json={"jsonrpc": "2.0", "id": body["id"], "error": error})
        self.calls.append(method)
        if method == "aria2.forceShutdown":
            self.running = False
        result = {
            "aria2.getVersion": {"version": "1.37.0"},
            "aria2.getSessionInfo": {"sessionId": self.session},
            "aria2.getGlobalOption": {},
            "aria2.getGlobalStat": {"numActive": "0", "numWaiting": "0"},
        }.get(method, "OK")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})


def _aria2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sidecar_secret) -> tuple[Aria2Client, _FakeAria2]:
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=True)
    (tmp_path / "aria2" / "runtime-profile").write_text("v4-older-pudge\n", encoding="utf-8")
    sidecar = _FakeAria2(client._secret if sidecar_secret == "pudge" else sidecar_secret)
    client._http.close()
    client._http = REAL_HTTPX_CLIENT(transport=httpx.MockTransport(sidecar.handle))
    monkeypatch.setattr(client, "_start", lambda: sidecar.calls.append("START"))
    monkeypatch.setattr(aria2_module.time, "sleep", lambda _seconds: None)
    return client, sidecar


def test_foreign_aria2_on_pudge_port_is_never_reconfigured_or_stopped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, foreign = _aria2(tmp_path, monkeypatch, None)  # accepts any token
    try:
        assert client._probe() is True
        with pytest.raises(Aria2Error, match="6801"):
            client.ensure_running()
        assert client.shutdown(save_session=True) is False
        assert client.stop_when_idle() is False
    finally:
        client.close()
    touched = {
        "aria2.changeGlobalOption",
        "aria2.saveSession",
        "aria2.forceShutdown",
        "aria2.shutdown",
        "START",
    }
    assert not touched & set(foreign.calls), foreign.calls


def test_own_aria2_is_recognised_once_per_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client, own = _aria2(tmp_path, monkeypatch, "pudge")
    (tmp_path / "aria2" / "runtime-profile").write_text(client._RUNTIME_PROFILE + "\n", encoding="utf-8")
    try:
        client.ensure_running()
        client.ensure_running()
        assert own.wrong_token_calls == 1  # aria2 delays wrong-token answers: proved once
        assert "aria2.changeGlobalOption" in own.calls
        assert client.shutdown(save_session=True) is True
        assert own.calls[-2:] == ["aria2.saveSession", "aria2.forceShutdown"]
    finally:
        client.close()


def test_rpc_secret_is_private_and_not_on_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Record the mode the secret file has as soon as it exists: a later chmod
    # must not be what makes it private.
    monkeypatch.setattr(Path, "chmod", lambda self, mode: None)
    previous = os.umask(0o022)
    try:
        client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=True)
    finally:
        os.umask(previous)
    secret_file = tmp_path / "aria2" / "rpc-secret"
    assert stat.S_IMODE(secret_file.stat().st_mode) == 0o600

    launches: list[list[str]] = []
    sidecar = _FakeAria2(secret="")

    def popen(command, **_kwargs):
        launches.append(list(command))
        conf = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--conf-path="))
        # aria2 reads name=value lines from --conf-path; the fake enforces that secret.
        values = dict(line.split("=", 1) for line in Path(conf).read_text().splitlines() if line)
        sidecar.secret = values["rpc-secret"]
        return SimpleNamespace(poll=lambda: None, pid=4242)

    client._http.close()
    client._http = REAL_HTTPX_CLIENT(transport=httpx.MockTransport(sidecar.handle))
    monkeypatch.setattr(client, "_binary_path", lambda: "/opt/homebrew/bin/aria2c")
    monkeypatch.setattr(aria2_module, "subprocess", SimpleNamespace(**{**vars(subprocess), "Popen": popen}))
    monkeypatch.setattr(aria2_module, "time", SimpleNamespace(**{**vars(time), "sleep": lambda _s: None}))
    try:
        client.ensure_running()
    finally:
        client.close()

    (command,) = launches
    assert not any(client._secret in arg for arg in command)
    assert "--no-conf=true" not in command  # it would make aria2 ignore --conf-path
    conf = tmp_path / "aria2" / "aria2-rpc.conf"
    assert f"--conf-path={conf}" in command
    assert conf.read_text(encoding="utf-8") == f"rpc-secret={client._secret}\n"
    assert stat.S_IMODE(conf.stat().st_mode) == 0o600
    assert sidecar.secret == client._secret and "aria2.changeGlobalOption" in sidecar.calls


# 7. qBittorrent probing sends no credentials and needs real qBittorrent evidence.


def test_qbittorrent_probe_sends_no_key_and_ignores_other_services(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, str]] = []
    login_page = "<html><head><title>qBittorrent WebUI</title></head></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        port = request.url.port
        seen.append((port, request.headers.get("authorization", "")))
        if port == 8080:
            raise httpx.ConnectError("connection refused")  # the configured address is down
        if port == 8081:  # some other local service that needs a login
            return httpx.Response(403, text="<html>Admin console</html>")
        if port == 8082:  # starts with "v" but is no version
            return httpx.Response(200, text="valid")
        if port == 8090:  # qBittorrent that requires authentication
            if request.url.path == "/":
                return httpx.Response(200, text=login_page)
            return httpx.Response(403, text="Forbidden")
        if port == 8091:
            return httpx.Response(200, text="v5.1.2")
        raise httpx.ConnectError("connection refused")

    client = QBittorrentClient("http://127.0.0.1:8080", api_key="secret-api-key")
    monkeypatch.setattr(client, "_config_candidate_urls", lambda: [])
    monkeypatch.setattr(client, "_lsof_candidate_urls", lambda: [])
    _patch_httpx_client(monkeypatch, handler)
    try:
        assert client._probe("http://127.0.0.1:8081") == (False, "")
        assert client._probe("http://127.0.0.1:8082") == (False, "")
        assert client._probe("http://127.0.0.1:8090") == (True, "")
        assert client._probe("http://127.0.0.1:8091") == (True, "v5.1.2")
        assert client._discover_local_webui() == "http://127.0.0.1:8090"
    finally:
        client.close_shared()
    probes = [auth for port, auth in seen if port != 8080]
    assert probes and set(probes) == {""}  # the API key went to no candidate port


# 8. An empty LLM reply is "no answer", not a crash.


@pytest.mark.parametrize("reply", [{"choices": []}, {"choices": [{"finish_reason": "stop"}]}, {}])
def test_empty_llm_reply_returns_none(reply: dict) -> None:
    client = OllamaClient(LLMConfig(provider="openai", base_url="https://gateway.example", model="m"))
    client.client.close()
    client.client = REAL_HTTPX_CLIENT(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=reply))
    )
    try:
        assert client.json_chat("system", "user") is None
        assert "empty content" in client.last_error
        assert client.chat_text([{"role": "user", "content": "hi"}]) is None
        assert "empty content" in client.last_error
    finally:
        client.close()
