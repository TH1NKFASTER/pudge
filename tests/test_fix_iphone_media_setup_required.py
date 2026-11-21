"""Second review of the media error mapping: every RuntimeError was final (HTTP 422), so a Jiten
rate limit or outage, a release-search error, a manager error or one raised while Pudge shuts
down made the phone drop what it sent. Now only errors that last until the user sets something
up are final: LightNovelSetupRequired / ReviewSetupRequired (a missing Jiten, jpdb or AniList
token), with KeyError, ValueError and the item-refusing MediaError. Everything else is 503 with
its reason, and the phone sends it again later."""

from __future__ import annotations

import http.client
import json
import threading
from types import SimpleNamespace

import pytest

from pudge.i18n import err
from pudge.iphone_http import IPhoneHTTPServer, media_refusal
from pudge.iphone_media import MediaError, MediaTemporaryError
from pudge.light_novels import JitenMutationUnknown, LightNovelError, LightNovelSetupRequired
from pudge.review_providers import (
    JitenReviewProvider,
    JpdbReviewProvider,
    ReviewProviderError,
    ReviewSetupRequired,
)


def test_a_missing_token_raises_the_setup_required_errors():
    for provider, key in (
        (JitenReviewProvider, "ln.jitenTokenMissing"),
        (JpdbReviewProvider, "ln.jpdbTokenMissing"),
    ):
        with pytest.raises(ReviewSetupRequired) as caught:
            provider("")
        assert str(caught.value) == err(key) and isinstance(caught.value, ReviewProviderError)


def test_a_light_novel_request_without_a_jiten_token_is_setup_required():
    from pudge.light_novels import LightNovelService

    service = LightNovelService.__new__(LightNovelService)
    service.settings = lambda: SimpleNamespace(jiten_api_key="")
    with pytest.raises(LightNovelSetupRequired) as caught:
        service._jiten_request("reader/parse", {})
    assert str(caught.value) == err("ln.jitenTokenMissing") and isinstance(caught.value, LightNovelError)


def _rate_limited() -> Exception:
    try:
        from pudge.jiten_client import JitenRefused
    except ImportError:  # before the shared Jiten client: the light-novel error it raised then
        return LightNovelError("Jiten is rate limiting requests; try again in a minute")
    return JitenRefused("Jiten is rate limiting requests", reason="rate_limit", retry_after=30)


def _nyaa_error() -> Exception:
    from pudge.providers.nyaa import NyaaError

    return NyaaError("nyaa.si did not answer")


@pytest.mark.parametrize(
    ("make", "status"),
    [
        (lambda: LightNovelSetupRequired(err("ln.jitenTokenMissing")), 422),
        (lambda: ReviewSetupRequired(err("ln.jpdbTokenMissing")), 422),
        (lambda: MediaError("This episode is no longer on the Mac"), 422),
        (lambda: ValueError("Invalid position"), 422),
        (lambda: KeyError("episode"), 422),
        (lambda: MediaTemporaryError("The Mac app is still starting"), 503),
        (lambda: RuntimeError("cannot schedule new futures after shutdown"), 503),
        (lambda: LightNovelError("Jiten returned HTTP 502"), 503),
        (lambda: JitenMutationUnknown("Jiten did not answer"), 503),
        (lambda: ReviewProviderError("Jiten refused the review"), 503),
        (_rate_limited, 503),
        (_nyaa_error, 503),
    ],
)
def test_each_failure_maps_to_its_status_with_its_reason(make, status):
    error = make()
    assert media_refusal(error) is (status == 422)
    server = IPhoneHTTPServer(("127.0.0.1", 0), _Service(error))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
        connection.request(
            "POST",
            "/api/v2/media/study/action",
            json.dumps({"wordId": 1}),
            {"Authorization": "Bearer token", "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        connection.close()
        assert response.status == status
        reason = (
            "This item is no longer on the Mac"
            if isinstance(error, KeyError)
            else (str(error).strip() or type(error).__name__)[:400]
        )
        assert body["error"] == reason
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


class _Media:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    def handle(self, command, route, query, body, device=""):
        raise self.error


class _Service:
    server_id = "server"

    def __init__(self, error: BaseException) -> None:
        self.media = _Media(error)

    def authenticate(self, token):
        return "phone"
