"""Patch E: interface fixes from the audit (items 59-67 and the UI review); window parts as
behaviour in the page harness (audit 71Б rewrite)."""

from __future__ import annotations

import re
from pathlib import Path

from pudge.config import AppConfig, load_config, write_config
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi
from css_support import css_rules
from study_page_support import GATE, LN_BOOK, SELECTION
from tests.i18n_support import catalog, en, has_text, ru


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


class _Log:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def _add(self, message: str, *args: object) -> None:
        self.lines.append(message % args if args else message)

    info = warning = error = debug = exception = _add


# Backend


def test_keep_dropped_cancels_only_pending_deletion(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    db = api.manager.db
    for media_id in (501, 502):
        db.upsert_anime(
            LibraryAnime(media_id=media_id, title=f"Show {media_id}", status="CURRENT", episodes=12)
        )
        video = tmp_path / f"{media_id}.mkv"
        video.write_bytes(b"x")
        db.upsert_episode(
            LibraryEpisode(
                media_id=media_id,
                title=f"Show {media_id}",
                episode=1,
                video_path=video,
                subtitle_path=None,
                state="ready",
            )
        )
        db.schedule_anime_cleanup(media_id, after_hours=24)

    result = api.keep_dropped_anime(501)

    assert result["ok"] is True and result["kept"] == 1
    with db.connect() as conn:
        rows = dict(conn.execute("SELECT media_id, delete_after FROM episodes").fetchall())
    assert rows[501] is None
    assert rows[502] is not None
    assert api.keep_dropped_anime(501)["kept"] == 0


def test_ui_errors_reach_the_log(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.logger = _Log()
    api.log_ui_error(
        {
            "kind": "unhandledrejection",
            "message": "boom",
            "source": "index.html",
            "line": "12",
            "column": "x",
            "stack": "at f",
        }
    )
    assert any(
        line.startswith("FAIL ui.js_error kind=unhandledrejection message='boom'")
        and "line=12 column=0" in line
        for line in api.logger.lines
    )


def test_diagnostics_text_redacts_secrets(tmp_path: Path, monkeypatch) -> None:
    import pudge.web_app as web_app

    api = make_api(tmp_path)
    monkeypatch.setattr(
        web_app,
        "tail_log",
        lambda limit=200: ["EVENT ok", "request api_key=SECRET123 done", "Authorization: Bearer TOKEN456"],
    )
    text = api.diagnostics_text()
    assert "Torrent backend:" in text and "Safe mode:" in text
    assert "SECRET123" not in text and "TOKEN456" not in text
    assert "api_key=[REDACTED]" in text
    monkeypatch.setattr(web_app.sys, "platform", "linux")
    copied = api.copy_diagnostics()
    assert copied["copied"] is False and copied["text"].startswith("Pudge ")


def test_op_ed_skip_settings_round_trip(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    result = api.save_settings({"skip_segments_enabled": False, "shortcut_mpv_skip_segment": "s"})
    settings = result["settings"]
    # One option is left: the skip button (detection follows it, no automatic skipping).
    assert settings["skip_segments_enabled"] is False
    assert not {"intro_detection_enabled", "auto_skip_intro", "auto_skip_outro"} & set(settings)
    assert settings["shortcut_mpv_skip_segment"] == "s"
    saved = load_config(api.config.config_path, resolve_secrets=False)
    assert saved.playback.skip_segments_enabled is False
    assert saved.shortcuts.mpv_skip_segment == "s"

    # A key Pudge already uses in mpv falls back to Tab.
    assert (
        api.save_settings({"shortcut_mpv_skip_segment": "Ctrl+a"})["settings"]["shortcut_mpv_skip_segment"]
        == "Tab"
    )
    # A save from an older window without this field leaves it alone.
    assert api.save_settings({"language": "en"})["settings"]["skip_segments_enabled"] is False


# Window


def test_sidebar_status_line_shows_work_and_errors(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            const line = () => ({shown: __visible('#status'), text: __text('#status'), error: $('status').classList.contains('error')});
            const out = {idle: line()};
            let finish; __api.refresh_local = () => new Promise((resolve, reject) => { finish = {resolve, reject}; });
            await __click('#refreshAll'); await __tick(10);
            out.working = line();
            finish.resolve({state: await __api.get_state(), stats: {}}); await __tick(50);
            out.done = line();
            await __click('#refreshAll'); await __tick(10);
            finish.reject(new Error('disk full')); await __tick(50);
            out.failed = line();
            return {...out, expected: {working: t('status.refreshing'), failed: t('error.generic', {error: 'disk full'})}};
            """,
            lang=lang,
            api={"startup_maintenance": {"running": False}},
        )
        out = result.value
        assert out["idle"] == {"shown": False, "text": "", "error": False}
        assert out["working"] == {"shown": True, "text": out["expected"]["working"], "error": False}
        assert out["done"]["shown"] is False
        assert out["failed"] == {"shown": True, "text": out["expected"]["failed"], "error": True}


def test_sidebar_status_texts_fit_and_wrap_instead_of_cutting() -> None:
    status = css_rules("index.html").declarations(".status")
    assert status["white-space"] == "normal" and "text-overflow" not in status
    # About 28 characters fit on one sidebar line at 12 px.
    for lang in ("en", "ru"):
        texts = {key: value for key, value in catalog(lang).items() if key.startswith("status.")}
        assert len(texts) >= 10
        for key, text in texts.items():
            assert len(text) <= 28, (lang, key, text)


# A title waiting for subtitles: its episode is on disk but not ready.
WAITING = {
    "media_id": 7,
    "title": "Show",
    "next_episode": 3,
    "site_url": "https://anilist.co/anime/7",
    "local": {"episode": 3, "state": "waiting_subtitles", "video_path": "/lib/Show - 03.mkv"},
}
DIAGNOSIS = {
    "title": "Show",
    "ready": False,
    "video_path": "/lib/Show - 03.mkv",
    "checks": [
        {"ok": True, "label": "Video", "detail": "found"},
        {"ok": False, "label": "Japanese subtitles", "detail": "not found yet"},
    ],
}
HOME = {
    "state": {"home": {"waiting": [WAITING]}},
    "api": {"diagnose_episode": DIAGNOSIS, "play": {"ok": True, "status": "starting"}},
}


def test_waiting_card_opens_why_not_ready_and_offers_watching_without_subtitles(page) -> None:
    assert en("action.watchWithoutJpSubs") == "Watch without Japanese subtitles"
    assert ru("action.watchWithoutJpSubs") == "Смотреть без японских субтитров"
    for lang in ("en", "ru"):
        result = page.run(
            """
            const open = async () => { await __click('#currentContent .waiting-card[data-media-id="7"]'); await __tick(20); };
            await open();
            const out = {title: __text('#modalTitle'), buttons: [...document.querySelectorAll('#modalBody button')].map(b => [b.dataset.action, b.textContent]),
                         expected: {title: `${t('action.whyNotReady')} — Show`, buttons: [
                           ['watch-diagnostic-without-subs', t('action.watchWithoutJpSubs')], ['retry-diagnostic', t('action.retryPreparation')],
                           ['choose-diagnostic-subtitle', t('action.chooseSubtitle')], ['find-diagnostic-release', t('action.findEpisode')]]}};
            await __click('[data-action="watch-diagnostic-without-subs"]'); await __tick(20);
            out.closed = !$('modalBackdrop').classList.contains('open');
            await open();
            await __click('[data-action="find-diagnostic-release"]'); await __tick(20);
            out.release = __text('#modalTitle');
            return out;
            """,
            lang=lang,
            **HOME,
        )
        out = result.value
        assert out["title"] == out["expected"]["title"]
        # No dead "open maintenance" button: exactly these actions.
        assert out["buttons"] == out["expected"]["buttons"]
        assert result.calls_of("diagnose_episode")[0] == [7, 3]
        assert out["closed"] is True
        assert result.calls_of("play") == [["/lib/Show - 03.mkv", True, False, False]]  # resume, plain video
        assert result.calls_of("search_releases") == [[7, 3, False]]


def test_cards_get_a_menu_button_that_opens_their_menu(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            const menuFor = async selector => {
              const button = document.querySelector(`${selector} > .card-menu-button`);
              const label = button?.getAttribute('aria-label');
              await __click(`${selector} > .card-menu-button`); await __tick(10);
              const out = {label, open: $('contextMenu').classList.contains('open'), items: [...$('contextMenu').querySelectorAll('button')].map(b => b.dataset.contextAction),
                           focused: document.activeElement?.dataset.contextAction ?? null};
              hideContextMenu();
              return out;
            };
            const out = {waiting: await menuFor('#currentContent .waiting-card[data-media-id="7"]')};
            setPage('planned'); await __tick(50);
            out.planned = await menuFor('#plannedContent .anime-card[data-media-id="8"]');
            setPage('current'); await __tick(50);
            await __click('#currentContent .waiting-card[data-media-id="7"] > .card-menu-button'); await __tick(10);
            await __click('#contextMenu [data-context-action="open"]'); await __tick(10);
            return {...out, expected: PudgeI18n.t('ui.actions')};
            """,
            lang=lang,
            state={
                "home": {"waiting": [WAITING]},
                "planned": [
                    {
                        "media_id": 8,
                        "title": "Later",
                        "status": "PLANNING",
                        "site_url": "https://anilist.co/anime/8",
                    }
                ],
            },
            api={"diagnose_episode": DIAGNOSIS},
        )
        out = result.value
        for card in ("waiting", "planned"):
            assert out[card]["label"] == out["expected"] and out[card]["open"] is True
            assert out[card]["items"][0] == "open" and out[card]["focused"] == "open"
        assert "diagnose" in out["waiting"]["items"]
        # The menu button does not run the card's own action (Why not ready?).
        assert not result.called("diagnose_episode")
        assert result.calls_of("open_url") == [["https://anilist.co/anime/7"]]
    rules = css_rules("index.html")
    hidden = rules.declarations(".card-menu-button")
    assert (hidden["opacity"], hidden["pointer-events"], hidden["position"]) == ("0", "none", "absolute")
    for selector in (
        ".airing-card:hover>.card-menu-button",
        ".anime-card:hover>.card-menu-button",
        ".card-menu-button:focus-visible",
        "article:focus-within>.card-menu-button",
    ):
        assert (rules.get(selector, "opacity"), rules.get(selector, "pointer-events")) == ("1", "auto"), (
            selector
        )
    assert rules.get(".anime-card", "position") == "relative"


def _luminance(hex_color: str) -> float:
    channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast_with_white(hex_color: str) -> float:
    return 1.05 / (_luminance(hex_color) + 0.05)


def test_accent_passes_contrast_with_white_text(page) -> None:
    accent = css_rules("index.html").get(":root", "--accent")
    assert _contrast_with_white(accent) >= 4.5
    tab = css_rules("debug.css").declarations(".pudge-debug-tabs button.active")
    assert tab["color"] == "#fff" and _contrast_with_white(tab["background"]) >= 4.5
    # The confirmation dialog and the volume editor bring their own button styles.
    result = page.run(
        """
        void pudgeConfirm('Delete?'); await __tick(20); PudgeConfirm.closeIfOpen();
        void window.PudgeLibraryMetadata.edit('ln', {id: 6, title: 'Novel', volume: 1}); await __tick(20);
        return [...document.querySelectorAll('style')].map(s => s.textContent).join('\\n');
        """
    )
    for selector in (".pudge-confirm-actions .confirm", ".pudge-meta-actions .save"):
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", result.value)
        assert match, selector
        rules = dict(part.split(":", 1) for part in match.group(1).split(";") if ":" in part)
        assert rules["color"].strip() in ("white", "#fff")
        assert _contrast_with_white(rules["background"].strip()) >= 4.5, selector


TRANSLATE = {
    "before": LN_BOOK + "__api.translate_text = async () => ({translation: 'I like cats'});",
    "state": {"settings": {"review_gate_ln_enabled": False}},
}


def test_selection_only_offers_translation(page) -> None:
    # Selecting to copy sends nothing; Translate (or T, also on the Russian layout) translates.
    for lang in ("en", "ru"):
        result = page.run(
            SELECTION
            + """
            const sent = () => __apiCalls.filter(c => c.name === 'translate_text').length;
            await __settle(openLightNovel(6)); await __tick(300);
            await selectParagraph(0);
            const out = {offer: __text('#pudgeTranslationPop'), expected: `${PudgeI18n.t('reading.translate')} T`, sentOnSelect: sent()};
            await __key('е', {code: 'KeyT'}, 'body'); await __tick(10);
            out.byKey = {sent: sent(), text: __text('#pudgeTranslationPop .pudge-translation-text')};
            // Any other text the window marks as translatable (sidebar, visual novel line) offers the same way.
            const root = document.createElement('div'); root.dataset.pudgeTranslateRoot = ''; root.textContent = '犬が走る。';
            document.body.appendChild(root);
            select(root); await __fire('[data-pudge-translate-root]', 'mouseup', {button: 0}, 'MouseEvent'); await __tick(10);
            out.other = {offer: __text('#pudgeTranslationPop'), sent: sent()};
            await press('[data-pudge-translate-now]'); await __tick(10);
            out.otherSent = __apiCalls.filter(c => c.name === 'translate_text').map(c => c.args[0]);
            return out;
            """,
            lang=lang,
            **TRANSLATE,
        )
        out = result.value
        assert out["offer"] == out["expected"] and out["sentOnSelect"] == 0
        assert out["byKey"] == {"sent": 1, "text": "I like cats"}
        assert out["other"] == {"offer": out["expected"], "sent": 1}
        assert out["otherSent"] == ["猫が好きだ。", "犬が走る。"]


def test_t_in_a_text_field_does_not_translate(page) -> None:
    result = page.run(
        SELECTION
        + """
        await __settle(openLightNovel(6)); await __tick(300);
        await selectParagraph(0);
        const field = document.createElement('input'); field.id = 'typing'; document.body.appendChild(field);
        await __key('t', {code: 'KeyT'}, '#typing'); await __tick(10);
        return __apiCalls.filter(c => c.name === 'translate_text').length;
        """,
        **TRANSLATE,
    )
    assert result.value == 0


def test_ln_shelf_is_not_rebuilt_while_reading(page) -> None:
    result = page.run(
        """
        setPage('lightnovels'); await __tick(50);
        const shelf = () => document.querySelector('#lightNovelsContent [data-ln-book="6"]');
        const before = shelf();
        await __settle(openLightNovel(6)); await __tick(300);
        await __click('#lnReader [data-ln-token="0:2"]'); await __tick(20);
        await __click('[data-pudge-study-extra-action="bookmark-here"]'); await __tick(20);
        const out = {same: shelf() === before, progressWhileReading: shelf().querySelector('.ln-card-progress span').style.width,
                     toast: __toasts().at(-1), expected: PudgeI18n.t('ui.bookmarkAndProgressUpdated')};
        await __click('#lnReaderClose'); await __tick(50);
        out.rebuilt = shelf() !== before;
        out.progressAfter = shelf().querySelector('.ln-card-progress span').style.width;
        return out;
        """,
        before=LN_BOOK
        + "__api.light_novel_bookmark = async (id, chapter, offset) => ({reading_progress: 0.5, read_character_count: 500, "
        "character_count: 1000, current_chapter: chapter, current_offset: offset});",
        state={"settings": {"review_gate_ln_enabled": False}},
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [
                    {
                        "id": 6,
                        "title": "Novel",
                        "volume": 1,
                        "reading_progress": 0.1,
                        "character_count": 1000,
                        "read_character_count": 100,
                    }
                ],
            }
        },
    )
    out = result.value
    assert out["same"] is True and out["progressWhileReading"] == "10%"
    assert out["toast"] == out["expected"]
    assert out["rebuilt"] is True and out["progressAfter"] == "50%"
    assert result.calls_of("light_novel_bookmark")[0][:2] == [6, 0]


def test_op_ed_skip_and_skip_key_are_in_settings(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('settings'); await __tick(20);
            await __click('[role="tab"][data-settings-category="advanced"]'); await __tick(10);
            const block = $('settingsOpEdSkip');
            const out = {shown: __visible('#settingsOpEdSkip'), heading: __text('#settingsOpEdSkip h3'),
                         controls: [...block.querySelectorAll('input')].map(node => [node.id, node.checked]),
                         // The usual left-aligned settings toggle, not a checkbox in the form grid's right column.
                         toggle: block.querySelector('#s_skip_segments').closest('label').className,
                         grid: Boolean(block.querySelector('.form-grid')),
                         toggleText: __text('#settingsOpEdSkip label'), expectedToggle: PudgeI18n.t('ui.skipButtonInMpv'),
                         hint: __text('#settingsOpEdSkip p.subtle'), expectedHint: PudgeI18n.t('ui.pudgeFindsOpEdFrom'),
                         expected: PudgeI18n.t('ui.openingsAndEndings'), skipKey: $('s_shortcut_mpv_skip').dataset.shortcutValue,
                         skipLabel: $('s_shortcut_mpv_skip').getAttribute('aria-label'), expectedLabel: PudgeI18n.t('ui.skipOpEdWhileThe')};
            await __input('#s_skip_segments', false);
            out.skipKeyDisabled = $('s_shortcut_mpv_skip').disabled;
            await __click('#saveSettings'); await __tick(10);
            return out;
            """,
            lang=lang,
            state={
                "settings": {
                    "skip_segments_enabled": True,
                    "shortcut_mpv_skip_segment": "s",
                }
            },
            before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
        )
        out = result.value
        assert out["shown"] is True and out["heading"] == out["expected"]
        assert out["controls"] == [["s_skip_segments", True]]
        assert out["toggle"] == "setting-toggle" and out["grid"] is False
        assert out["toggleText"] == out["expectedToggle"] and out["hint"] == out["expectedHint"]
        assert out["skipKey"] == "s" and out["skipLabel"] == out["expectedLabel"]
        assert out["skipKeyDisabled"] is True, "the skip key needs the button"
        [[values]] = result.calls_of("save_settings")
        assert values["skip_segments_enabled"] is False and values["shortcut_mpv_skip_segment"] == "s"
        assert not {"intro_detection_enabled", "auto_skip_intro", "auto_skip_outro"} & set(values)
    # No key yet: the recorder shows Tab; a cleared key (Backspace while recording) saves Tab too.
    result = page.run(
        """
        setPage('settings'); await __tick(20);
        const shown = $('s_shortcut_mpv_skip').dataset.shortcutValue;
        await __click('#s_shortcut_mpv_skip');
        await __key('Backspace', {code: 'Backspace'}, '#s_shortcut_mpv_skip');
        const cleared = $('s_shortcut_mpv_skip').dataset.shortcutValue;
        await __click('#saveSettings'); await __tick(10);
        return {shown, cleared};
        """,
        state={"settings": {"shortcut_mpv_skip_segment": ""}},
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    assert result.value == {"shown": "Tab", "cleared": ""}
    assert result.calls_of("save_settings")[0][0]["shortcut_mpv_skip_segment"] == "Tab"


def test_at_most_three_toasts_and_no_duplicates(page) -> None:
    result = page.run(
        """
        for (const text of ['one', 'two', 'three', 'four']) toast(text);
        toast('four');
        await __tick(20);
        return __toasts();
        """
    )
    assert result.value == ["two", "three", "four"]


def test_dialogs_take_focus_and_give_it_back(page) -> None:
    result = page.run(
        """
        $('refreshAll').focus();
        const shown = openEpisodeDiagnostics(ui.state.home.waiting[0]); await __tick(50); await shown;
        const dialog = document.querySelector('#modalBackdrop [role="dialog"]');
        const out = {modal: dialog?.getAttribute('aria-modal'), title: dialog && document.getElementById(dialog.getAttribute('aria-labelledby'))?.id,
                     focusInside: Boolean(dialog?.contains(document.activeElement))};
        await __key('Escape', {code: 'Escape'}, 'body'); await __tick(20);
        out.closed = !$('modalBackdrop').classList.contains('open');
        out.focus = document.activeElement?.id;
        return out;
        """,
        **HOME,
    )
    assert result.value == {
        "modal": "true",
        "title": "modalTitle",
        "focusInside": True,
        "closed": True,
        "focus": "refreshAll",
    }


def test_window_errors_reach_the_log_and_say_so(page) -> None:
    # An unhandled promise rejection: a toast and one line in the app log per message and
    # 15 s. (Script errors take the same path; the harness cannot raise a window "error"
    # event whose target is the page's window.)
    result = page.run(
        """
        Promise.reject(new Error('lost promise')); await __tick(20);
        Promise.reject(new Error('lost promise')); await __tick(20);      // repeated at once: not logged again
        await __tick(15000);
        Promise.reject(new Error('lost promise')); await __tick(20);
        return {toasts: __toasts(), expected: PudgeI18n.t('ui.interfaceError', {text: 'lost promise'})};
        """,
        allow_errors=True,
    )
    assert result.value["expected"] in result.value["toasts"]
    reports = [call[0] for call in result.calls_of("log_ui_error")]
    assert [(r["kind"], r["message"]) for r in reports] == [("rejection", "lost promise")] * 2
    assert "lost promise" in reports[0]["stack"]


def test_new_release_popup_waits_while_the_review_gate_is_open(page) -> None:
    result = page.run(
        """
        const popup = () => $('plannedReleaseBackdrop').classList.contains('open');
        await startPlay(EPISODE); await __tick(20);
        window.OFFERS = [{media_id: 8, title: 'Later', format: 'TV'}];
        await __tick(60000);                                   // the minute poll finds a release
        const out = {duringGate: popup(), gate: PudgeReviewGate.isOpen()};
        await __click('[data-review-gate-close]');
        await __tick(60000);
        out.afterGate = popup(); out.heading = __text('#plannedReleaseHeading'); out.expected = PudgeI18n.t('ui.newReleases');
        return out;
        """,
        before=GATE
        + "window.OFFERS = []; __api.poll_planned_release_discovery = async () => ({offers: window.OFFERS});",
    )
    out = result.value
    assert out["gate"] is True and out["duringGate"] is False
    assert out["afterGate"] is True and out["heading"] == out["expected"]


def test_copy_diagnostics_falls_back_to_the_window_clipboard(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            setPage('settings'); await __tick(20);
            await __click('#copyDiagnostics'); await __tick(20);
            return {toast: __toasts().at(-1), expected: PudgeI18n.t('ui.diagnosticsCopiedPasteThemInto'), label: __text('#copyDiagnostics'),
                    expectedLabel: t('action.copyDiagnostics')};
            """,
            lang=lang,
            api={"copy_diagnostics": {"copied": False, "text": "Pudge 1.0\nSafe mode: off"}},
        )
        assert result.value["toast"] == result.value["expected"]
        assert result.value["label"] == result.value["expectedLabel"]
        assert result.clipboard == ["Pudge 1.0\nSafe mode: off"]


def test_old_name_is_gone_from_the_window() -> None:
    assert not has_text("Anime MPV", "en") and not has_text("Anime MPV", "ru")


def test_old_name_is_not_shown(page) -> None:
    result = page.run("return {title: document.title, text: document.body.innerText};")
    assert "Anime MPV" not in result.value["title"] and "Anime MPV" not in result.value["text"]


def test_dropped_title_can_be_kept(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            const keep = document.querySelector('#currentContent [data-action="keep-dropped"]');
            const out = {label: keep?.textContent, title: keep?.title, expected: [PudgeI18n.t('ui.keep'), PudgeI18n.t('ui.doNotDeleteThisTitle')]};
            await __click('#currentContent [data-action="keep-dropped"]'); await __tick(20);
            return {...out, toast: __toasts().at(-1), kept: PudgeI18n.t('ui.theFilesWillBeKept')};
            """,
            lang=lang,
            state={
                "home": {
                    "dropped": [
                        {
                            "media_id": 9,
                            "title": "Dropped",
                            "delete_at": 1790900000,
                            "delete_remaining_seconds": 7200,
                        }
                    ]
                }
            },
            api={"keep_dropped_anime": {"ok": True, "kept": 1}},
        )
        out = result.value
        assert [out["label"], out["title"]] == out["expected"]
        assert result.calls_of("keep_dropped_anime") == [[9]]
        assert out["toast"] == out["kept"]


def test_escape_on_the_first_launch_guide_does_not_skip_setup(page) -> None:
    result = page.run(
        """
        const open = () => $('onboardingBackdrop').classList.contains('open');
        const out = {first: open()};
        await __key('Escape', {code: 'Escape'}, 'body'); await __tick(20);
        out.afterEscape = open();
        return out;
        """,
        state={"settings": {"onboarding_completed": False}},
    )
    assert result.value == {"first": True, "afterEscape": True}
    assert not result.called("skip_onboarding")
    # Opened again from Settings, Escape just closes it.
    result = page.run(
        """
        setPage('settings'); await __tick(20);
        await __click('#runSetupGuide'); await __tick(20);
        const opened = $('onboardingBackdrop').classList.contains('open');
        await __key('Escape', {code: 'Escape'}, 'body'); await __tick(20);
        return {opened, closed: !$('onboardingBackdrop').classList.contains('open')};
        """
    )
    assert result.value == {"opened": True, "closed": True}
    assert not result.called("skip_onboarding")
