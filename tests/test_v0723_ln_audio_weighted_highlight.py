"""Listen together: word audio weights and the spoken-word highlight (behaviour; audit 71Б rewrite).

One feature's history in one scenario set (weighted highlight v0.7.23 and its follow-ups
v3..v6; pauses are in test_v0724_ln_audio_speech_pause.py, seeks and the transport clock in
test_v0729_ln_audio_sync_responsive.py). The reader runs in node with light novel 9 linked
to audiobook 31 (tests/t71g19_support.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pudge.web_app as web_app
from t71g19_support import LN_PAIRED, OPEN
from test_web_app import make_api

# The alignment says the first 0.2 characters take a second (a slow start), then speech
# runs at 1.8 characters per second.
SLOW_START = [(10, 0), (11, 0.2), (12, 2), (28, 18)]
SLOW_START_JS = "AUDIO.path = " + json.dumps([{"time": t, "offset": o} for t, o in SLOW_START]) + ";"


def _offset_at(time: float) -> float:
    for (t0, o0), (t1, o1) in zip(SLOW_START, SLOW_START[1:]):
        if t0 <= time <= t1:
            return o0 + (o1 - o0) * (time - t0) / (t1 - t0)
    raise AssertionError(time)


def _percent(value: str) -> float:
    return float(value.rstrip("%"))


def test_word_audio_weights_follow_the_reading(page) -> None:
    # Each character of a word gets a share of the word's spoken length: a kanji gets
    # the morae of its reading, small kana are no mora of their own, and the book's own
    # ruby wins over the dictionary reading.
    result = page.run(
        OPEN
        + "return wordNodes().map(node => [surface(node), node.dataset.lnAudioWeights, node.dataset.lnAudioReading]);",
        before=LN_PAIRED,
    )
    assert result.value == [
        ["承る", "5,1", "うけたまわる"],  # 承 = うけたまわ (5 morae), る = 1
        ["言い方", "1,1,2", "言[い]い方[かた]"],  # bracket notation: only 方 has two morae
        ["大人", "1.5,1.5", "大人[おとな]"],  # three morae over two kanji
        ["洒落る", "1,1,1", "洒落[しゃれ]る"],  # しゃれ is two morae (ゃ is not a mora)
        ["話した", "2,1,1", "話[はな]した"],  # dictionary form 話す, inflected in the text
        ["きゃく", "0.5,0.5,1", "きゃく"],  # no dictionary token: き and ゃ share one mora
        ["承る", "5,1", "しょう"],  # the book's ruby うけたまわ, not the dictionary's しょう
    ]


def test_spoken_word_is_highlighted_and_finished_before_the_next(page) -> None:
    # Audio plays from 10 s (offset = seconds - 10). Sampled every 100 ms: one word is lit,
    # it fills up between the reader's polls (250 ms) and is shown finished when the next
    # one lights up; the next word's furigana shows just before it is spoken.
    result = page.run(
        OPEN
        + """
        const samples = [];
        for (let k = 0; k < 32; k++) { await __tick(100); samples.push({time: audioAt(), words: words()}); }
        return samples;
        """,
        before=LN_PAIRED,
    )
    samples = result.value
    lit = [[i for i, word in enumerate(sample["words"]) if word["current"]] for sample in samples]
    assert all(len(indices) == 1 for indices in lit)
    order = [indices[0] for indices in lit]
    assert order == sorted(order) and order[0] == 0 and order[-1] == 1
    for sample, index in zip(samples, order):
        # The lit word is the one being spoken (the reader's clock may lead by < 0.1 s).
        offset = sample["time"] - 10
        assert (-0.1 <= offset < 2.1) if index == 0 else (1.9 <= offset < 5)
    first = [_percent(sample["words"][0]["progress"]) for sample, index in zip(samples, order) if index == 0]
    assert len(first) > 15 and all(b > a for a, b in zip(first, first[1:]))  # fills up between polls too
    switch = order.index(1)
    before, after = samples[switch - 1]["words"], samples[switch]["words"]
    assert before[1]["preview"] and not before[1]["current"]
    assert after[0]["progress"] == "100%" and after[0]["done"] and not after[0]["current"]
    assert not after[1]["preview"]
    assert not any(word["finishing"] for sample in samples for word in sample["words"])


def test_highlight_between_polls_follows_the_anchor_path(page) -> None:
    # mpv stops answering after two polls: the reader's own clock moves the highlight
    # along the alignment's path, not at a constant speed.
    result = page.run(
        OPEN
        + SLOW_START_JS
        + """
        await __tick(400); AUDIO.hold = true;
        const out = [];
        for (const until of [10.6, 11.5]) {
          while (audioAt() < until) await __tick(20);
          out.push({time: audioAt(), progress: progressOf(0)});
        }
        return out;
        """,
        before=LN_PAIRED,
    )
    for sample in result.value:
        assert abs(_percent(sample["progress"]) - _offset_at(sample["time"]) / 2 * 100) < 10, sample


def test_background_window_still_follows_each_poll(page) -> None:
    # Behind other windows no animation frames run; every poll still draws the place the
    # audio is at on the alignment's path.
    result = page.run(
        OPEN
        + SLOW_START_JS
        + """
        await __setHidden(true);
        const out = [];
        for (let k = 0; k < 12; k++) { await __tick(250); out.push({time: audioAt(), lit: current(), progress: current() >= 0 ? progressOf(current()) : ''}); }
        return out;
        """,
        before=LN_PAIRED,
    )
    samples = [sample for sample in result.value if sample["lit"] == 0 and sample["time"] > 10.6]
    assert len(samples) >= 3
    for sample in samples:
        assert abs(_percent(sample["progress"]) - _offset_at(sample["time"]) / 2 * 100) < 20, sample
    assert result.value[-1]["lit"] == 1


def test_word_switch_is_immediate_and_small_clock_steps_back_are_ignored(page) -> None:
    result = page.run(
        OPEN
        + """
        AUDIO.playing = false;
        const out = {};
        render(11.5, 1.5); out.speaking = words().slice(0, 2);
        // The next sample is 0.3 characters earlier (clock jitter): the highlight stays.
        render(11.55, 1.2); out.jitter = words().slice(0, 2);
        out.rebased = trace('clock_rebase_backward').map(row => [row.source_offset, row.rendered_offset]);
        // The offset reaches the next word: it lights up on this very frame.
        render(12.05, 2.05); out.next = words().slice(0, 2);
        // A correction of more than two characters is shown.
        render(12.1, 0); out.corrected = words().slice(0, 2);
        return out;
        """,
        before=LN_PAIRED,
    )
    out = result.value
    assert [w["current"] for w in out["speaking"]] == [True, False] and out["speaking"][0][
        "progress"
    ] == "75.0%"
    assert out["jitter"] == out["speaking"]
    assert out["rebased"] == [[1.2, 1.5]]
    assert (
        out["next"][0]["current"] is False and out["next"][0]["progress"] == "100%" and out["next"][0]["done"]
    )
    assert out["next"][1]["current"] is True and out["next"][1]["progress"] == "1.7%"
    assert out["corrected"][0]["current"] is True and out["corrected"][0]["progress"] == "0.0%"


def test_cmd_shift_l_exports_the_sync_trace(page) -> None:
    # ⌘⇧L in the reader with linked audio saves the listen-together trace and says where.
    # The key is matched by its position (code KeyL), so a Russian layout ("д") works too.
    for lang in ("en", "ru"):
        result = page.run(
            OPEN
            + """
            await __tick(2500);
            await __key('д', {code: 'KeyL', metaKey: true, shiftKey: true}, 'body');
            await __tick(10);
            return {toasts: __toasts(), saved: PudgeI18n.t('ui.traceSaved', {path: '/logs/pudge-ln-audio-sync.json'})};
            """,
            lang=lang,
            before=LN_PAIRED,
        )
        assert result.value["saved"] in result.value["toasts"]
        [[payload]] = result.calls_of("light_novel_export_audio_sync_trace")
        assert payload["book"] == {"id": 9, "title": "Novel", "audiobook_id": 31}
        changes = [
            row["next_word"]
            for row in payload["events"]
            if row["event"] == "word_change" and row.get("next_word")
        ]
        assert [(word["surface"], word["weights"], word["reading"]) for word in changes][:2] == [
            ("承る", [5, 1], "うけたまわる"),
            ("言い方", [1, 1, 2], "言[い]い方[かた]"),
        ]
        assert not result.called("export_runtime_debug_bundle")


def test_sync_trace_keeps_the_newest_2400_events(page) -> None:
    result = page.run(
        OPEN
        + """
        AUDIO.playing = false;
        for (let k = 0; k < 2600; k++) lnPairedTrace('seek', {position: k}, {sourceOffset: k, renderedOffset: k});
        await exportLnPairedTrace();
        """,
        before=LN_PAIRED,
    )
    [[payload]] = result.calls_of("light_novel_export_audio_sync_trace")
    seeks = [row["source_offset"] for row in payload["events"] if row["event"] == "seek"]
    assert len(payload["events"]) == 2400
    assert seeks == list(range(2600 - len(seeks), 2600))


def test_backend_writes_the_trace_to_the_debug_log_folder_and_reveals_it(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    logs = tmp_path / "debug-logs"
    logs.mkdir()
    monkeypatch.setattr(web_app, "debug_log_dir", lambda **_kwargs: logs)
    opened: list[list[str]] = []
    monkeypatch.setattr(
        web_app.subprocess, "Popen", lambda command, *args, **kwargs: opened.append(list(command))
    )
    events = [{"event": "frame", "n": n} for n in range(3500)] + ["not an event"]
    result = api.light_novel_export_audio_sync_trace({"schema": 1, "book": {"id": 9}, "events": events})
    output = Path(result["path"])
    assert output.parent == logs and output.name.startswith("pudge-ln-audio-sync-")
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert [row["n"] for row in saved["trace"]["events"]] == list(range(501, 3500))  # the newest 3000 entries
    assert saved["trace"]["book"] == {"id": 9}
    assert result["event_count"] == 2999
    assert opened == [["open", "-R", str(output)]]
