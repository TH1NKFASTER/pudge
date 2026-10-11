from __future__ import annotations

import json

import httpx
import pytest

from pudge import i18n
from pudge.providers.aria2 import Aria2Client, Aria2Error

INFO_HASH = "4ae3d4b812e7ba766d008d04b41b79344b7ad0bb"


def _client(tmp_path, respond):
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    client._http.close()

    def handle(request):
        payload = json.loads(request.content)
        method, params = payload["method"], payload["params"][1:]
        if method == "aria2.getVersion":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": payload["id"],
                    "result": {"version": "1.37.0", "enabledFeatures": ["BitTorrent"]},
                },
            )
        status, body = respond(method, params)
        return httpx.Response(status, json={"jsonrpc": "2.0", "id": payload["id"], **body})

    client._http = httpx.Client(transport=httpx.MockTransport(handle))
    return client


def test_real_aria2_http400_invalid_gid_means_absent(tmp_path):
    # Real aria2 returns HTTP 400 for RPC errors; an unknown info hash must be
    # treated as "not added yet", not as an unreachable RPC (Ao no Hako S2 stall).
    def respond(method, params):
        if method in {"aria2.tellActive", "aria2.tellWaiting", "aria2.tellStopped"}:
            return 200, {"result": []}
        assert method == "aria2.tellStatus"
        return 400, {"error": {"code": 1, "message": f"Invalid GID {params[0]}"}}

    client = _client(tmp_path, respond)
    try:
        assert client.torrent_status(INFO_HASH) is None
    finally:
        client.close()


def test_http400_error_message_is_structured(tmp_path):
    client = _client(
        tmp_path, lambda method, params: (400, {"error": {"code": 1, "message": "permission denied"}})
    )
    try:
        with pytest.raises(Aria2Error, match=r"^aria2 RPC: permission denied$"):
            client._rpc_raw("aria2.tellStatus", ["x"])
    finally:
        client.close()


def test_http_error_without_json_body_still_reports_unreachable(tmp_path):
    client = Aria2Client(state_dir=tmp_path / "aria2", auto_start=False)
    client._http.close()
    client._http = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(502, text="bad gateway"))
    )
    try:
        with pytest.raises(Aria2Error, match=r"^aria2 RPC unavailable: ") as caught:
            client._rpc_raw("aria2.tellStatus", ["x"])
        assert str(caught.value).endswith("; aria2 response: bad gateway")
        assert i18n.message(str(caught.value), "ru").startswith("aria2 RPC недоступен: ")
    finally:
        client.close()
