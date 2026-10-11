"""The iPhone Bonjour re-check (dns-sd every 30 s on a Mac) never outlives the API that started
it: turning access off, quitting, or a test's teardown stops it and its dns-sd processes. On the
user's Mac a test left it running and the next test's Popen spy saw a stray dns-sd."""

from __future__ import annotations

import os
import shutil
import threading

from pudge import web_app_iphone
from pudge.web_app import WebAppApi


class _Process:
    def __init__(self) -> None:
        self.terminated = False

    def poll(self):
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True


def _advertising_api() -> tuple[WebAppApi, threading.Event, _Process]:
    api = WebAppApi.__new__(WebAppApi)
    stop = threading.Event()
    process = _Process()
    api._iphone_bonjour_lock = threading.Lock()
    api._iphone_bonjour_stop = stop
    api._iphone_bonjour = {"en0": process}
    web_app_iphone._IPHONE_BONJOUR_OWNERS.add(api)
    return api, stop, process


def test_teardown_stops_every_bonjour_advertisement_and_its_recheck() -> None:
    api, stop, process = _advertising_api()
    web_app_iphone.stop_iphone_bonjour_everywhere()
    assert stop.is_set() and process.terminated and api._iphone_bonjour == {}


def test_tests_never_reach_the_real_dns_sd() -> None:
    found = shutil.which("dns-sd")
    assert found is not None and os.path.dirname(found).endswith("gui-stubs")
