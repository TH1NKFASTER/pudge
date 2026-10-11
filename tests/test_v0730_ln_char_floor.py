from css_support import css_rules
from pudge.reading_audio_alignment import _enforce_min_char_duration
from t71g20_support import READER


def _rows(points):
    return [{"time": t, "offset": o} for t, o in points]


def test_burst_after_slow_bridge_borrows_from_earlier_speech():
    # おばあちゃん|から: 7 chars over 1.6 s, then ゃ and ん at 40 ms each.
    anchors = _rows([(3070.88, 13043), (3072.48, 13050), (3072.52, 13051), (3072.56, 13052)])
    fixed, changed = _enforce_min_char_duration(anchors, [{"start": 3070.0, "end": 3074.0}])
    times = [row["time"] for row in fixed]
    assert changed
    assert times[0] == 3070.88 and times[-1] == 3072.56  # outer points untouched
    gaps = [b - a for a, b in zip(times, times[1:])]
    assert min(gaps[1:]) >= 0.08 - 1e-6  # every spoken char gets >= 80 ms of audio
    assert times == sorted(times)


def test_pause_hold_is_a_barrier():
    anchors = _rows([(10.0, 100), (12.0, 101), (12.02, 102)])
    fixed, changed = _enforce_min_char_duration(anchors, [{"start": 11.9, "end": 13.0}])
    assert changed == 0 and fixed == anchors


def test_wall_clock_bridge_is_never_rewritten():
    anchors = [
        {"time": 1.0, "offset": 0},
        {"time": 2.0, "offset": 5},
        {"time": 2.02, "offset": 6, "wall_clock_from_previous": True},
    ]
    fixed, changed = _enforce_min_char_duration(anchors, [{"start": 0, "end": 5}])
    assert changed == 0 and fixed == anchors


def test_reader_flashes_the_word_it_just_left_as_finished(page) -> None:
    # Listen together: the word the voice leaves is shown fully read and flashes "done" briefly.
    result = page.run(
        READER
        + r"""
        renderLnPairedPosition(at(0.5), 5, {speechActive: true});
        renderLnPairedPosition(at(1.2), 12, {speechActive: true});
        const left = words();
        await __tick(150);
        return {left, later: words()};
        """
    )
    first, second = result.value["left"][:2]
    assert (first["current"], first["done"], first["progress"]) == (False, True, "100%")
    assert (second["current"], second["done"], second["progress"]) == (True, False, "20.0%")
    assert result.value["later"][0] == {**first, "done": False}
    # The flash is drawn: the class has a background of its own.
    assert css_rules("index.html").get(".ln-paired-word-done", "background")
