"""Frontend review 5 (window shell, dialogs, Downloads, Settings, sidebar): behaviour fixes.

- The Downloads window's poll, its late answers and torrent actions no longer replace another
  dialog that took the modal over (a final rating prompt), and they stop once it is gone.
- Escape closes the AniList notifications dialog (it passed it by and left full screen).
- Enter on the confirmation's Cancel button cancels (it confirmed the question).
- The relation preview shows a year / episode count read back from data-* as text.
- Custom select menus go with their selects (Settings drawn again kept every old copy).
- The sidebar's live text follows the chosen study service (it looked up `window.ui`, which the
  main window does not have: index.html's `ui` is a global const).
- The pairing status is not asked for every 2 s while the window is in the background.
- Popups that open by themselves wait while a question is open or a text field has the focus.
- A global search still waiting for its delay does not run after the overlay closed.
- "Check release/subtitle upgrades" work again after one check; Finish in the setup guide
  saves once on a double click.
"""

from __future__ import annotations

import json

import pytest
from test_future_sidebar_read_together import SIDEBAR_LIVE

DOWNLOADS = {
    "downloads": [
        {
            "hash": "abc",
            "name": "Show - 01",
            "anime_title": "Show",
            "state": "downloading",
            "progress": 0.4,
            "media_id": 7,
            "backend": "aria2",
        }
    ],
    "enabled": True,
    "queued": [],
    "storage": {},
}


def test_downloads_window_leaves_a_dialog_that_took_the_modal_alone(page) -> None:
    result = page.run(
        r"""
        await openDownloadCenter(null); await __tick(50);
        const out = {list: Boolean(document.querySelector('#modalBody [data-download-center]'))};
        const before = __apiCalls.filter(c => c.name === 'torrent_downloads').length;
        // An episode ended meanwhile: its final rating takes the dialog.
        showScoreModal({media_id: 7, title: 'Show', user_score: null}, true, 12);
        await __tick(10);
        await __tick(10000);
        out.title = __text('#modalTitle');
        out.wheel = Boolean(document.querySelector('#modalBody .score-wheel'));
        out.polled = __apiCalls.filter(c => c.name === 'torrent_downloads').length - before;
        // A push of torrent traffic does not redraw it either.
        applyTorrentTraffic({enabled: true, stale: false, updated_at: Date.now() / 1000, live_downloads: {}});
        out.afterPush = Boolean(document.querySelector('#modalBody .score-wheel'));
        return out;
        """,
        api={"torrent_downloads": DOWNLOADS, "final_sequel_option": {"sequel": None}},
    )
    out = result.value
    assert out["list"] is True
    assert out["title"] == page.run("return t('modal.rateFinal', {title: 'Show'})").value
    assert out["wheel"] is True and out["afterPush"] is True
    assert out["polled"] <= 1  # at most the poll that was already due; then it stops


def test_a_slow_downloads_answer_does_not_reopen_the_list_over_a_newer_dialog(page) -> None:
    result = page.run(
        r"""
        let answer; __api.torrent_downloads = () => new Promise(resolve => { answer = resolve; });
        const opening = openDownloadCenter(null); await __tick(10);
        showScoreModal({media_id: 7, title: 'Show', user_score: null}, false, null);
        await __tick(10);
        answer(window.DL); await __flush(); await __tick(10); await opening;
        return {wheel: Boolean(document.querySelector('#modalBody .score-wheel')),
                list: Boolean(document.querySelector('#modalBody [data-download-center]')),
                title: __text('#modalTitle') === t('modal.setScore')};
        """,
        before="window.DL = " + json.dumps(DOWNLOADS) + ";",
        api={"final_sequel_option": {"sequel": None}},
    )
    assert result.value == {"wheel": True, "list": False, "title": True}


def test_a_torrent_action_answered_after_close_does_not_bring_the_window_back(page) -> None:
    result = page.run(
        r"""
        await openDownloadCenter(null); await __tick(50);
        let answer; __api.torrent_download_action = () => new Promise(resolve => { answer = resolve; });
        document.querySelector('#modalBody [data-torrent-row]').click(); await __tick(10);
        const pause = document.querySelector('#modalBody [data-action="torrent-pause"]');
        const done = handleAction(pause); await __tick(10);
        closeModal(); await __tick(10);
        answer({...window.DL}); await __flush(); await done; await __tick(10);
        return {open: $('modalBackdrop').classList.contains('open'), center: ui.downloadCenter};
        """,
        before="window.DL = " + json.dumps(DOWNLOADS) + ";",
        api={"torrent_downloads": DOWNLOADS},
    )
    assert result.value == {"open": False, "center": None}


def test_escape_closes_the_anilist_notifications_dialog_not_full_screen(page) -> None:
    result = page.run(
        r"""
        renderAniListRelatedNotifications([{id: 1, media_id: 9, title: 'New show', created_at: Date.now() / 1000,
                                            site_url: 'https://anilist.co/anime/9'}]);
        await __tick(10);
        const out = {open: $('anilistRelatedBackdrop').classList.contains('open')};
        await __key('Escape'); await __tick(10);
        out.afterEscape = $('anilistRelatedBackdrop').classList.contains('open');
        return out;
        """
    )
    assert result.value == {"open": True, "afterEscape": False}
    assert not result.called("exit_fullscreen")


def test_enter_on_cancel_cancels_the_confirmation(page) -> None:
    result = page.run(
        r"""
        const answers = [];
        pudgeConfirm('Reset?').then(value => answers.push(value)); await __tick(50);
        await __key('Enter', {}, '#pudgeConfirmBackdrop [data-pudge-confirm=cancel]'); await __tick(10);
        // Enter on Cancel is the button's own press (a real click follows it in the browser).
        document.querySelector('#pudgeConfirmBackdrop [data-pudge-confirm=cancel]').click(); await __tick(10);
        pudgeConfirm('Again?').then(value => answers.push(value)); await __tick(50);
        await __key('Enter', {}, '#pudgeConfirmBackdrop [data-pudge-confirm=ok]'); await __tick(10);
        return answers;
        """
    )
    assert result.value == [False, True]


def test_relation_preview_shows_year_and_episodes_as_text(page) -> None:
    result = page.run(
        r"""
        const node = document.createElement('div');
        node.dataset.title = 'Show'; node.dataset.year = '<img src=x onerror=alert(1)>'; node.dataset.episodes = '<b>12</b>';
        document.body.appendChild(node);
        showRelationPreview(node); await __tick(20);
        const preview = $('relationPreview');
        return {img: preview.querySelectorAll('img').length, bold: preview.querySelectorAll('b').length,
                text: preview.textContent.includes('<b>12</b>')};
        """
    )
    assert result.value == {"img": 0, "bold": 0, "text": True}


def test_custom_select_menus_go_with_their_selects(page) -> None:
    result = page.run(
        r"""
        const menus = () => document.querySelectorAll('body > .pudge-select-menu').length;
        const openMenus = () => document.querySelectorAll('body > .pudge-select-menu.open').length;
        await __click('[data-page=settings]'); await __tick(50);
        const first = menus();
        for (let i = 0; i < 3; i++) { renderSettingsSafely(); await __tick(20); }
        const after = menus();
        // A select that is still on the page keeps a working menu; moved (taken out and put
        // back), its menu comes back with it.
        const shell = document.querySelector('#settingsContent select').closest('.pudge-select');
        const button = () => shell.querySelector('.pudge-select-button');
        button().click(); await __tick(10);
        const open = openMenus();
        PudgeSelect.closeIfOpen(); await __tick(10);
        const parent = shell.parentNode, next = shell.nextSibling;
        shell.remove(); await __tick(10);
        const whileOut = menus();
        parent.insertBefore(shell, next); await __tick(10);
        button().click(); await __tick(10);
        const reopened = openMenus();
        // Removed while open: the menu stays until it is closed, then it goes.
        shell.remove(); await __tick(10);
        const stillOpen = openMenus();
        PudgeSelect.closeIfOpen(); await __tick(10);
        return {first, after, open, whileOut, reopened, stillOpen, gone: menus()};
        """
    )
    out = result.value
    assert out["after"] == out["first"]
    assert out["open"] == 1 and out["reopened"] == 1 and out["stillOpen"] == 1
    assert out["whileOut"] == out["first"] - 1 and out["gone"] == out["first"] - 1


@pytest.mark.parametrize("backend", ["jiten", "jpdb"])
def test_sidebar_live_text_uses_the_chosen_study_service(page, backend) -> None:
    result = page.run(
        r"""
        await __tick(1500);
        await __fire('[data-sc-live-text] [data-pudge-study-token]', 'pointerover', {}, 'PointerEvent'); await __tick(1500);
        return {states: __apiCalls.filter(c => c.name === 'study_states').map(c => c.args[0].backend),
                decks: __apiCalls.filter(c => c.name === 'study_deck_preference').map(c => c.args[0]),
                jitenLink: Boolean(document.querySelector('#pudgeStudyCard [data-pudge-study-jiten]'))};
        """,
        before=SIDEBAR_LIVE,
        api={"light_novel_state": {"version": 1, "settings": {"study_backend": backend}, "books": []}},
    )
    out = result.value
    # The word card comes from the chosen service; Jiten word states and links only for Jiten.
    assert out["decks"] and set(out["decks"]) == {backend}
    assert set(out["states"]) <= {backend} and (out["states"] or backend == "jpdb")
    assert out["jitenLink"] is (backend == "jiten")


def test_pairing_status_is_not_polled_while_the_window_is_hidden(page) -> None:
    result = page.run(
        r"""
        await __click('[data-page=settings]'); await __tick(50);
        document.querySelector('[data-settings-category=advanced]')?.click(); await __tick(50);
        const count = () => __apiCalls.filter(c => c.name === 'iphone_status').length;
        await __tick(4000);
        const shown = count();
        await __setHidden(true);
        const hiddenStart = count();
        await __tick(20000);
        const hidden = count() - hiddenStart;
        await __setHidden(false); await __tick(2500);
        return {shown, hidden, back: count() - hiddenStart - hidden};
        """,
        api={"iphone_status": {"enabled": False, "pending": [], "devices": []}},
    )
    out = result.value
    assert out["shown"] >= 2
    assert out["hidden"] == 0
    assert out["back"] >= 1


def test_popups_wait_while_a_question_is_open_or_a_field_has_the_focus(page) -> None:
    result = page.run(
        r"""
        const item = id => ({id, media_id: 100 + id, title: 'New ' + id, created_at: Date.now() / 1000});
        const open = () => $('anilistRelatedBackdrop').classList.contains('open');
        void pudgeConfirm('Sure?'); await __tick(50);
        renderAniListRelatedNotifications([item(1)]); await __tick(10);
        const duringQuestion = open();
        PudgeConfirm.closeIfOpen(); await __tick(10);
        $('plannedSearch').focus();
        renderAniListRelatedNotifications([item(1)]); await __tick(10);
        const whileTyping = open();
        $('plannedSearch').blur();
        renderAniListRelatedNotifications([item(1)]); await __tick(10);
        return {duringQuestion, whileTyping, later: open()};
        """
    )
    assert result.value == {"duringQuestion": False, "whileTyping": False, "later": True}


def test_closing_the_search_cancels_a_search_still_waiting_for_its_delay(page) -> None:
    result = page.run(
        r"""
        openGlobalSearch(); await __tick(20);
        await __input('#globalSearchInput', 'abc', {change: false});
        await __tick(50);
        closeGlobalSearch();
        await __tick(500);
        return __apiCalls.filter(c => c.name === 'global_media_search').length;
        """,
        api={"global_media_search": []},
    )
    assert result.value == 0


@pytest.mark.parametrize(
    ("button", "method"),
    [
        ("checkReleaseUpgrades", "check_release_upgrades"),
        ("checkSubtitleUpgrades", "check_subtitle_upgrades"),
    ],
)
def test_upgrade_check_buttons_work_again_after_a_check(page, button, method) -> None:
    result = page.run(
        r"""
        await __click('[data-page=settings]'); await __tick(50);
        const id = window.BUTTON;
        $(id).click(); await __tick(50);
        const afterOk = $(id)?.disabled;
        __api[window.METHOD] = async () => { throw new Error('offline'); };
        $(id).click(); await __tick(50);
        return {afterOk, afterError: $(id)?.disabled};
        """,
        before=f"window.BUTTON = '{button}'; window.METHOD = '{method}'; __api.{method} = async () => ({{scheduled: 0}});",
        allow_errors=True,
    )
    assert result.value == {"afterOk": False, "afterError": False}


def test_a_double_click_on_finish_saves_the_setup_guide_once(page) -> None:
    result = page.run(
        r"""
        let answer; __api.complete_onboarding = () => new Promise(resolve => { answer = resolve; });
        showOnboarding(true, 5); await __tick(20);
        const first = finishOnboarding(), second = finishOnboarding();
        await __tick(10);
        answer({settings: {...ui.state.settings, onboarding_completed: true}});
        await __flush(); await first; await second; await __tick(10);
        return __apiCalls.filter(c => c.name === 'complete_onboarding').length;
        """
    )
    assert result.value == 1


# ---- second pass: image addresses, dropdowns after a redraw, Tab, card actions, Downloads, suggestions


def test_image_addresses_from_metadata_keep_to_web_inline_and_own_server(page) -> None:
    result = page.run(
        r"""
        const kept = ['https://s4.anilist.co/x.jpg', 'http://example.invalid/y.png', '/covers/7.list.jpg', 'covers/7.jpg',
                      'data:image/png;base64,AAAA', 'blob:http://127.0.0.1/abc'];
        const dropped = ['javascript:alert(1)', ' java\tscript:alert(1)', 'file:///etc/passwd', 'data:text/html,<b>x</b>',
                         'pudge-app://open', 'vbscript:x', ''];
        const out = {kept: kept.map(pudgeSafeMediaUrl), dropped: dropped.map(pudgeSafeMediaUrl)};
        await __tick(100);
        const card = document.querySelector('#currentContent article[data-media-id="5"]');
        out.cardImages = [...card.querySelectorAll('img')].map(img => img.getAttribute('src'));
        out.placeholder = Boolean(card.querySelector('[data-pudge-cover-placeholder]'));
        ensureGlobalSearchOverlay();
        renderGlobalSearchResults([{kind: 'anime', title: 'Bad', cover: 'javascript:alert(1)'}, {kind: 'anime', title: 'Good', cover: '/covers/1.jpg'}], 'x');
        out.search = [...document.querySelectorAll('#globalSearchResults img')].map(img => img.getAttribute('src'));
        return out;
        """,
        state={
            "home": {
                "caught_up": [
                    {
                        "media_id": 5,
                        "title": "Show",
                        "cover": "javascript:alert(1)",
                        "cover_remote": "file:///x.jpg",
                    }
                ]
            }
        },
    )
    out = result.value
    assert out["kept"] == [
        "https://s4.anilist.co/x.jpg",
        "http://example.invalid/y.png",
        "/covers/7.list.jpg",
        "covers/7.jpg",
        "data:image/png;base64,AAAA",
        "blob:http://127.0.0.1/abc",
    ]
    assert out["dropped"] == [""] * 7
    assert out["cardImages"] == [] and out["placeholder"] is True
    assert out["search"] == ["/covers/1.jpg"]


def test_planned_suggestion_covers_load_lazily_at_list_size(page) -> None:
    result = page.run(
        r"""
        renderPlannedSuggestions([
          {media_id: 1, media_kind: 'anime', title: 'One', cover: 'https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx1.jpg'},
          {media_id: 2, media_kind: 'anime', title: 'Two', cover: ''},
          {media_id: 3, media_kind: 'anime', title: 'Three', cover: 'javascript:alert(1)'},
        ], 'abc');
        return [...document.querySelectorAll('#plannedSuggestions article')].map(card => {
          const img = card.querySelector('img');
          return img ? {src: img.getAttribute('src'), loading: img.getAttribute('loading'), decoding: img.getAttribute('decoding'),
                        width: img.getAttribute('width'), height: img.getAttribute('height')} : null;
        });
        """
    )
    assert result.value == [
        {
            "src": "https://s4.anilist.co/file/anilistcdn/media/anime/cover/medium/bx1.jpg",
            "loading": "lazy",
            "decoding": "async",
            "width": "42",
            "height": "58",
        },
        None,
        None,
    ]


AUDIOBOOKS = {
    "audiobook_state": {
        "books": [
            {"id": 4, "title": "Audio", "duration": 600},
            {"id": 5, "title": "Other", "duration": 600},
        ]
    },
    "audiobook_set_speed": {"ok": True},
}


def test_an_open_speed_menu_keeps_working_after_the_cards_are_drawn_again(page) -> None:
    result = page.run(
        r"""
        setPage('audiobooks', true); await __tick(300);
        await __key('a', {metaKey: true}, 'body');
        await __fire('[data-audiobook-id="4"] .pudge-select:has([data-audio-speed]) .pudge-select-button', 'pointerdown', {button: 0}, 'PointerEvent');
        const before = document.querySelector('[data-audiobook-id="4"] [data-audio-speed]');
        // Escape leaves the card selection first: the cards are drawn again, the menu stays open.
        window.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', code: 'Escape', bubbles: true, cancelable: true}));
        await __tick(50);
        const after = document.querySelector('[data-audiobook-id="4"] [data-audio-speed]');
        const menu = document.querySelector('body > .pudge-select-menu.open');
        const option = [...menu.querySelectorAll('[data-pudge-select-index]')].find(button => button.textContent.includes('1.5'));
        option.click(); await __tick(50);
        return {redrawn: before !== after, menus: document.querySelectorAll('body > .pudge-select-menu.open').length};
        """,
        api=AUDIOBOOKS,
    )
    assert result.value == {"redrawn": True, "menus": 0}
    assert result.calls_of("audiobook_set_speed") == [[4, 1.5]]


def test_tab_stays_inside_the_open_dialog(page) -> None:
    result = page.run(
        r"""
        await openDownloadCenter(null); await __tick(50);
        const dialog = document.querySelector('#modalBackdrop .modal');
        const items = [...dialog.querySelectorAll('button:not([disabled]),input:not([disabled])')].filter(el => el.checkVisibility());
        const first = items[0], last = items[items.length - 1];
        last.focus(); await __key('Tab');
        const wrapped = document.activeElement === first;
        first.focus(); await __key('Tab', {shiftKey: true});
        const back = document.activeElement === last;
        $('refreshAll').focus(); await __key('Tab');
        const pulledIn = document.activeElement === first;
        // In the middle Tab is left to the browser.
        items[1].focus(); const middle = await __key('Tab');
        closeModal(); await __tick(10);
        $('refreshAll').focus(); const closed = await __key('Tab');
        return {wrapped, back, pulledIn, middleDefault: middle.defaultPrevented, closedDefault: closed.defaultPrevented};
        """,
        api={"torrent_downloads": DOWNLOADS},
    )
    assert result.value == {
        "wrapped": True,
        "back": True,
        "pulledIn": True,
        "middleDefault": False,
        "closedDefault": False,
    }


READY = {
    "home": {
        "new_ready": [
            {"media_id": 1, "title": "Ready", "local": {"video_path": "/v/ready.mkv"}, "ready_episodes": [1]},
        ],
        "caught_up": [{"media_id": 2, "title": "Caught", "site_url": "https://anilist.co/anime/2"}],
    }
}


def test_a_double_click_on_a_card_runs_its_action_once(page) -> None:
    result = page.run(
        r"""
        await __tick(100);
        let started; __api.play = () => new Promise(resolve => { started = resolve; });
        const ready = '#currentContent article[data-media-id="1"]', caught = '#currentContent article[data-media-id="2"]';
        await __click(ready); await __click(ready); await __tick(10);
        const playsWhileStarting = __apiCalls.filter(c => c.name === 'play').length;
        started({status: 'starting'}); await __tick(10);
        await __click(caught); await __click(caught); await __tick(10);
        const opened = __apiCalls.filter(c => c.name === 'open_url').length;
        await __tick(1000);
        await __click(caught); await __tick(10);
        return {playsWhileStarting, opened, openedLater: __apiCalls.filter(c => c.name === 'open_url').length};
        """,
        state=READY,
        api={"review_gate_status": {"blocking": False}, "play_status": {"status": "running"}},
    )
    assert result.value == {"playsWhileStarting": 1, "opened": 1, "openedLater": 2}


def test_the_downloads_window_is_patched_in_place_on_each_refresh(page) -> None:
    result = page.run(
        r"""
        await openDownloadCenter(null); await __tick(50);
        // The only torrent in view is open by itself.
        const pause = document.querySelector('#modalBody [data-action="torrent-pause"]');
        pause.focus();
        const row = document.querySelector('#modalBody [data-torrent-group="abc"]');
        window.PROGRESS = 0.8;
        await __tick(2100);
        return {sameButton: document.querySelector('#modalBody [data-action="torrent-pause"]') === pause,
                focused: document.activeElement === pause, sameRow: document.querySelector('#modalBody [data-torrent-group="abc"]') === row,
                open: !document.querySelector('#modalBody [data-torrent-group="abc"] article').hidden,
                percent: document.querySelector('#modalBody [data-torrent-group="abc"] .torrent-progress small').textContent};
        """,
        before="window.PROGRESS = 0.4; window.DL = "
        + json.dumps(DOWNLOADS)
        + "; __api.torrent_downloads = async () => ({...window.DL, downloads: window.DL.downloads.map(row => ({...row, progress: window.PROGRESS}))});",
    )
    assert result.value == {
        "sameButton": True,
        "focused": True,
        "sameRow": True,
        "open": True,
        "percent": "80%",
    }
