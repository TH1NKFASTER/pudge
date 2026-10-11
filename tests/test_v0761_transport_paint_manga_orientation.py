"""Read together: a quick Pause+Resume reaches mpv as two commands and the highlight keeps
moving; the pause bridge does not build library state. Manga: an existing OCR artifact
without the backend's orientation fix is still drawn vertically when its geometry and text
say so (behaviour; audit 71Б rewrite)."""

from __future__ import annotations

from css_support import css_rules
from pudge.web_app import WebAppApi
from t71g21_support import READER, paired
from tests.t71g06_support import reader


def test_pause_bridge_does_not_build_the_full_audiobook_library_state() -> None:
    calls: list[tuple[object, ...]] = []

    class FakeAudiobooks:
        def set_paused(self, book_id: int, paused: bool) -> dict[str, object]:
            calls.append(("set_paused", book_id, paused))
            return {"ok": True, "book_id": book_id, "paused": paused}

        def state(self) -> dict[str, object]:
            raise AssertionError("latency-sensitive pause must not build library state")

    api = WebAppApi.__new__(WebAppApi)
    api.audiobooks = FakeAudiobooks()

    assert api.audiobook_set_paused(75, True) == {
        "ok": True,
        "book_id": 75,
        "paused": True,
    }
    assert calls == [("set_paused", 75, True)]


# mpv takes 60 ms to answer a pause / resume.
SLOW_MPV = """
const setPausedNow = __api.audiobook_set_paused;
__api.audiobook_set_paused = async (...args) => { await new Promise(resolve => setTimeout(resolve, 60)); return setPausedNow(...args); };
"""


def test_quick_pause_and_resume_keep_the_text_moving(page) -> None:
    # Old bug: Pause and Resume pressed before mpv answered the first were coalesced; when
    # the late pause answer arrived it stopped the highlight's clock although the narration
    # played on, and the text stood still until the backend answered again.
    result = page.run(
        READER
        + r"""
        await openReader(); await __tick(1000);
        const since = __apiCalls.length;
        await __key(' ', {code: 'Space'}, 'body');      // Pause ...
        await __key(' ', {code: 'Space'}, 'body');      // ... and Resume at once
        AUDIO.hold = true;                              // the backend is busy: no state answers for now
        const samples = [];
        for (let k = 0; k < 6; k++) { await __tick(300); samples.push(shown()); }
        return {shown: samples, transport: __apiCalls.slice(since).filter(c => c.name === 'audiobook_set_paused').map(c => c.args),
                playing: AUDIO.playing, label: __text('#lnPairedAudio'), pause: t('ui.pause')};
        """,
        **paired(True, extra=SLOW_MPV),
    )
    out = result.value
    # Both presses reach mpv, in order; the narration plays on and so does the highlight
    # (one character a second here).
    assert out["transport"] == [[75, True], [75, False]]
    assert out["playing"] is True and out["label"] == out["pause"]
    assert out["shown"][-1] - out["shown"][0] > 1.2, out["shown"]
    assert not result.called("audiobook_stop") and not result.called("light_novel_play_paired")


def test_reader_draws_old_cached_horizontal_bubbles_vertically(page) -> None:
    # Regions from an OCR artifact made before the backend normalized orientation.
    bubble = {
        "text": "おれの財宝か？欲しけりゃくれてやる",
        "orientation": "horizontal",
        "detector": "vision-contrast+vision-original+vision-rectangles",
        "x": 0.697947,
        "y": 0.5315,
        "width": 0.180421,
        "height": 0.0945,
    }
    caption = {
        "text": "冒険の夜明け",
        "orientation": "horizontal",
        "detector": "vision-contrast+vision-original",
        "x": 0.1,
        "y": 0.1,
        "width": 0.212,
        "height": 0.043667,
    }
    result = reader(
        page,
        r"""
        const drawn = Object.fromEntries([...document.querySelectorAll('#mangaV2Pages .manga-v2-text-region')].map(node =>
          [node.getAttribute('aria-label'), node.classList.contains('vertical-text')]));
        const overlays = (await exportDebug()).frames[0].overlays.map(o => [o.text, o.effective_orientation, o.orientation_reason]);
        return {drawn, overlays};
        """,
        regions={0: [bubble, caption]},
        before="__api.manga_export_ocr_debug = async () => ({path: '/logs/ocr.zip'});",
    )
    out = result.value
    assert out["drawn"] == {"おれの財宝か？欲しけりゃくれてやる": True, "冒険の夜明け": False}
    # The OCR debug log says how each region was drawn, and why.
    assert sorted(out["overlays"]) == [
        ["おれの財宝か？欲しけりゃくれてやる", "vertical", "low-confidence-vision-japanese"],
        ["冒険の夜明け", "horizontal", "horizontal"],
    ]
    # A vertical bubble's text runs top to bottom, right to left.
    assert (
        css_rules("manga_reader_v2.css").get(
            ".manga-v2-text-region.vertical-text .manga-v2-region-content", "writing-mode"
        )
        == "vertical-rl"
    )
