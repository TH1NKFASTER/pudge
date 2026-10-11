"""Read-together and audiobook controls share labels, shortcuts and instant feedback
(behaviour, page harness; audit 71Б rewrite)."""

import json

from tests.i18n_support import en, has_text, keys_with_text, ru

# Light novel 6 read together with audiobook 75 (alignment ready). window.__state is what the
# backend reports; the transport calls wait until the test releases them (window.__release()).
PAIRED_LN = """
window.__state = {audiobook_id: 75, ln_book_id: 6, ln_chapter_index: 0, alignment: {ready: true, status: 'ready'}, alignment_mode: 'stt', linked: true,
  playing: PLAYING, player_running: PLAYING, paused: false, position: 100, speed: 1, duration: 3600, chapter_char_offset_exact: 0, chapter_char_offset: 0,
  anchor_window: {left_time: 100, left_offset: 0, right_time: 104, right_offset: 6, path: [{time: 100, offset: 0}, {time: 104, offset: 6}]}};
__api.light_novel_open = async id => ({id, title: 'Novel', current_chapter: 0, current_offset: 0, chapters: [{chapter_index: 0, title: 'One'}],
  paired_audio: {ln_book_id: id, alignment_mode: 'stt', alignment: {ready: true}, book: {id: 75, playing: PLAYING, player_running: PLAYING, paused: false}}});
__api.light_novel_chapter = async (id, index) => ({book_id: id, chapter_index: index, paragraphs: ['猫が好きです。'],
  tokens: [[{start: 0, end: 1, surface: '猫'}, {start: 1, end: 2, surface: 'が'}, {start: 2, end: 4, surface: '好き'}, {start: 4, end: 6, surface: 'です'}]]});
__api.light_novel_paired_state = () => Promise.resolve({...window.__state});
window.__release = () => {};
const held = playing => () => new Promise(resolve => { window.__release = () => { window.__state = {...window.__state, playing, player_running: true}; resolve({...window.__state}); }; });
__api.light_novel_play_paired = held(true);
__api.audiobook_set_paused = (_id, paused) => held(!paused)();
__api.audiobook_stop = async () => ({ok: true});
__api.audiobook_seek = async () => ({ok: true});
__api.audiobook_set_speed = async (_id, speed) => ({...window.__state, speed});
"""
LN = {
    "state": {"settings": {"review_gate_ln_enabled": False}},
    "api": {"startup_maintenance": {"running": False}},
}


def paired_ln(playing: bool) -> dict:
    return {**LN, "before": PAIRED_LN.replace("PLAYING", "true" if playing else "false")}


def test_ln_uses_short_listen_label_and_optimistic_toggle(page) -> None:
    assert not has_text("Listen together")
    # "Читать и слушать" is the LN context-menu label of the read-together action (plan item 12);
    # the transport button stays short. Audit 68Б: texts live in the catalogs.
    assert keys_with_text("Читать и слушать", "ru") == ["ui.readAndListen"]
    assert en("ui.readAndListen") == "Read and listen"
    assert (en("ui.listen"), ru("ui.listen")) == ("Listen", "Слушать")
    book = {"id": 6, "title": "Novel", "volume": 1, "paired_audio": {"audiobook_id": 75, "book": {"id": 75}}}
    for lang in ("en", "ru"):
        menu = page.run(
            """
            setPage('lightnovels', true); await __tick(100);
            await __fire('[data-ln-book="6"]', 'contextmenu', {clientX: 5, clientY: 5}, 'MouseEvent');
            return {items: [...document.querySelectorAll('[data-ln-context-action]')].filter(b => b.checkVisibility()).map(b => b.innerText),
                    page: $('lightnovels').textContent, label: PudgeI18n.t('ui.readAndListen')};
            """,
            lang=lang,
            api={"light_novel_state": {"version": 1, "settings": {}, "books": [book]}},
        )
        assert menu.value["items"].count(menu.value["label"]) == 1
        assert "Listen together" not in menu.value["page"]
        # Listen → Pause at once, before the backend has started the narration; Pause pauses (never stops).
        result = page.run(
            """
            await __settle(openLightNovel(6)); await __tick(300);
            const out = {stopped: __text('#lnPairedAudio')};
            await __click('#lnPairedAudio');
            out.starting = __text('#lnPairedAudio');
            window.__release(); await __tick(300);
            out.playing = __text('#lnPairedAudio');
            await __click('#lnPairedAudio');
            out.pausing = __text('#lnPairedAudio');
            window.__release(); await __tick(300);
            out.paused = __text('#lnPairedAudio');
            out.labels = [PudgeI18n.t('ui.listen'), PudgeI18n.t('ui.pause'), PudgeI18n.t('ui.resume')];
            return out;
            """,
            lang=lang,
            **paired_ln(False),
        )
        out = result.value
        listen, pause, resume = out["labels"]
        assert (out["stopped"], out["starting"], out["playing"], out["pausing"], out["paused"]) == (
            listen,
            pause,
            pause,
            resume,
            resume,
        )
        assert result.calls_of("light_novel_play_paired") == [[6, 0, 0, 1]]
        assert result.calls_of("audiobook_set_paused") == [[75, True]]
        assert not result.called("audiobook_stop")


AUDIOBOOK = {
    "id": 3,
    "title": "Book",
    "duration": 600,
    "position": 120,
    "playing": False,
    "player_running": False,
    "speed": 1,
}
# The audiobook page: play/stop answers wait for window.__release().
AUDIO_PAGE = """
window.__books = [BOOK];
window.__release = () => {};
const held = playing => () => new Promise(resolve => { window.__release = () => {
  window.__books = window.__books.map(b => ({...b, playing, player_running: playing})); resolve({ok: true, state: {books: window.__books}}); }; });
__api.audiobook_state = async () => ({books: window.__books});
__api.audiobook_play = held(true);
__api.audiobook_stop = held(false);
__api.audiobook_seek = async () => ({ok: true, state: {books: window.__books}});
__api.audiobook_set_speed = async (id, speed) => { window.__books = window.__books.map(b => ({...b, speed})); return {ok: true, state: {books: window.__books}}; };
""".replace("BOOK", json.dumps(AUDIOBOOK))
CARD_BUTTON = "() => __text('[data-audiobook-id=\"3\"] .primary')"


def test_ln_and_audiobook_pages_share_playback_shortcuts(page) -> None:
    keys = """
    for (const key of ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']) { await __key(key, {}, 'body'); await __tick(50); }
    await __key(' ', {code: 'Space'}, 'body'); await __tick(50); window.__release(); await __tick(300);
    """
    reader = page.run("await __settle(openLightNovel(6)); await __tick(300);" + keys, **paired_ln(True))
    assert reader.calls_of("audiobook_seek") == [[75, -5], [75, 5], [75, -15], [75, 15]]
    assert reader.calls_of("audiobook_set_paused") == [[75, True]]
    audio = page.run("setPage('audiobooks', true); await __tick(100);" + keys, before=AUDIO_PAGE)
    assert audio.calls_of("audiobook_seek") == [[3, -5], [3, 5], [3, -15], [3, 15]]
    assert audio.calls_of("audiobook_play") == [[3, None, 1]]


def test_audiobook_page_updates_play_stop_before_api_finishes(page) -> None:
    result = page.run(
        f"""
        setPage('audiobooks', true); await __tick(100);
        const button = {CARD_BUTTON};
        const out = {{stopped: button()}};
        await __click('[data-audiobook-id="3"] [data-media-action="play-audio"]');
        out.starting = button();
        window.__release(); await __tick(100);
        await __click('[data-audiobook-id="3"] [data-media-action="stop-audio"]');
        out.stopping = button();
        window.__release(); await __tick(100);
        out.stoppedAgain = button();
        out.labels = [PudgeI18n.t('ui.resume'), PudgeI18n.t('media.stop')];
        return out;
        """,
        before=AUDIO_PAGE,
    )
    out = result.value
    resume, stop = out["labels"]
    assert (out["stopped"], out["starting"], out["stopping"], out["stoppedAgain"]) == (
        resume,
        stop,
        resume,
        resume,
    )
    assert result.calls_of("audiobook_play") == [[3, None, 1]] and result.calls_of("audiobook_stop") == [[3]]
