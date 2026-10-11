"""The Screen text shortcut makes the focused window the VN: pressing ⌘⌥⇧V with no
VN capture running starts the regular Visual novels capture on the frontmost window
(the VN page shows it, titled after the window) and the panel shows its lines; the
second press closes the panel and stops that capture. Without the Screen Recording
permission the window picker is used instead, and a line (once in the panel, and in
Settings) offers to allow it; the macOS request is made at most once, on a click."""

from __future__ import annotations

import sys
import threading
import types

from tests.i18n_support import en, ru
from tests.test_fix_screen_text_permission import VN, Api, Source, make_panel
from tests.test_patch_e_ui import make_api


class MainApi(Api):
    def __init__(self, vn: VN) -> None:
        super().__init__(vn)
        self.changed = 0

    def visual_novel_changed(self) -> None:
        self.changed += 1


def test_shortcut_starts_the_vn_capture_on_the_focused_window_and_stops_it() -> None:
    vn = VN()
    api, source = MainApi(vn), Source(allowed=True)
    panel, windows = make_panel(api, source)
    panel.toggle()
    # The regular VN start (window id, title = the window's title), no picker.
    assert api.starts == [(117, "jpdb", "", False)]
    assert api.changed == 1  # the Visual novels page is told to show it
    vn.line("それが、二階堂ヒロだった。", 1)
    state = panel.state()
    assert (state["text"], state["permission_hint"]) == ("それが、二階堂ヒロだった。", False)
    panel.toggle()
    assert windows[0].destroyed and api.vn_stops == 1 and api.changed == 2


def test_without_permission_the_picker_is_used_and_the_hint_shows_once() -> None:
    vn = VN()
    api, source = MainApi(vn), Source(allowed=False)
    panel, _windows = make_panel(api, source)
    api.picker_choice.set()
    panel.toggle()
    assert api.starts == [(0, "", "", True)]
    assert panel.state()["permission_hint"] is True
    panel.toggle()
    panel.toggle()
    assert panel.state()["permission_hint"] is False  # once per run
    panel.toggle()


def test_allow_button_requests_screen_recording_once_and_opens_the_settings(tmp_path, monkeypatch) -> None:
    api = make_api(tmp_path)
    try:
        requests: list[int] = []
        opened: list[int] = []
        quartz = types.ModuleType("Quartz")
        quartz.CGRequestScreenCaptureAccess = lambda: requests.append(1) or False
        monkeypatch.setitem(sys.modules, "Quartz", quartz)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(api, "open_screen_recording_settings", lambda: opened.append(1) or {"ok": True})
        allowed = {"value": False}
        monkeypatch.setattr(api.visual_novels, "_screen_recording_access", lambda: allowed["value"])
        assert api.screen_recording_status() == {"allowed": False}
        assert api._settings_payload()["screen_recording_allowed"] is False
        first = api.screen_recording_request_access()
        second = api.screen_recording_request_access()
        assert (first["requested"], second["requested"]) == (True, False)
        assert requests == [1] and opened == [1, 1]
        # The marker survives a restart: a new window does not ask again.
        again = make_api(tmp_path)
        try:
            monkeypatch.setattr(again.visual_novels, "_screen_recording_access", lambda: False)
            monkeypatch.setattr(again, "open_screen_recording_settings", lambda: {"ok": True})
            assert again.screen_recording_request_access()["requested"] is False
        finally:
            again.close()
        allowed["value"] = True
        assert api.screen_recording_request_access()["requested"] is False and requests == [1]
    finally:
        api.close()


def test_main_window_is_told_to_refresh_the_visual_novels_page(tmp_path) -> None:
    api = make_api(tmp_path)
    try:
        scripts: list[str] = []
        api.window = types.SimpleNamespace(run_js=scripts.append)
        api.visual_novel_changed()
        assert scripts == ["window.PudgeVisualNovels?.refresh?.()"]
    finally:
        api.window = None
        api.close()


def test_visual_novels_page_shows_a_capture_the_shortcut_started(page) -> None:
    result = page.run(
        r"""
        setPage('visualnovels'); await __tick(50);
        const out = {before: __visible('#vnStop')};
        window.vnState = {running: true, status: 'reading', window_id: 117, window_title: 'jpdb', transcript: [],
          current_text: '', capture_count: 1, ocr_count: 1};
        await PudgeVisualNovels.refresh(); await __tick(10);
        out.after = {stop: __visible('#vnStop'), start: __visible('#vnStart'), title: document.getElementById('vnGameTitle').value};
        return out;
        """,
        before="window.vnState = {running: false, status: 'idle', transcript: []};"
        "__api.visual_novel_state = async () => ({...window.vnState});",
    )
    assert result.value == {"before": False, "after": {"stop": True, "start": False, "title": "jpdb"}}


def test_panel_and_settings_offer_to_allow_screen_recording(page) -> None:
    for lang, text in (("en", en), ("ru", ru)):
        panel = page.run(
            r"""
            await __tick(50);
            const out = {hint: __visible('#screenTextHint'), text: __text('#screenTextHint')};
            await __click('#screenTextAllow'); await __tick(10);
            window.screenState = {...window.screenState, permission_hint: false};
            await __tick(800); out.later = __visible('#screenTextHint');
            return out;
            """,
            name="screen_text.html",
            api={"screen_text_settings": {"language": lang}},
            before="window.screenState = {running: false, status: 'choosing', text: '', text_id: 0, permission_hint: true};"
            "__api.screen_text_state = async () => ({...window.screenState});",
        )
        out = panel.value
        assert out["hint"] is True and text("screenText.allowHint") in out["text"] and out["later"] is False
        assert panel.called("screen_recording_request_access")
    for allowed, shown in ((False, True), (True, False), (None, False)):
        settings = page.run(
            r"""
            setPage('settings', true); await __tick(100);
            await __click('.settings-categories [data-settings-category="advanced"]'); await __tick(10);
            const out = {shortcuts: __visible('#s_shortcut_screen_text'), shown: __visible('#s_screen_recording_hint'), text: __text('#s_screen_recording_hint')};
            if (out.shown) { await __click('#screenRecordingAllow'); await __tick(10); }
            return out;
            """,
            state={"settings": {"screen_recording_allowed": allowed}},
        )
        assert settings.value["shortcuts"] is True and settings.value["shown"] is shown
        if shown:
            assert en("screenText.allowHint") in settings.value["text"]
            assert settings.called("screen_recording_request_access")


def test_second_press_during_the_picker_still_works_with_the_hint() -> None:
    vn = VN()
    api, source = MainApi(vn), Source(allowed=False)
    panel, windows = make_panel(api, source)
    opener = threading.Thread(target=panel.toggle)
    opener.start()
    assert api.picker_open.wait(2.0)
    assert not panel.opened
    panel.toggle()
    opener.join(2.0)
    assert windows == [] and api.changed == 0
    # The hint was never seen, so the next panel still shows it.
    api.picker_choice.set()
    panel.toggle()
    assert panel.opened and panel.state()["permission_hint"] is True
    panel.toggle()
