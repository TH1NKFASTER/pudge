from pudge.reading_audio_alignment import _enforce_min_char_duration, _relocate_misplaced_pause_holds


def _rows(points):
    return [{"time": t, "offset": o} for t, o in points]


# Real book 178 ch3 (fixes4 trace): …と返してあげました。桐生くんにも言い返す
REAL = _rows([
    (3740.16, 16063.999), (3740.699, 16063.999), (3740.7, 16064), (3740.78, 16065),
    (3740.96, 16072.999), (3741.239, 16072.999), (3741.24, 16073), (3745.2, 16083),
    (3745.3, 16084),
])
REGIONS = [{"start": s, "end": e} for s, e in [
    (3737.14, 3740.16), (3740.7, 3740.96), (3741.24, 3742.32), (3743.8, 3745.6),
]]


def test_early_sentence_hold_moves_to_real_pause():
    fixed, moved = _relocate_misplaced_pause_holds(REAL, REGIONS)
    assert moved == 1
    by = [(r["time"], r["offset"]) for r in fixed]
    assert (3742.32, 16072.999) in by and (3743.799, 16072.999) in by and (3743.8, 16073) in by
    # outer anchors and earlier 」 hold untouched
    assert by[:4] == [(r["time"], r["offset"]) for r in REAL[:4]]
    assert by[-2:] == [(3745.2, 16083), (3745.3, 16084)]
    assert [t for t, _ in by] == sorted(t for t, _ in by)
    _, floor = _enforce_min_char_duration(fixed, REGIONS)
    assert floor == 0  # no burst left for the char floor


def test_hold_not_moved_when_left_segment_is_plausible():
    anchors = _rows([(0.0, 0), (1.0, 7.999), (1.3, 7.999), (1.301, 8), (5.0, 18)])
    regions = [{"start": 0, "end": 1.0}, {"start": 1.3, "end": 2.5}, {"start": 3.5, "end": 5}]
    fixed, moved = _relocate_misplaced_pause_holds(anchors, regions)
    assert moved == 0 and fixed == anchors


def test_hold_not_moved_without_clearly_longer_later_gap():
    anchors = _rows([(0.0, 0), (0.2, 7.999), (0.5, 7.999), (0.501, 8), (3.0, 18)])
    regions = [{"start": 0, "end": 0.2}, {"start": 0.5, "end": 1.5}, {"start": 1.8, "end": 3}]
    fixed, moved = _relocate_misplaced_pause_holds(anchors, regions)
    assert moved == 0 and fixed == anchors


def test_hold_not_moved_if_next_sentence_would_burst():
    anchors = _rows([(0.0, 0), (0.2, 7.999), (0.5, 7.999), (0.501, 8), (3.0, 18)])
    regions = [{"start": 0, "end": 0.2}, {"start": 0.5, "end": 1.0}, {"start": 2.5, "end": 3}]
    fixed, moved = _relocate_misplaced_pause_holds(anchors, regions)
    assert moved == 0 and fixed == anchors  # 10 chars would get only 0.5 s
