from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_ln_uses_short_listen_label_and_optimistic_toggle() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")

    assert "Listen together" not in html
    # "Читать и слушать" is now the requested LN context-menu label for the
    # read-together action (plan item 12); the transport button stays short.
    assert html.count("Читать и слушать") == 1
    assert 'data-ln-context-action="read-together">${ui.lang===\'ru\'?\'Читать и слушать\':\'Read and listen\'}' in html
    assert "ui.lang==='ru'?'Слушать':'Listen'" in html
    assert "async function toggleLnPairedPlayback()" in html
    assert "syncLnPairedTransportIntent(desired)" in html
    assert "const optimistic={...ui.lnPairedState,playing:desired" not in html
    assert "ui.lnPairedTransportPromise" in html
    assert "ui.lnPairedStarted=true" in html
    assert html.index("syncLnPairedTransportIntent(desired)") < html.index(
        "await pywebview.api.audiobook_set_paused(Number(state.audiobook_id),true);"
    )
    assert "await pywebview.api.audiobook_stop(Number(state.audiobook_id));" not in html[
        html.index("async function toggleLnPairedPlayback()"):
        html.index("async function openLightNovel")
    ]


def test_ln_and_audiobook_pages_share_playback_shortcuts() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")
    shortcut_map = "{ArrowLeft:-5,ArrowRight:5,ArrowUp:-15,ArrowDown:15}"

    assert f"const lnAudioShortcuts={shortcut_map};" in html
    # Space arbitration moved into lnPairedSpaceTransportEvent; its behaviour
    # is executed in tests/js/ln_entry_contract.cjs (space_keydown).
    assert "event.code!=='Space'" in html
    assert "if(lnPairedSpaceTransportEvent(event)){event.preventDefault();event.stopPropagation();if(!event.repeat)void toggleLnPairedPlayback();return;}" in html

    assert f"const audioShortcuts={shortcut_map};" in media
    assert "event.code==='Space'" in media
    assert "void toggleActiveAudiobook()" in media
    assert "void seekAudiobook(book.id,audioShortcuts[event.key])" in media


def test_audiobook_page_updates_play_stop_before_api_finishes() -> None:
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")

    play = media[media.index("const playAudiobook = async"):media.index(
        "const stopAudiobook = async"
    )]
    stop = media[media.index("const stopAudiobook = async"):media.index(
        "const seekAudiobook = async"
    )]

    assert play.index("optimisticAudioPlaying(id,true);") < play.index(
        "await pywebview.api.audiobook_play"
    )
    assert stop.index("optimisticAudioPlaying(id,false);") < stop.index(
        "await pywebview.api.audiobook_stop"
    )
