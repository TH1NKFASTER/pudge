from __future__ import annotations

from pudge.manager_models import LibraryAnime
from pudge.planned_release_discovery import PlannedReleaseDiscovery
from test_web_app import make_api
from tests.css_support import css_rules


def test_later_survives_restart_and_late_worker_without_changing_planning(tmp_path, monkeypatch):
    api = make_api(tmp_path)
    monkeypatch.setattr(
        api, "_anilist_client", lambda: (_ for _ in ()).throw(AssertionError("Later must remain local"))
    )
    for media_id in (3, 4):
        api.manager.db.upsert_anime(LibraryAnime(media_id, f"Synthetic {media_id}", status="PLANNING"))
        api.manager.planned_release_discovery()._save(media_id, {"phase": "found", "found_at": 100})
    assert api.dismiss_planned_release_offers([3, 3, None, "bad", -1, 999]) == {"ok": True, "dismissed": 1}
    assert api.manager.db.get_anime(3).status == "PLANNING"
    # A worker racing the acknowledgement may write the older found result.
    api.manager.planned_release_discovery()._save(3, {"phase": "found", "found_at": 101})
    restarted = make_api(tmp_path)
    assert [row["media_id"] for row in restarted.poll_planned_release_discovery()["offers"]] == [4]
    assert restarted.dismiss_planned_release_offers([3]) == {"ok": True, "dismissed": 0}
    assert [row["media_id"] for row in PlannedReleaseDiscovery(restarted.manager).offers()] == [4]


def test_later_does_not_dismiss_unshown_or_unreleased_entries(tmp_path):
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(LibraryAnime(5, "Synthetic future", status="PLANNING"))
    discovery = api.manager.planned_release_discovery()
    discovery._save(5, {"phase": "armed", "airing_at": 2_100_000_000})
    assert api.dismiss_planned_release_offers([5]) == {"ok": True, "dismissed": 0}
    assert discovery._state(5)["phase"] == "armed"


def test_later_ui_waits_for_acknowledgement_and_retries_errors(page):
    # "Later" closes the new-releases popup only once the backend has saved it; a failed save
    # keeps the popup with an error and can be retried; acknowledged titles never come back.
    result = page.run(
        r"""
        const popup = () => ({open: $('plannedReleaseBackdrop').classList.contains('open') && __visible('#plannedReleaseBackdrop'),
                              titles: [...document.querySelectorAll('#plannedReleaseList strong')].map(n => n.innerText),
                              later: __text('#plannedReleaseClose'), disabled: $('plannedReleaseClose').disabled});
        const out = {shown: popup()};
        await __click('#plannedReleaseClose');
        out.saving = popup();
        await __click('#plannedReleaseBackdrop');                 // a click beside the popup while saving
        answers.shift()({ok: true}); await __flush();
        out.closed = popup();
        await __tick(60000);                                     // the next poll still lists 3
        out.next = popup();
        await __click('#plannedReleaseClose');
        answers.shift()({ok: false}); await __flush();
        out.failed = {...popup(), toast: __text('#toast'), error: t('error.generic', {error: PudgeI18n.t('ui.couldNotSaveLaterRetry')})};
        await __click('#plannedReleaseClose');
        answers.shift()({ok: true}); await __flush();
        out.retried = popup();
        out.later = PudgeI18n.t('ui.later');
        return out;
        """,
        before=r"""
        const offers = [[{media_id: 3, title: 'Synthetic release'}],
                        [{media_id: 3, title: 'Synthetic release'}, {media_id: 4, title: 'New synthetic release'}]];
        __api.poll_planned_release_discovery = async () => ({offers: offers.length > 1 ? offers.shift() : offers[0]});
        window.answers = [];
        __api.dismiss_planned_release_offers = () => new Promise(resolve => window.answers.push(resolve));
        """,
    )
    out = result.value
    later = out["later"]
    assert out["shown"] == {"open": True, "titles": ["Synthetic release"], "later": later, "disabled": False}
    assert out["saving"] == {**out["shown"], "disabled": True}
    assert out["closed"]["open"] is False
    assert out["next"] == {
        "open": True,
        "titles": ["New synthetic release"],
        "later": later,
        "disabled": False,
    }
    assert out["failed"]["open"] is True and out["failed"]["disabled"] is False
    assert out["failed"]["toast"] == out["failed"]["error"]
    assert out["retried"]["open"] is False
    assert result.calls_of("dismiss_planned_release_offers") == [[[3]], [[4]], [[4]]]


# Assistant panel geometry: the panel's box follows its inline position and size.
PANEL_LAYOUT = r"""
Object.defineProperty(Element.prototype, '__rect', {
  configurable: true,
  get() {
    if (this.id !== 'pudgeAssistant') return undefined;
    const px = value => parseFloat(value) || 0;
    return {x: px(this.style.left), y: px(this.style.top), width: px(this.style.width), height: px(this.style.height)};
  },
  set(value) { Object.defineProperty(this, '__rect', {value, writable: true, configurable: true}); },
});
"""
LLM = {
    "llm_enabled": True,
    "llm_model": "model",
    "llm_url": "http://127.0.0.1:11434",
    "llm_provider": "ollama",
}


def test_assistant_fits_saved_geometry_window_resize_drag_and_reopen(page):
    result = page.run(
        r"""
        PudgeUiStorage.set('pudge.assistant.geometry.v1', {x: 2300, y: 900, width: 700, height: 850});
        PudgeAssistant.show();
        const panel = $('pudgeAssistant');
        const rect = () => { const r = panel.getBoundingClientRect(); return {x: r.left, y: r.top, width: r.width, height: r.height, vw: innerWidth, vh: innerHeight}; };
        const pointer = (sel, type, init) => __fire('#pudgeAssistant ' + sel, type, init, 'PointerEvent');
        const out = {saved: rect(), windows: []};
        for (const [width, height] of [[1440, 900], [800, 420], [240, 180], [980, 600]]) {
          innerWidth = width; innerHeight = height; window.dispatchEvent(new Event('resize'));
          const step = {resized: rect()};
          await pointer('.pa-head', 'pointerdown', {button: 0, pointerId: 1, clientX: 12, clientY: 12});
          await pointer('.pa-head', 'pointermove', {pointerId: 1, clientX: -3000, clientY: -3000}); step.draggedUp = rect();
          await pointer('.pa-head', 'pointermove', {pointerId: 1, clientX: 3000, clientY: 3000}); step.draggedDown = rect();
          await pointer('.pa-head', 'pointerup', {pointerId: 1});
          await pointer('[data-pa-resize=se]', 'pointerdown', {button: 0, pointerId: 2, clientX: 0, clientY: 0});
          await pointer('[data-pa-resize=se]', 'pointermove', {pointerId: 2, clientX: 5000, clientY: 5000}); step.enlarged = rect();
          await pointer('[data-pa-resize=se]', 'pointerup', {pointerId: 2});
          out.windows.push(step);
        }
        // Collapsed while the window shrinks: reopening fits it again.
        PudgeAssistant.collapse(); innerWidth = 420; innerHeight = 300; window.dispatchEvent(new Event('resize'));
        PudgeAssistant.show();
        out.reopened = rect();
        return out;
        """,
        viewport={"width": 1024, "height": 720},
        state={"settings": LLM},
        before=PANEL_LAYOUT,
    )
    out = result.value

    def inside(rect: dict) -> bool:
        return (
            rect["width"] > 0
            and rect["height"] > 0
            and rect["x"] >= 0
            and rect["y"] >= 0
            and rect["x"] + rect["width"] <= rect["vw"]
            and rect["y"] + rect["height"] <= rect["vh"]
        )

    # A geometry saved on a bigger screen is pulled into the window.
    assert inside(out["saved"])
    for step in out["windows"]:
        assert all(inside(rect) for rect in step.values()), step
    assert out["windows"][0]["enlarged"]["width"] > 700  # the corner handle does resize
    assert inside(out["reopened"])
    # No CSS minimum size that would push the panel out of a small window.
    rules = css_rules("reading_assistant.css").declarations(".pudge-assistant")
    assert (rules.get("position"), rules.get("box-sizing")) == ("fixed", "border-box")
    assert (rules.get("min-width", "0"), rules.get("min-height", "0")) == ("0", "0")
