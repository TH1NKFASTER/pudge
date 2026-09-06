from pathlib import Path

from pudge.subtitle_formats import parse_srt, write_srt


def test_write_srt_collision_shift_never_reorders_sorted_starts(tmp_path: Path) -> None:
    output = tmp_path / "collision.srt"
    write_srt(
        [
            (1.624, 2.024, "一"),
            (2.023, 2.423, "二"),
            (2.024, 2.424, "三"),
        ],
        output,
    )
    starts = [start for start, _end, _text in parse_srt(output)]
    assert starts == sorted(starts)
