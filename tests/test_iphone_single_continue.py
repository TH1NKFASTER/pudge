from __future__ import annotations

from source_policy import source
from test_iphone_audio_repository import archived, checkpoint, service  # noqa: F401
from tests.i18n_support import has_text


def test_phone_playing_now_is_reported_only_for_a_fresh_non_paused_checkpoint(service, monkeypatch):
    import pudge.iphone_service as module

    device, manifest = archived(service)
    service.record_checkpoint(device, checkpoint(manifest, device, reason="periodic"))
    state = service.progress(manifest["assetId"])
    assert service.playing_elsewhere(state, "mac") is True
    assert service.progress_view(manifest["assetId"])["playingOn"] == device
    now = module.now_ms()
    monkeypatch.setattr(module, "now_ms", lambda: now + 60_000)
    assert service.playing_elsewhere(state, "mac") is False


def test_paused_phone_is_not_playing(service):
    device, manifest = archived(service)
    service.record_checkpoint(device, checkpoint(manifest, device, reason="pause"))
    assert service.progress_view(manifest["assetId"])["playingOn"] is None


def test_mac_playing_is_reported_from_the_live_player(service):
    from types import SimpleNamespace

    _, manifest = archived(service)
    service._record_mac_action(manifest["assetId"], 1000, "periodic")
    with service.db.connect() as conn:
        book = conn.execute(
            "SELECT book_id FROM iphone_assets WHERE id=?", (manifest["assetId"],)
        ).fetchone()[0]
    service.audiobooks = SimpleNamespace(is_playing=lambda b: b == book, is_paused=lambda b: False)
    assert service.progress_view(manifest["assetId"])["playingOn"] == "mac"
    service.audiobooks = SimpleNamespace(is_playing=lambda b: False, is_paused=lambda b: False)
    assert service.progress_view(manifest["assetId"])["playingOn"] is None


# The audiobook card on the Mac: the phone holds the newest position (deviceId) and plays now.
PHONE_BOOK = {
    "id": 3,
    "title": "Book",
    "duration": 600,
    "position": 120,
    "playing": False,
    "player_running": False,
    "iphone_live": True,
    "iphone_live_name": "Maksim iPad",
    "iphone_progress": {"selected": {"deviceId": "phone-1", "positionMs": 125000}, "conflicts": []},
}
CARD = r"""
setPage('audiobooks', true); await __tick(50);
const visible = () => [...document.querySelectorAll('[data-audiobook-id="3"] button')].filter(b => b.checkVisibility());
const out = {primary: visible().filter(b => b.classList.contains('primary')).map(b => ({text: b.innerText, title: b.title})),
             texts: visible().map(b => b.innerText),
             resume: PudgeI18n.t('ui.resume'), playing: PudgeI18n.t('media.playingOnDeviceNow', {device: 'Maksim iPad'})};
await __click('[data-audiobook-id="3"] [data-media-action="play-audio"]');
await __tick(50);
out.toasts = __toasts();
out.confirm = __visible('#pudgeConfirmBackdrop') ? __text('#pudgeConfirmMessage') : null;
// The playing device is named (several iPhones and iPads can be paired).
out.expected = {pausing: PudgeI18n.t('media.pausingDevice', {device: 'Maksim iPad'}),
                paused: PudgeI18n.t('media.devicePausedContinuingOnMac', {device: 'Maksim iPad'}),
                unconfirmed: PudgeI18n.t('media.deviceDidNotConfirmPause', {device: 'Maksim iPad'})};
if (out.confirm) { await __click('#pudgeConfirmBackdrop [data-pudge-confirm="ok"]'); await __tick(50); }
return out;
"""


def test_mac_card_has_one_resume_button_that_hands_off_from_a_playing_iphone(page):
    # One Resume button (no separate "Continue here from iPhone"): while the phone plays
    # this book it pauses the phone and the Mac continues from the phone's position.
    for lang in ("en", "ru"):
        result = page.run(
            CARD,
            lang=lang,
            api={
                "audiobook_state": {"books": [PHONE_BOOK]},
                "iphone_resume_on_mac": {"status": "source_paused", "targetStatus": "started"},
            },
        )
        out = result.value
        assert out["primary"] == [{"text": out["resume"], "title": out["playing"]}]
        assert out["texts"].count(out["resume"]) == 1 and not [
            text for text in out["texts"] if "iPhone" in text
        ]
        assert out["toasts"] == [out["expected"]["pausing"], out["expected"]["paused"]]
        [[book_id, operation, speed]] = result.calls_of("iphone_resume_on_mac")
        assert (book_id, speed) == (3, 1) and len(operation) == 36
        assert not result.called("audiobook_play")  # the backend started the Mac player itself
    assert not has_text("Continue here from iPhone") and not has_text("Продолжить здесь с iPhone", "ru")


def test_resume_without_a_playing_iphone_plays_on_the_mac_directly(page):
    paused_phone = {**PHONE_BOOK, "iphone_live": False}
    result = page.run(CARD, api={"audiobook_state": {"books": [paused_phone]}})
    assert result.value["primary"] == [{"text": result.value["resume"], "title": ""}]
    assert not result.called("iphone_resume_on_mac")
    assert result.calls_of("audiobook_play") == [[3, None, 1]]


def test_unconfirmed_handoff_asks_before_playing_from_the_saved_place(page):
    result = page.run(
        CARD,
        api={
            "audiobook_state": {"books": [PHONE_BOOK]},
            "iphone_resume_on_mac": {"status": "source_offline"},
        },
    )
    out = result.value
    assert out["confirm"] == out["expected"]["unconfirmed"]
    assert len(result.calls_of("iphone_resume_on_mac")) == 1
    assert result.calls_of("audiobook_play") == [[3, None, 1]]  # after the user confirmed


def test_iphone_book_screen_has_one_continue_button():
    # policy: the iPhone app (Swift) cannot run here and the Mac has no counterpart for the
    # phone's book screen; the Mac side of the handoff is tested above (playingOn, handoff).
    screen = source("ios/Pudge/UI/LibraryScreen.swift")
    assert "continueListening(book)" in screen
    assert "Продолжить здесь с Mac" not in screen
    assert "Продолжить с последнего сохранённого места" not in screen
