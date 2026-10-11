"""v0.7.30 follow-up fixes: word card layout and grades, custom select menus, listen-together
scrolling and speed, Jiten state refresh after a review, music videos, sidebar live text and
covers (behaviour; audit 71Б rewrite: page harness and parsed CSS instead of source text)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from css_support import css_rules
from pudge.web_app import WebAppApi
from study_page_support import LN_BOOK
from test_web_app import make_api
from tests.test_v072_audio_status_credentials_playback import LN, READER

ROOT = Path(__file__).resolve().parents[1]
STUDY = {"before": LN_BOOK, "state": {"settings": {"review_gate_ln_enabled": False}}}


# --- word card -------------------------------------------------------------------------------


def test_study_header_row_is_vertically_centred() -> None:
    rules = css_rules("reading_tools.css")
    head = rules.declarations(".pudge-study-head")
    assert head["grid-template-areas"] == '"term actions close"' and head["align-items"] == "center"
    close = rules.declarations(".pudge-study-close")
    assert close["grid-area"] == "close" and close["align-self"] == "center"


def test_grade_buttons_have_colour_only_hover_and_grey_unavailable_state() -> None:
    rules = css_rules("reading_tools.css")
    for tone in ("again", "hard", "good", "easy"):
        hover = rules.declarations(f".pudge-study-grade.grade-{tone}:hover")
        assert hover.get("background"), tone
        assert "transform" not in hover and "filter" not in hover, (
            tone
        )  # nothing moves or blurs under the pointer
    assert rules.get(".pudge-study-grade.is-unavailable", "cursor") == "not-allowed"
    assert rules.get(".pudge-study-grade.is-unavailable:hover", "cursor") == "not-allowed"


def test_unavailable_grades_keep_hover_tooltip(page) -> None:
    # aria-disabled keeps WebKit's native title tooltip; a [disabled] button would drop it.
    result = page.run(
        r"""
        window.LN_VOCAB[0].reviewable = false;   // 猫: no safe id mapping for the SRS
        await __settle(openLightNovel(6)); await __tick(300);
        await __click('#lnReader [data-ln-token="0:0"]'); await __tick(50);
        const grades = () => [...document.querySelectorAll('#pudgeStudyCard [data-pudge-study-review]')];
        const out = {grades: grades().map(b => ({aria: b.getAttribute('aria-disabled'), disabled: b.hasAttribute('disabled'), title: b.title,
                                                 unavailable: b.classList.contains('is-unavailable')})),
                     reason: t('reading.noSafeNativeIdMapping')};
        await __click('#pudgeStudyCard [data-pudge-study-review="good"]'); await __tick(50);
        out.open = document.getElementById('pudgeStudyCard').classList.contains('open');
        return out;
        """,
        **STUDY,
    )
    out = result.value
    assert len(out["grades"]) == 4
    assert all(
        grade == {"aria": "true", "disabled": False, "title": out["reason"], "unavailable": True}
        for grade in out["grades"]
    )
    assert not result.called("study_action") and not result.called("light_novel_study_action")
    assert out["open"] is True


# --- custom select menus ---------------------------------------------------------------------

SELECT = r"""
document.body.insertAdjacentHTML('beforeend', '<select id="testSpeed"><option value="1">1×</option><option value="1.5">1.5×</option></select>'
  + '<select id="testHidden" hidden><option>a</option></select>');
await __flush();
const shell = id => document.getElementById(id).closest('.pudge-select');
const button = id => shell(id).querySelector('.pudge-select-button');
const menuOpen = () => Boolean(document.querySelector('.pudge-select-menu.open'));
const options = () => [...document.querySelectorAll('.pudge-select-menu.open [data-pudge-select-index]')];
const press = async id => { await __fire(`#${id} + .pudge-select-button`, 'pointerdown', {button: 0}, 'PointerEvent'); };
"""


def test_select_toggles_on_press_because_webkit_can_drop_the_click(page) -> None:
    result = page.run(
        SELECT
        + r"""
        const out = {};
        await press('testSpeed'); out.pressed = menuOpen();            // the press alone opens (WebKit may drop the click)
        button('testSpeed').click(); await __flush(); out.clickAfterPress = menuOpen();   // its own click does not close it again
        await press('testSpeed'); out.pressedAgain = menuOpen();
        await __tick(1000);
        button('testSpeed').click(); await __flush(); out.keyboardClick = menuOpen();     // a click without a press still toggles
        return out;
        """,
    )
    assert result.value == {
        "pressed": True,
        "clickAfterPress": True,
        "pressedAgain": False,
        "keyboardClick": True,
    }


def test_open_select_menu_is_not_rebuilt_under_the_pointer(page) -> None:
    result = page.run(
        SELECT
        + r"""
        await press('testSpeed');
        const hovered = options()[1];
        const select = document.getElementById('testSpeed');
        select.value = '1'; PudgeSelect.sync(select); await __flush();       // a poll re-sets the same value
        const out = {same: options()[1] === hovered, connected: hovered.isConnected};
        hovered.click(); await __flush();                                      // the user's click still lands
        out.value = select.value; out.closed = !menuOpen();
        await press('testSpeed');
        const before = options()[0];
        select.insertAdjacentHTML('beforeend', '<option value="2">2×</option>'); PudgeSelect.sync(select); await __flush();
        out.rebuilt = options()[0] !== before && options().length === 3;
        return out;
        """,
    )
    assert result.value == {"same": True, "connected": True, "value": "1.5", "closed": True, "rebuilt": True}


def test_select_menu_open_close_reasons_are_exported(page) -> None:
    result = page.run(
        SELECT
        + r"""
        await __settle(openLightNovel(6)); await __tick(300);
        await press('testSpeed'); await press('testSpeed');
        await __key('Enter', {}, '#testHidden + .pudge-select-button');       // a hidden select refuses to open
        await __settle(exportActiveDebugContext());
        return PudgeSelect.trace().map(row => [row.action, row.id, row.reason ?? null]);
        """,
        before=READER.replace("ALIGNMENT", json.dumps({"status": "ready", "ready": True})),
        api={**LN["api"], "light_novel_export_audio_sync_trace": {"ok": True, "path": "/tmp/trace.json"}},
        state=LN["state"],
    )
    rows = result.value
    assert ["open", "testSpeed", None] in rows and ["close", "testSpeed", "toggle-press"] in rows
    assert ["open-refused", "testHidden", None] in rows
    # The LN sync trace (the debug export of an open reader) carries the same rows.
    [[payload]] = result.calls_of("light_novel_export_audio_sync_trace")
    exported = [[row["action"], row["id"], row.get("reason")] for row in payload["select_debug"]]
    assert exported == rows


# --- listen together -------------------------------------------------------------------------

# The open reader of light novel 6 (narrated by audiobook 75, alignment ready): three words of
# 10 audio seconds each, far below the visible part of the reader.
PAIRED_READER = r"""
await __settle(openLightNovel(6)); await __tick(300);
const reader = document.getElementById('lnReader');
reader.innerHTML = '<p data-ln-paragraph="0" data-ln-audio-start="0" data-ln-audio-end="30">'
  + ['一', '二', '三'].map((w, i) => `<span class="ln-word" data-ln-token="${i}" data-ln-audio-start="${i * 10}" data-ln-audio-end="${i * 10 + 10}">${w}</span>`).join('')
  + '</p>';
ui.lnChapter = {chapter_index: 0};
const at = (position, extra = {}) => ({alignment_mode: 'stt', playing: true, ln_chapter_index: 0, position, ...extra});
const current = () => [...reader.querySelectorAll('.ln-word')].findIndex(w => w.classList.contains('ln-paired-word-current'));
const scroller = document.getElementById('lnReaderScroll');
scroller.__rect = {top: 0, left: 0, width: 800, height: 600};
reader.querySelectorAll('.ln-word').forEach((w, i) => { w.__rect = {top: 2000 + i * 40, left: 0, width: 20, height: 30}; });
const followed = [];
Object.getPrototypeOf(reader).scrollIntoView = function (options) { followed.push([this.dataset.lnToken, options?.behavior]); };
"""
PAIRED = {"before": READER.replace("ALIGNMENT", json.dumps({"status": "ready", "ready": True})), **LN}


def test_reader_auto_follow_scroll_does_not_defer_paired_highlight(page) -> None:
    for user_input in ("wheel", "pointerdown"):
        result = page.run(
            PAIRED_READER
            + r"""
            renderLnPairedPosition(at(1.0), 5, {speechActive: true});          // the spoken word is off screen: follow it
            const out = {first: current(), followed: [...followed]};
            scroller.dispatchEvent(new Event('scroll'));                        // the reader's own smooth scroll
            renderLnPairedPosition(at(1.4), 15, {speechActive: true});
            out.duringFollow = current();                                       // the highlight moves at once
            await __tick(1200);
            renderLnPairedPosition(at(2.2), 5, {speechActive: true, seekJump: true});   // follow again ...
            scroller.dispatchEvent(new Event(INPUT));                           // ... but the user takes over
            scroller.dispatchEvent(new Event('scroll'));
            renderLnPairedPosition(at(2.6), 25, {speechActive: true});
            out.duringUserScroll = current();                                   // never repaint under a manual scroll
            window.dispatchEvent(new Event('pointerup'));
            await __tick(400);
            out.settled = current();
            return out;
            """.replace("INPUT", json.dumps(user_input)),
            **PAIRED,
        )
        out = result.value
        assert out["first"] == 0 and out["followed"] == [["0", "smooth"]], user_input
        assert out["duringFollow"] == 1, user_input
        assert out["duringUserScroll"] == 0 and out["settled"] == 2, user_input


def test_ln_speed_choice_is_not_reverted_by_stale_poll_and_pause_keeps_highlight(page) -> None:
    result = page.run(
        PAIRED_READER
        + r"""
        renderLnPairedPosition(at(1.0), 5, {speechActive: true});
        // Paused narration; the backend still reports the old speed for a while.
        window.backend = {linked: true, audiobook_id: 75, ln_book_id: 6, playing: false, player_running: true, paused: true,
                          position: 22, duration: 3600, speed: 1, alignment_mode: 'stt', ln_chapter_index: 0,
                          chapter_char_offset_exact: 25, alignment: {status: 'ready', ready: true}};
        __api.light_novel_paired_state = async () => ({...window.backend});
        __api.audiobook_set_speed = async (_id, speed) => ({ok: true});
        const speed = () => document.getElementById('lnPairedSpeed').value;
        const poll = async () => { syncLnPairedTray({...window.backend}); await __flush(); return speed(); };
        await __input('#lnPairedSpeed', '1.5'); await __tick(50);
        const out = {chosen: speed(), word: current()};
        out.stalePoll = await poll();                            // a poll from before the change
        await __tick(5000); out.unconfirmed = await poll();      // never confirmed: after a while the backend wins
        await __input('#lnPairedSpeed', '2'); await __tick(50);
        window.backend.speed = 2; out.confirmed = await poll();
        window.backend.speed = 1; out.changedElsewhere = await poll();   // once confirmed, later changes show at once
        return out;
        """,
        **PAIRED,
    )
    out = result.value
    assert result.calls_of("audiobook_set_speed") == [[75, 1.5], [75, 2]]
    assert out["chosen"] == out["stalePoll"] == "1.5"
    assert out["word"] == 0  # paused: the speed change does not move the highlight to the reported offset
    assert (out["unconfirmed"], out["confirmed"], out["changedElsewhere"]) == ("1", "2", "1")


# --- Jiten state after a review ------------------------------------------------------------

# 好き (103) is learning; Jiten reports the review's result only after `window.lagging` more
# forced lookups (window.lagging = 99: never).
JITEN = (
    LN_BOOK
    + r"""
window.lagging = 2;
__api.study_provider_capabilities = async () => ({account_key: 'acc'});
__api.study_states = async payload => {
  const after = payload.force && window.reviewed && window.lagging-- <= 0;
  if (after) window.updated = true;
  const known = window.updated;
  return {ok: true, account_scope: 'acc', states: payload.words.map(([wordId, readingIndex]) => ({ok: true, wordId, readingIndex,
    states: wordId === 103 ? [known ? 'mature' : 'young'] : ['new'], normalizedState: wordId === 103 ? (known ? 'mature' : 'young') : 'new'}))};
};
__api.study_action = async () => { window.reviewed = true; return {ok: true}; };
"""
)


def test_review_refresh_retries_until_jiten_state_changes_and_recolours_ln(page) -> None:
    run = r"""
    await __settle(openLightNovel(6)); await __tick(300);
    const word = token => document.querySelector(`#lnReader [data-ln-token="${token}"]`).className.split(' ').filter(c => c.startsWith('state-'));
    const out = {before: word('0:2')};
    await __click('#lnReader [data-ln-token="0:2"]'); await __tick(50);
    const reviewedAt = Date.now();
    await __click('#pudgeStudyCard [data-pudge-study-review="good"]');
    for (let i = 0; i < 150; i++) await __tick(100);
    out.after = [word('0:2'), word('1:1')];
    out.lookups = __apiCalls.filter(c => c.name === 'study_states' && c.args[0].force).map(c => c.at);
    return out;
    """
    result = page.run(run, **{**STUDY, "before": JITEN})
    out = result.value
    assert out["before"] == ["state-young"]
    # Both occurrences of the word change colour once Jiten reports the new state ...
    assert out["after"] == [["state-mature"], ["state-mature"]]
    # ... found by retrying (Jiten updates its SRS a little later), then no more lookups.
    gaps = [b - a for a, b in zip(out["lookups"], out["lookups"][1:])]
    assert gaps == [1200, 3000]
    never = page.run(run, **{**STUDY, "before": JITEN.replace("window.lagging = 2", "window.lagging = 99")})
    gaps = [b - a for a, b in zip(never.value["lookups"], never.value["lookups"][1:])]
    assert gaps == [1200, 3000, 7000] and never.value["after"] == [["state-young"], ["state-young"]]


# --- music videos ----------------------------------------------------------------------------


def test_music_videos_are_never_announced_by_the_backend(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    api.config.anilist.enabled = True
    api.config.anilist.access_token = "token"
    now = int(__import__("time").time())
    rows = [
        {"id": 1, "media_id": 11, "media_type": "ANIME", "format": "MUSIC", "created_at": now},
        {"id": 2, "media_id": 12, "media_type": "ANIME", "format": "TV", "created_at": now},
        {"id": 3, "media_id": 13, "media_type": "ANIME", "format": "music", "created_at": now},
    ]
    client = SimpleNamespace(related_media_addition_notifications=lambda per_page: rows, close=lambda: None)
    monkeypatch.setattr(api, "_anilist_client", lambda: client)
    assert [row["id"] for row in api.poll_anilist_related_media_notifications()["notifications"]] == [2]


def test_music_videos_are_never_announced_in_the_window(page) -> None:
    music = {"format": "MUSIC", "title": "Opening MV"}
    show = {"format": "TV", "title": "Season 2"}
    related = page.run(
        "await __tick(2000); return {open: __visible('#anilistRelatedBackdrop'), text: __text('#anilistRelatedList')};",
        api={
            "poll_anilist_related_media_notifications": {
                "notifications": [{**music, "id": 1, "media_id": 11}, {**show, "id": 2, "media_id": 12}]
            },
            "mark_anilist_related_media_notifications_seen": {"ok": True},
        },
    )
    assert (
        related.value["open"] is True
        and "Season 2" in related.value["text"]
        and "Opening MV" not in related.value["text"]
    )
    assert related.calls_of("mark_anilist_related_media_notifications_seen") == [[[2]]]
    releases = page.run(
        "await __tick(2000); return {open: __visible('#plannedReleaseBackdrop'), text: __text('#plannedReleaseList')};",
        api={
            "poll_planned_release_discovery": {
                "offers": [{**music, "media_id": 11}, {**show, "media_id": 12}]
            }
        },
    )
    assert (
        releases.value["open"] is True
        and "Season 2" in releases.value["text"]
        and "Opening MV" not in releases.value["text"]
    )
    only_music = page.run(
        "await __tick(2000); return [__visible('#anilistRelatedBackdrop'), __visible('#plannedReleaseBackdrop')];",
        api={
            "poll_planned_release_discovery": {"offers": [{**music, "media_id": 11}]},
            "poll_anilist_related_media_notifications": {
                "notifications": [{**music, "id": 1, "media_id": 11}]
            },
        },
    )
    assert only_music.value == [False, False]


# --- sidebar ---------------------------------------------------------------------------------

# The sidebar mini-player of audiobook 5 narrating light novel 11 (window.audioBooks between polls).
SIDEBAR_LIVE = r"""
window.audioBooks = [{id: 5, title: 'Book', playing: true, player_running: true, position: 30, duration: 600, speed: 1,
                      cover_url: 'covers/5.jpg', chapters: [{start: 0, end: 600, title: 'One'}], linked_light_novel: {id: 11, title: 'Novel'}}];
window.floatOpen = false;
__api.sidebar_audio_state = async () => ({books: window.audioBooks, float_open: window.floatOpen});
__api.light_novel_paired_state = async () => ({ln_chapter_index: 0, chapter_char_offset_exact: 1.5, playing: true, position: 30});
__api.light_novel_chapter = async () => ({paragraphs: ['猫が好きです。'], tokens: [[{start: 0, end: 1}, {start: 1, end: 2}, {start: 2, end: 4}]], vocabulary: []});
"""


def test_sidebar_live_text_survives_hidden_window(page) -> None:
    result = page.run(
        r"""
        await __tick(1500);
        const live = () => document.querySelector('[data-sc-live-text]')?.innerText ?? null;
        const out = {shown: live()};
        await __setHidden(true); await __tick(3000);
        out.hidden = live();                                   // alt-tab: frames pause, the text stays
        await __setHidden(false); await __tick(100);
        out.back = live();
        applyPowerStatus({mode: 'energy_saving', reasons: ['manual']}); await __tick(10);
        out.energySaving = live();                             // energy saving: the live text goes
        return out;
        """,
        before=SIDEBAR_LIVE,
    )
    out = result.value
    assert out["shown"] == out["hidden"] == out["back"] == "猫が好きです。"
    assert not out["energySaving"]
    floating = page.run(
        r"""
        await __tick(1500);
        const before = document.querySelector('[data-sc-live-text]')?.innerText;
        window.floatOpen = true; await __tick(2100);           // the floating player now shows the live text
        await __setHidden(true); await __tick(10);
        return [before, document.querySelector('[data-sc-live-text]')?.innerText ?? ''];
        """,
        before=SIDEBAR_LIVE,
    )
    assert floating.value == ["猫が好きです。", ""]


# Gestures on a cover image (CoverPreview): trackpad pinch.
PINCH = r"""
const preview = () => document.querySelector('.pudge-cover-preview.open img')?.getAttribute('src') ?? null;
const gesture = (img, type, scale) => { const event = new Event(type, {bubbles: true, cancelable: true}); event.scale = scale; img.dispatchEvent(event); };
const pinch = async img => { img.__rect = {x: 10, y: 10, width: 76, height: 108};
  gesture(img, 'gesturestart', 1); gesture(img, 'gesturechange', 2); gesture(img, 'gestureend', 2); await __tick(300); };
const closePreview = async () => { if (document.querySelector('.pudge-cover-preview-close')) await __click('.pudge-cover-preview-close'); await __tick(300); };
"""


def test_sidebar_cover_click_opens_audiobook_and_zoom_uses_cover_preview_gestures(page) -> None:
    result = page.run(
        PINCH
        + r"""
        await __tick(1500);
        const cover = () => document.querySelector('.sidebar-audio-cover img');
        await pinch(cover());
        const out = {pinched: preview()};
        await closePreview();
        await __click('.sidebar-audio-cover img'); await __tick(300);
        out.afterClick = {page: ui.page, preview: preview()};
        return out;
        """,
        before=SIDEBAR_LIVE,
        api={"audiobook_state": {"books": []}},
    )
    out = result.value
    assert out["pinched"] and out["pinched"].endswith("covers/5.jpg")  # zoom like any other cover
    assert out["afterClick"] == {"page": "audiobooks", "preview": None}  # a click shows the audiobook
    card = {
        "wordId": 1,
        "readingIndex": 0,
        "wordTextPlain": "選ぶ",
        "readings": [{"readingIndex": 0, "text": "選ぶ", "rubyText": "選[えら]ぶ"}],
        "definitions": [{"meanings": ["choose"]}],
        "pudgeContextImage": "covers/scene.jpg",
    }
    review = page.run(
        PINCH
        + r"""
        await __tick(1100);
        await __click('[data-sc-reveal]'); await __tick(10);
        await __click('.sidebar-due-image img'); await __tick(300);
        return {preview: preview(), page: ui.page};
        """,
        before=r"""
        __api.sidebar_due_review_cards = async () => ({cards: [CARD]});
        __api.sidebar_review_settings = async value => ({interval: value || 'continuous'});
        """.replace("CARD", json.dumps(card, ensure_ascii=False)),
    )
    assert (
        review.value["preview"].endswith("covers/scene.jpg") and review.value["page"] == "current"
    )  # a review image zooms on click


# --- removed things --------------------------------------------------------------------------


def test_mangaocr_install_log_button_is_fully_removed(page) -> None:
    assert not hasattr(WebAppApi, "reveal_manga_ocr_install_log")
    result = page.run(
        r"""
        setPage('settings', true); await __tick(300);
        await __settle(PudgeMedia.refreshMangaOcrStatus()); await __tick(50);
        const block = document.getElementById('mangaOcrStatus').closest('.setting-block');
        return {buttons: [...block.querySelectorAll('button')].filter(b => b.checkVisibility()).map(b => b.id || b.innerText),
                logLabel: t('ui.installLog'), status: __text('#mangaOcrStatus')};
        """,
        api={
            "manga_ocr_status": {
                "state": "failed",
                "backend": "mokuro",
                "running": False,
                "detail": "pip failed",
                "log_path": "/tmp/mokuro.log",
            }
        },
    )
    out = result.value
    assert out["status"]
    assert "installMangaOcr" in out["buttons"]
    assert "openMangaOcrLog" not in out["buttons"] and out["logLabel"] not in out["buttons"]


def test_repository_root_has_no_leftover_tool_files() -> None:
    assert not (ROOT / "....").exists()
