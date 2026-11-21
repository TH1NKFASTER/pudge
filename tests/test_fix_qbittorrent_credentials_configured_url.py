"""qBittorrent's login and API key go only to the address in Settings.

Discovery looks at other local ports when the configured one does not answer; any process
(another account's) can listen there and show a "qBittorrent" page. It used to receive
the user name and password, and after a switch even the API key.
"""

from __future__ import annotations

import httpx
import pytest

from pudge.branding import APP_NAME
from pudge.providers import qbittorrent as qbt_module
from pudge.providers.qbittorrent import QBittorrentClient, QBittorrentError
from tests.i18n_support import en

LOGIN_PAGE = "<html><head><title>qBittorrent WebUI</title></head></html>"


def _install(monkeypatch, handler) -> None:
    real_client = httpx.Client

    def make(*args, **kwargs):
        kwargs.pop("proxy", None)
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", make)

    def new_client(self, base_url):
        return qbt_module._SessionClient(
            base_url=base_url,
            timeout=self.timeout,
            follow_redirects=True,
            headers=self._headers(base_url),
            relogin=self._relogin_after_403,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(QBittorrentClient, "_new_client", new_client)


def _client(monkeypatch, handler, **kwargs) -> QBittorrentClient:
    _install(monkeypatch, handler)
    client = QBittorrentClient("http://127.0.0.1:8080", auto_start_app=False, **kwargs)
    monkeypatch.setattr(client, "_config_candidate_urls", lambda: [])
    monkeypatch.setattr(client, "_lsof_candidate_urls", lambda: [])
    return client


def test_a_discovered_web_ui_that_needs_a_login_never_gets_the_password(monkeypatch) -> None:
    seen: list[tuple[int, str, str, bytes]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.port, request.method, request.url.path, request.read()))
        if request.url.port == 8080:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.port == 8090:  # "qBittorrent" that requires a login
            if request.url.path == "/":
                return httpx.Response(200, text=LOGIN_PAGE)
            if request.url.path == "/api/v2/auth/login":
                return httpx.Response(200, text="Ok.")
            return httpx.Response(403, text="Forbidden")
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(monkeypatch, handler, username="admin", password="hunter2-password")
    try:
        with pytest.raises(QBittorrentError) as caught:
            client.version()
    finally:
        client.close_shared()
    assert str(caught.value) == en(
        "error.qbt.webUiElsewhere",
        url="http://127.0.0.1:8090",
        configured="http://127.0.0.1:8080",
        app=APP_NAME,
    )
    sent_elsewhere = [entry for entry in seen if entry[0] != 8080]
    assert sent_elsewhere
    assert all(b"hunter2-password" not in body for _port, _method, _path, body in sent_elsewhere)
    assert not any(path == "/api/v2/auth/login" for port, _m, path, _b in sent_elsewhere)
    # The configured address did get the login attempt.
    assert any(port == 8080 and path == "/api/v2/auth/login" for port, _m, path, _b in seen)


def test_a_discovered_web_ui_without_a_login_works_and_gets_no_api_key(monkeypatch) -> None:
    seen: list[tuple[int, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.port, request.url.path, request.headers.get("authorization", "")))
        if request.url.port == 8091 and request.url.path == "/api/v2/app/version":
            return httpx.Response(200, text="v5.1.2")  # "bypass authentication for localhost"
        if request.url.port == 8091 and request.url.path == "/api/v2/torrents/categories":
            return httpx.Response(200, json={})
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(monkeypatch, handler, api_key="secret-api-key")
    try:
        assert client.version() == "v5.1.2"
        assert client.base_url == "http://127.0.0.1:8091"
        assert client.categories() == {}
    finally:
        client.close_shared()
    elsewhere = [auth for port, _path, auth in seen if port != 8080]
    assert elsewhere and set(elsewhere) == {""}
    assert any(port == 8080 and auth == "Bearer secret-api-key" for port, _path, auth in seen)


@pytest.mark.parametrize(
    "configured, candidate, allowed",
    [
        ("http://127.0.0.1:8080", "http://localhost:8080", True),
        ("http://localhost:8080", "http://127.0.0.1:8080/", True),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8090", False),
        ("http://127.0.0.1:8080", "https://127.0.0.1:8080", False),
        ("http://nas.local:8080", "http://127.0.0.1:8080", False),
    ],
)
def test_only_the_configured_address_and_its_loopback_aliases_get_credentials(
    configured: str, candidate: str, allowed: bool
) -> None:
    client = QBittorrentClient(configured, auto_start_app=False)
    try:
        assert client._credentials_allowed(candidate) is allowed
    finally:
        client.close_shared()


def test_the_message_is_in_both_languages() -> None:
    from tests.i18n_support import ru

    for tr in (en, ru):
        text = tr(
            "error.qbt.webUiElsewhere", url="http://127.0.0.1:8090", configured="http://x:1", app="Pudge"
        )
        assert "http://127.0.0.1:8090" in text and "http://x:1" in text and "{" not in text
