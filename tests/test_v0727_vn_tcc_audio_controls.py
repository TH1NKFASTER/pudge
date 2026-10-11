from __future__ import annotations

import json
import threading

from pudge.visual_novels import VisualNovelService


def test_vn_false_preflight_is_diagnostic_not_capture_gate(monkeypatch) -> None:
    import pudge.visual_novels as module

    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    service = VisualNovelService()
    monkeypatch.setattr(service, "_screen_recording_access", lambda: False)
    entered = threading.Event()

    def fake_loop(_stop_event, _generation, window_id):
        assert window_id == 123
        entered.set()

    monkeypatch.setattr(service, "_capture_loop", fake_loop)
    state = service.start(123, "VN")
    assert entered.wait(1.0)
    state = service.state()
    assert state.get("error_code") != "permission_required"
    assert state.get("screen_recording_preflight") is False


def test_vn_actual_screencapturekit_permission_error_is_authoritative() -> None:
    class Error:
        def localizedDescription(self):
            return "The user declined Screen Recording permission"

        def code(self):
            return -1

    assert VisualNovelService._screen_capture_error_code(Error()) == "permission_required"


# Audiobook 4 on the Audiobooks page. window.__book is what the backend reports (the
# player moves it); speed changes wait until the test calls window.__saved().
AUDIOBOOK = {
    "id": 4,
    "title": "Audio",
    "duration": 600,
    "position": 30,
    "playing": False,
    "player_running": False,
    "speed": 1,
}
AUDIO_PAGE = r"""
window.__book = BOOK;
window.__saved = () => {};
__api.audiobook_state = async () => ({books: [{...window.__book}]});
__api.sidebar_audio_state = async () => ({books: [{...window.__book}]});
__api.audiobook_play = async () => { window.__book = {...window.__book, playing: true, player_running: true}; return {ok: true}; };
__api.audiobook_set_speed = (id, speed) => new Promise(resolve => { window.__saved = () => {
  window.__book = {...window.__book, speed}; resolve({ok: true, book: {...window.__book}}); }; });
"""
AUDIO_HELPERS = r"""
const card = () => document.querySelector('[data-audiobook-id="4"]');
const summary = () => __text('[data-audio-live-summary="4"]');
const speedMenu = '[data-audiobook-id="4"] .pudge-select:has([data-audio-speed]) .pudge-select-button';
const chooseSpeed = async label => {
  await __fire(speedMenu, 'pointerdown', {button: 0}, 'PointerEvent');
  [...document.querySelectorAll('.pudge-select-menu.open .pudge-select-option')].find(b => b.textContent === label).click();
  await __tick(10);
};
const live = () => ({summary: summary(), slider: card().querySelector('[data-audio-position]').value});
"""


def audio_page(**book) -> str:
    return AUDIO_PAGE.replace("BOOK", json.dumps({**AUDIOBOOK, **book}))


def test_live_poll_updates_a_playing_audiobook_card_in_place(page) -> None:
    # The poll rebuilt the playing card every second: an open speed list closed under the
    # pointer and the chosen speed never reached the player.
    result = page.run(
        AUDIO_HELPERS
        + r"""
        setPage('audiobooks', true); await __tick(100);
        await __click('[data-audiobook-id="4"] [data-media-action="play-audio"]'); await __tick(50);
        const playing = card();
        await __fire(speedMenu, 'pointerdown', {button: 0}, 'PointerEvent');      // the speed list is open...
        window.__book = {...window.__book, position: 90}; await __tick(2500);     // ...while the player moves on
        const out = {same: playing === card(), ...live(), menu: document.querySelectorAll('.pudge-select-menu.open').length,
                     expected: `1:30 / 10:00 · 15% · ${PudgeI18n.t('media.timeLeft', {time: '8:30'})}`};
        [...document.querySelectorAll('.pudge-select-menu.open .pudge-select-option')].find(b => b.textContent === '1.5×').click();
        await __tick(10); window.__saved(); await __tick(50);
        return out;
        """,
        before=audio_page(),
    )
    out = result.value
    assert out["same"] is True and out["menu"] == 1
    assert (out["summary"], out["slider"]) == (out["expected"], "90")
    assert result.calls_of("audiobook_set_speed") == [[4, 1.5]]


def test_pressing_the_position_slider_holds_live_updates_until_a_speed_is_chosen(page) -> None:
    # A press on the slider keeps the card still for 15 s (the native control must not be
    # replaced under the pointer); choosing a speed ends that pause at once.
    result = page.run(
        AUDIO_HELPERS
        + r"""
        setPage('audiobooks', true); await __tick(100);
        await __click('[data-audiobook-id="4"] [data-media-action="play-audio"]'); await __tick(50);
        await __fire('[data-audiobook-id="4"] [data-audio-position]', 'pointerdown', {button: 0}, 'PointerEvent');
        window.__book = {...window.__book, position: 90}; await __tick(10000);
        const pressed = live();
        await __tick(6000);
        const later = live();
        await __fire('[data-audiobook-id="4"] [data-audio-position]', 'pointerdown', {button: 0}, 'PointerEvent');
        await chooseSpeed('1.5×'); window.__saved(); await __tick(20);
        window.__book = {...window.__book, position: 150}; await __tick(1000);
        return {pressed, later, afterSpeed: live()};
        """,
        before=audio_page(),
    )
    out = result.value
    assert out["pressed"]["slider"] == "30"
    assert out["later"]["slider"] == "90"
    assert out["afterSpeed"]["slider"] == "150"
    assert result.calls_of("audiobook_set_speed") == [[4, 1.5]]


def test_audiobooks_page_opened_during_playback_shows_the_playing_book(page) -> None:
    # Playback started elsewhere (the sidebar, the light novel reader, or it kept playing
    # while the window restarted); then the user opens the Audiobooks page.
    result = page.run(
        AUDIO_HELPERS
        + r"""
        setPage('audiobooks', true); await __tick(3000);
        return {card: Boolean(card()), summary: summary()};
        """,
        before=audio_page(playing=True, player_running=True),
    )
    assert result.value["card"] is True
