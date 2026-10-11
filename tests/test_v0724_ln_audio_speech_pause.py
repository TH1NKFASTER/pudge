"""Listen together: pauses in speech (behaviour; audit 71Б rewrite).

A caller that knows nobody speaks (speechActive: false) freezes the spoken word; normal
playback never freezes on the speech-activity regions of the alignment (the old FFT
"micro-freezes"). Reader set-up: tests/t71g19_support.py (offset = audio seconds - 10)."""

from __future__ import annotations

from t71g19_support import LN_PAIRED, OPEN


def _lit(state):
    return [(word["text"], word["progress"]) for word in state if word["current"]]


def test_silence_keeps_the_spoken_word_without_progress(page) -> None:
    result = page.run(
        OPEN
        + """
        AUDIO.playing = false;
        const out = {};
        render(10.5, 0.5); out.speaking = words();                               // no option: speech
        render(10.9, 0.9, {speechActive: false}); out.pause = words();
        render(11.3, 2.5, {speechActive: false}); out.pauseLater = words();      // the clock passed the next word
        render(11.6, 2.6, {speechActive: true}); out.resumed = words();
        render(20.0, 10.5, {speechActive: false}); out.seekInSilence = words();  // a seek shows the new place at once
        out.pauses = trace('speech_pause').map(row => [row.speech_active, row.current_word && row.current_word.surface]);
        return out;
        """,
        before=LN_PAIRED,
    )
    out = result.value
    assert _lit(out["speaking"]) == [("承る", "25.0%")]
    assert out["pause"] == out["speaking"] and out["pauseLater"] == out["speaking"]
    assert _lit(out["resumed"]) == [("言い方", "20.0%")] and out["resumed"][0]["progress"] == "100%"
    assert _lit(out["seekInSilence"]) == [("話した", "16.7%")]
    assert [word["progress"] for word in out["seekInSilence"][:2]] == ["", ""]
    # The sync trace says why the highlight stood still (one record per pause, see v0729).
    assert out["pauses"] == [[False, "承る"]]


def test_speech_after_a_silent_start_lights_the_first_word(page) -> None:
    # Started in silence, only the paragraph is marked; the paragraph's first word starts at
    # the same audio position and is lit as soon as speech starts.
    result = page.run(
        OPEN
        + """
        AUDIO.playing = false;
        render(10.2, 0.2, {speechActive: false}); const silent = words();
        render(10.5, 0.5, {speechActive: true});
        return {silent, speaking: words()};
        """,
        before=LN_PAIRED,
    )
    assert _lit(result.value["silent"]) == [] and all(not word["progress"] for word in result.value["silent"])
    assert _lit(result.value["speaking"]) == [("承る", "25.0%")]


def test_playback_through_quiet_stretches_keeps_moving(page) -> None:
    # The alignment marks 10.4-12.6 s as quiet (speech-activity regions, widened by 0.7 s
    # each side: quiet from 11.1 to 11.9 s). Playback still moves the highlight on the
    # anchor clock there, at every frame and poll.
    result = page.run(
        OPEN
        + """
        AUDIO.activity = [{start: 10, end: 10.4}, {start: 12.6, end: 28}];
        const samples = [];
        while (audioAt() < 11.15) await __tick(20);
        while (audioAt() < 11.85) { await __tick(50); samples.push({lit: current(), progress: progressOf(0)}); }
        return {samples, pauses: trace('speech_pause').length};
        """,
        before=LN_PAIRED,
    )
    samples = result.value["samples"]
    assert {sample["lit"] for sample in samples} == {0}
    progress = [float(sample["progress"].rstrip("%")) for sample in samples]
    assert len(progress) >= 12 and all(b > a for a, b in zip(progress, progress[1:]))
    assert result.value["pauses"] == 0
