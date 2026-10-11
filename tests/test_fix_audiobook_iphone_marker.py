"""Audiobook card: each iPhone/iPad's differing position as a yellow, clickable mark on the seek bar."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from test_iphone_audio_repository import checkpoint, service  # noqa: F401
from test_iphone_handoff import setup_live

BOOK = {
    "id": 5,
    "title": "また、同じ夢を見ていた",
    "duration": 31000,
    "position": 191,
    "speed": 1.25,
    "chapters": [],
    "iphone_progress": {
        "revision": 4,
        "selected": {"deviceId": "mac"},
        "conflicts": [{"deviceId": "phone-1", "positionMs": 7750000}],
    },
    "iphone_position": {
        "checkpoint_id": "cp-phone",
        "revision": 4,
        "position": 7750,
        "device_name": "Maksim iPhone",
        "updated_at": 1791100000,
    },
}
BOOK["iphone_positions"] = [BOOK["iphone_position"]]
RESOLVED = {
    **BOOK,
    "position": 7750,
    "iphone_progress": {"revision": 5, "selected": {"deviceId": "phone-1"}, "conflicts": []},
    "iphone_position": None,
    "iphone_positions": [],
}


def test_mark_sits_on_the_seek_bar_with_its_position_in_the_tooltip(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('audiobooks', true);
            await __tick(500);
            const mark = document.querySelector('.audiobook-scrubber-shell .audiobook-iphone-marker');
            await __fire('.audiobook-iphone-marker', 'mouseover', {}, 'MouseEvent');
            const tip = document.getElementById('titleTooltip');
            return {
              left: mark?.style.left, tooltip: tip.classList.contains('show') ? tip.innerText : null,
              expected: PudgeI18n.t('media.devicePositionAt', {device: 'Maksim iPhone', time: '2:09:10'}),
              note: __text('.audiobook-sync-conflict'), short: PudgeI18n.t('media.positionDiffersOn', {devices: 'Maksim iPhone'}),
            };
            """,
            lang=lang,
            api={"audiobook_state": {"books": [BOOK]}},
        )
        out = result.value
        assert out["left"] == "25%"
        assert out["tooltip"].startswith(out["expected"] + ", ")
        assert out["note"] == out["short"]  # the short note stays


def test_clicking_the_mark_takes_the_iphone_position_and_clears_the_conflict(page) -> None:
    result = page.run(
        r"""
        setPage('audiobooks', true);
        await __tick(500);
        await __click('.audiobook-iphone-marker');
        await __tick(50);
        return {
          marks: document.querySelectorAll('.audiobook-iphone-marker').length,
          notes: document.querySelectorAll('.audiobook-sync-conflict').length,
          value: document.querySelector('[data-audio-position]')?.value,
          selected: document.querySelector('.audiobook-card.selected') !== null,
        };
        """,
        api={
            "audiobook_state": {"books": [BOOK]},
            "audiobook_use_iphone_position": {"ok": True, "book": RESOLVED},
        },
    )
    assert result.calls_of("audiobook_use_iphone_position") == [[5, "cp-phone", 4]]
    assert result.value["marks"] == 0 and result.value["notes"] == 0
    assert str(result.value["value"]) == "7750"
    assert result.value["selected"] is False  # the mark is a control, not a card-selection click


def test_a_stale_choice_reloads_the_current_positions(page) -> None:
    result = page.run(
        r"""
        setPage('audiobooks', true);
        await __tick(500);
        const before = __apiCalls.filter(call => call.name === 'audiobook_state').length;
        await __click('.audiobook-iphone-marker');
        await __tick(50);
        return {
          reloaded: __apiCalls.filter(call => call.name === 'audiobook_state').length > before,
          toasts: __toasts(), expected: PudgeI18n.t('media.iphonePositionChanged'),
        };
        """,
        before="__api.audiobook_use_iphone_position = async () => { throw new Error('Progress changed; refresh choices'); };",
        api={"audiobook_state": {"books": [BOOK]}},
    )
    assert result.value["reloaded"] is True
    assert result.value["expected"] in result.value["toasts"]


def test_no_mark_without_a_conflict(page) -> None:
    result = page.run(
        "setPage('audiobooks', true); await __tick(500); return document.querySelectorAll('.audiobook-iphone-marker').length;",
        api={"audiobook_state": {"books": [RESOLVED]}},
    )
    assert result.value == 0


def test_phone_candidate_and_choosing_it_keeps_the_live_mac_player_in_sync(service):  # noqa: F811
    device, manifest, _ = setup_live(service)
    service.record_mac_position(manifest["assetId"], "live-one", 5000)
    phone = checkpoint(manifest, device, position=40000, reason="pause")
    assert service.record_checkpoint(device, phone)["status"] == "conflict"
    progress = service.progress(manifest["assetId"])
    choice = service.phone_position_choice(progress)
    assert choice["checkpoint_id"] == phone["checkpointId"]
    assert choice["position"] == 40.0 and choice["revision"] == progress["revision"]
    assert choice["device_name"] == "Phone"

    assert service.use_phone_position(1, choice["checkpoint_id"], choice["revision"]) == 40.0
    assert service.progress(manifest["assetId"])["conflicts"] == []
    # The Mac keeps playing from there: its next save is accepted, no new conflict.
    assert service.record_mac_position(manifest["assetId"], "live-one", 41000)["status"] == "accepted"
    assert service.progress(manifest["assetId"])["conflicts"] == []
    assert service.phone_position_choice(service.progress(manifest["assetId"])) is None


def test_mac_only_conflicts_have_no_phone_candidate(service):  # noqa: F811
    progress = {"revision": 3, "selected": {"deviceId": "mac"}, "conflicts": [{"deviceId": "mac"}]}
    assert service.phone_position_choice(progress) is None


def test_api_seeks_a_playing_mac_player_and_returns_the_resolved_book():
    from pudge.web_app import WebAppApi

    calls = []

    class Service:
        def use_phone_position(self, book_id, checkpoint_id, revision):
            calls.append(("resolve", book_id, checkpoint_id, revision))
            return 90.0

        def asset_for_book(self, book_id):
            return "asset"

        def progress(self, asset):
            return {"revision": 5, "selected": {"deviceId": "phone"}, "conflicts": []}

        def playing_elsewhere(self, progress, here):
            return False

        def phone_position_choices(self, progress):
            return []

        def device_name(self, device):
            return "Phone"

    class Audio:
        def is_playing(self, book_id):
            return True

        def seek_to(self, book_id, position):
            calls.append(("seek", book_id, position))

        def book(self, book_id):
            return {"id": book_id, "position": 90.0}

    api = SimpleNamespace(
        _iphone_service=Service(), _iphone_server=object(), audiobooks=Audio(), logger=logging.getLogger("t")
    )
    api._iphone_audio_state = lambda state: WebAppApi._iphone_audio_state(api, state)
    result = WebAppApi.audiobook_use_iphone_position(api, 5, "cp", 4)
    assert calls == [("resolve", 5, "cp", 4), ("seek", 5, 90.0)]
    assert result["book"]["iphone_progress"]["conflicts"] == [] and result["book"]["iphone_position"] is None


TWO_DEVICES = {
    **BOOK,
    "iphone_positions": [
        {
            **BOOK["iphone_position"],
            "checkpoint_id": "cp-ipad",
            "position": 3100,
            "device_name": "Maksim iPad",
        },
        BOOK["iphone_position"],
    ],
}


def test_each_differing_device_has_its_own_named_mark_and_the_note_names_them(page) -> None:
    result = page.run(
        r"""
        setPage('audiobooks', true);
        await __tick(500);
        const marks = [...document.querySelectorAll('.audiobook-iphone-marker')];
        return {
          marks: marks.map(mark => [mark.dataset.checkpointId, mark.style.left, mark.getAttribute('aria-label')]),
          note: __text('.audiobook-sync-conflict'),
          short: PudgeI18n.t('media.positionDiffersOn', {devices: 'Maksim iPad, Maksim iPhone'}),
          ipad: PudgeI18n.t('media.devicePositionAt', {device: 'Maksim iPad', time: '51:40'}),
        };
        """,
        api={"audiobook_state": {"books": [TWO_DEVICES]}},
    )
    out = result.value
    assert [mark[:2] for mark in out["marks"]] == [["cp-ipad", "10%"], ["cp-phone", "25%"]]
    assert out["marks"][0][2].startswith(out["ipad"])
    assert out["note"] == out["short"]


def test_choosing_one_device_resolves_for_all(page) -> None:
    result = page.run(
        r"""
        setPage('audiobooks', true);
        await __tick(500);
        await __click('.audiobook-iphone-marker[data-checkpoint-id="cp-ipad"]');
        await __tick(50);
        return document.querySelectorAll('.audiobook-iphone-marker').length;
        """,
        api={
            "audiobook_state": {"books": [TWO_DEVICES]},
            "audiobook_use_iphone_position": {"ok": True, "book": RESOLVED},
        },
    )
    assert result.calls_of("audiobook_use_iphone_position") == [[5, "cp-ipad", 4]]
    assert result.value == 0
