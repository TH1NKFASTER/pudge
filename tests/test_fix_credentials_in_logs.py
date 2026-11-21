"""Credentials never reach the runtime log, Copy diagnostics or debug bundles (WEBSEC-1)."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from pudge.diagnostics import DebugBundleBuilder
from pudge.logging_utils import configure_logging
from pudge.providers.nyaa import NyaaClient, NyaaError
from pudge.url_credentials import (
    mask_url_password,
    redact_url_credentials,
    restore_url_password,
    url_for_log,
)
from pudge.secrets_store import SECRET_MASK

sanitize = DebugBundleBuilder._sanitize_text


@pytest.mark.parametrize(
    "text, secret",
    [
        ("Release search unavailable through socks5://alice:Hunter2pw@127.0.0.1:9", "Hunter2pw"),
        ("socks5h://bob:s3cr3t-pass@vpn.example:1080 refused", "s3cr3t-pass"),
        ("redis://user:pa55word@cache:6379/0", "pa55word"),
        ("https://alice:topsecret@tracker.example/announce", "topsecret"),
        ("curl -H X-Pudge-Token: tok-abcdef123456 http://127.0.0.1:5000/api/mpv/explain", "tok-abcdef123456"),
        (
            '{"jsonrpc":"2.0","method":"aria2.tellStatus","params":["token:Aria2Secret99","gid"]}',
            "Aria2Secret99",
        ),
        ("params=['token:Aria2Secret99', 'gid']", "Aria2Secret99"),
        ("PUDGE_ASSISTANT_TOKEN=assistant-secret-1", "assistant-secret-1"),
        ('"token": "plain-json-token"', "plain-json-token"),
        ("x-api-key: key-0123456789", "key-0123456789"),
        ("http://127.0.0.1:53111/k/PageKey_0123456789abc/covers/a.jpg", "PageKey_0123456789abc"),
    ],
)
def test_sanitize_text_scrubs_every_credential_form(text: str, secret: str) -> None:
    cleaned = sanitize(text)
    assert secret not in cleaned
    assert "[REDACTED]" in cleaned


def test_sanitize_text_keeps_the_proxy_host_and_ordinary_words() -> None:
    cleaned = sanitize("through socks5://alice:pw@127.0.0.1:9 error='timed out'")
    assert "socks5://[REDACTED]@127.0.0.1:9" in cleaned and "timed out" in cleaned
    # Not credentials: an e-mail in a query, token counters, a tokenizer name.
    unchanged = "http://example.com/x?e=a@b.com max_tokens=500 tokens: 5 tokenizer: fugashi"
    assert sanitize(unchanged) == unchanged


def test_redact_hides_json_args_and_header_keys() -> None:
    payload = {
        "args": ["aria2c", "--rpc-secret=abc"],
        "frontend": {"X-Pudge-Token": "tok", "note": "via socks5://u:p4ss@h:1"},
    }
    redacted = DebugBundleBuilder._redact(payload)
    assert redacted["args"] == "[REDACTED]"
    assert redacted["frontend"]["X-Pudge-Token"] == "[REDACTED]"
    assert "p4ss" not in redacted["frontend"]["note"]
    # A JSON log line goes through the same redaction.
    line = json.dumps({"event": "rpc", "args": ["token:zzz"], "url": "socks5://u:p4ss@h:1"}) + "\n"
    exported = DebugBundleBuilder._sanitized_lines([line])
    assert "zzz" not in exported and "p4ss" not in exported


def test_url_helpers_log_mask_and_restore_a_proxy_password() -> None:
    proxy = "socks5://alice:Hunter2pw@127.0.0.1:1080"
    assert url_for_log(proxy) == "socks5://127.0.0.1:1080"
    assert url_for_log("alice:pw@[::1]:1080") == "[::1]:1080"
    masked = mask_url_password(proxy)
    assert masked == f"socks5://alice:{SECRET_MASK}@127.0.0.1:1080"
    assert restore_url_password(masked, proxy) == proxy
    # A typed password wins; no saved one drops the mask. (An edited host: see below.)
    assert (
        restore_url_password("socks5://alice:new@127.0.0.1:1080", proxy)
        == "socks5://alice:new@127.0.0.1:1080"
    )
    assert restore_url_password(masked, "socks5://127.0.0.1:1080") == "socks5://alice@127.0.0.1:1080"
    assert mask_url_password("socks5://[::1]:1080") == "socks5://[::1]:1080"
    assert redact_url_credentials("no url here") == "no url here"


def test_nyaa_error_names_the_proxy_without_its_password() -> None:
    proxy = "socks5://alice:Hunter2pw@127.0.0.1:9"
    client = NyaaClient("https://nyaa.example", proxy_mode="proxy_only", proxy_url=proxy)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client._clients[client.proxy_url] = httpx.Client(transport=httpx.MockTransport(refuse))
    with pytest.raises(NyaaError) as caught:
        client._get("https://nyaa.example/?q=x", client.proxy_url)
    message = str(caught.value)
    assert "socks5://127.0.0.1:9" in message
    assert "Hunter2pw" not in message and "alice" not in message


def test_runtime_log_never_holds_a_url_password(tmp_path) -> None:
    log_path = tmp_path / "runtime.log"
    logger = configure_logging(log_path)
    added = [handler for handler in logger.handlers if getattr(handler, "baseFilename", "") == str(log_path)]
    try:
        logger.warning("FALLBACK step=nyaa.search error=%r", "through socks5://alice:Hunter2pw@127.0.0.1:9")
        for handler in added:
            handler.flush()
        text = log_path.read_text(encoding="utf-8")
    finally:
        for handler in added:
            logger.removeHandler(handler)
            handler.close()
    assert "Hunter2pw" not in text
    assert "socks5://[REDACTED]@127.0.0.1:9" in text
    assert logging.getLogger("pudge") is logger


@pytest.mark.parametrize(
    "edited",
    [
        f"socks5://bob:{SECRET_MASK}@other.example:1080",  # another host
        f"socks5://bob:{SECRET_MASK}@good.example:1081",  # another port
        f"socks5h://bob:{SECRET_MASK}@good.example:1080",  # another scheme
        f"socks5://eve:{SECRET_MASK}@good.example:1080",  # another user
    ],
)
def test_a_masked_password_never_follows_an_edited_address(edited: str) -> None:
    saved = "socks5://bob:pw123@good.example:1080"
    restored = restore_url_password(edited, saved)
    assert "pw123" not in restored and SECRET_MASK not in restored
    assert restored == edited.replace(f":{SECRET_MASK}@", "@")
    # The same address (host case aside) gets the saved password back.
    assert restore_url_password(f"socks5://bob:{SECRET_MASK}@GOOD.example:1080", saved) == (
        "socks5://bob:pw123@GOOD.example:1080"
    )
