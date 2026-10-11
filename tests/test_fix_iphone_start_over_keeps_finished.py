"""iPhone "Start over" no longer clears Finished (audit item 1A, behaviour).

Finished is set when a book is finished and cleared only by an explicit "Mark unfinished".
The phone's Start over sends a `reset` checkpoint, which the Mac took as "unfinished"; now
it only moves the place to 0:00 and Finished stays. Mark unfinished (on the Mac or the
phone) is the checkpoint `unfinish`, which the Mac announces as the capability
"progress-unfinish" so a phone never sends it to an older Mac.
"""

from __future__ import annotations

import http.client
import json
import ssl

from test_fix_iphone_lan_addresses import network as network_fixture
from test_iphone_audio_repository import archived, checkpoint
from test_iphone_audio_repository import service as service_fixture
from test_iphone_desktop_integration import desktop_book

from pudge.iphone_http import start_iphone_server

service = service_fixture
network = network_fixture


def _mac_book(service, book):
    with service.db.connect() as conn:
        row = conn.execute("SELECT position,finished FROM audiobooks WHERE id=?", (book,)).fetchone()
    return float(row["position"]), bool(row["finished"])


def test_phone_start_over_keeps_finished_and_mark_unfinished_clears_it(service, tmp_path) -> None:
    device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    asset = manifest["assetId"]
    finish = checkpoint(manifest, device, position=60000, reason="finish")
    assert service.record_checkpoint(device, finish)["status"] == "accepted"
    assert service.progress(asset)["finished"] is True

    start_over = checkpoint(manifest, device, base=1, position=0, reason="reset")
    assert service.record_checkpoint(device, start_over)["status"] == "accepted"

    state = service.progress(asset)
    assert state["finished"] is True and state["selected"]["positionMs"] == 0
    assert _mac_book(service, book) == (0.0, True)  # the Mac's library too

    unfinish = checkpoint(manifest, device, base=2, position=0, reason="unfinish")
    assert service.record_checkpoint(device, unfinish)["status"] == "accepted"
    state = service.progress(asset)
    assert state["finished"] is False and state["selected"]["positionMs"] == 0
    assert _mac_book(service, book) == (0.0, False)
    # A "Mark unfinished" from a phone that is not playing does not look like playback there.
    assert service.progress_view(asset).get("playingOn") is None


def test_mac_mark_unfinished_is_the_unfinish_checkpoint(service, tmp_path) -> None:
    device, manifest = archived(service)
    book, _ = desktop_book(service, manifest, tmp_path)
    asset = manifest["assetId"]
    service.mark_mac_finished(book, True)
    service.mark_mac_finished(book, False, position=12.0)
    state = service.progress(asset)
    assert state["finished"] is False
    assert (state["selected"]["reason"], state["selected"]["positionMs"]) == ("unfinish", 12000)


def test_the_mac_announces_mark_unfinished_to_the_phone(tmp_path, network) -> None:
    from pudge.database import Database
    from pudge.iphone_service import IPhoneService

    mobile = IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")
    server, thread, _pin = start_iphone_server(mobile, host="127.0.0.1", port=0)
    try:
        context = ssl.create_default_context(cafile=str(mobile.root / "tls-cert.pem"))
        context.check_hostname = False
        connection = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context, timeout=5)
        try:
            connection.request("GET", "/api/v2/capabilities")
            answer = json.loads(connection.getresponse().read())
        finally:
            connection.close()
    finally:
        server.shutdown()
        server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)
    assert "progress-unfinish" in answer["capabilities"] and answer["protocolMajor"] == 2
