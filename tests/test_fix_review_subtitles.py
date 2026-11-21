"""Subtitle, file-name and mpv fixes from the code review.

1. Shift-JIS / CP932 / UTF-16 subtitles: UTF-16 was tried before Shift-JIS even
   without a BOM, so an even-sized Shift-JIS file decoded as garbage and the
   Jimaku file was thrown away; the subtitle readers read UTF-8 only (Shift-JIS
   became mojibake, UTF-16 gave no cues). Now UTF-16 needs a BOM or its NUL-byte
   pattern, then UTF-8, then Shift-JIS/CP932, and every reader uses that decoder.
2. An SRT with a missing blank line merged two cues and showed the second
   timing line on screen. Every timing line now starts its own cue.
3. "Show - 05.mkv" took episode 05.5's "Show - 05.5.ja.ass" as its subtitle.
4. "Show - CM 01", "Menu 02", "Omake 01", "Trailer 01", "Recap 01" became regular
   episodes 1/2 (anitopy does not report those types), and "Show - 2024" became
   episode 2024. They are now extras like an NCOP ("SP 01" a special), and a
   lone year is no episode.
5. A scene missing from the video (the clock drops) dragged the clock switch
   through minutes of dialogue, or back to the opening gap, and the shifted
   result was accepted as reliable. The switch now moves only a few cues;
   otherwise the subtitle-only result is rejected.
6. One very long cue (an ASS watermark 0:00-9:59:59) made subtitle-only
   alignment fail a correct shift and run 20x slower. It is now ignored for
   alignment and kept in the output.
7. Ctrl+T in mpv failed for any line starting with "-" (argparse: "expected one
   argument"). The text now goes as one "--option=value" argument.
8. Counting the episode on close: the tracking file was deleted as mpv exited,
   before the detached helper read it. It now stays; old ones are removed at the
   next launch.
"""

from __future__ import annotations

import os
import random
import time
from pathlib import Path

import pytest

from pudge.anilist_tracking import TrackingPayload, create_tracking_file
from pudge.extra_files import extra_file_kind
from pudge.filename import parse_anime_filename
from pudge.language import decode_subtitle_bytes, is_japanese_subtitle
from pudge.library import sidecar_subtitle
from pudge.manager_models import LibraryAnime
from pudge.release_parser import parse_release_name
from pudge.subtitle_formats import (
    convert_to_plain_srt,
    parse_srt,
    subtitle_has_genuine_overlaps,
    write_srt,
)
from pudge.subtitles.timeline_alignment import _stabilize_decreasing_boundaries, align_subtitle_timelines
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario

SRT = (
    "1\r\n00:00:01,000 --> 00:00:03,000\r\nこんにちは、元気ですか？\r\n\r\n"
    "2\r\n00:00:04,000 --> 00:00:06,000\r\nありがとうございます。お願いします\r\n\r\n"
    "3\r\n00:00:07,000 --> 00:00:09,000\r\nそれはちょっと難しいですね\r\n"
)
CUES = [
    (1.0, 3.0, "こんにちは、元気ですか？"),
    (4.0, 6.0, "ありがとうございます。お願いします"),
    (7.0, 9.0, "それはちょっと難しいですね"),
]


# --- 1. encodings ----------------------------------------------------------


def test_even_sized_shift_jis_is_not_read_as_utf16() -> None:
    data = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは、元気ですか？\n\n".encode("shift_jis")
    assert len(data) % 2 == 0
    assert decode_subtitle_bytes(data).endswith("こんにちは、元気ですか？\n\n")
    assert decode_subtitle_bytes(SRT.encode("utf-8")) == SRT
    assert decode_subtitle_bytes(SRT.encode("utf-16")) == SRT  # with a BOM
    assert decode_subtitle_bytes(SRT.encode("utf-16-le")) == SRT  # no BOM: NUL pattern
    assert decode_subtitle_bytes(SRT.encode("utf-16-be")) == SRT
    # A read cut inside the last UTF-8 character is still UTF-8.
    assert decode_subtitle_bytes(SRT.encode("utf-8")[:-2] + "ね".encode("utf-8")[:2]) == SRT[:-2]


@pytest.mark.parametrize("encoding", ["shift_jis", "cp932", "utf-16", "utf-16-le"])
def test_srt_in_legacy_or_utf16_encoding_parses(tmp_path: Path, encoding: str) -> None:
    path = tmp_path / f"Show - 01.{encoding}.srt"
    path.write_bytes(SRT.encode(encoding))
    assert is_japanese_subtitle(path)
    assert parse_srt(path) == CUES


def test_shift_jis_ass_converts_through_python_and_ffmpeg(tmp_path: Path) -> None:
    ass = (
        "[Script Info]\nScriptType: v4.00+\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "".join(
            f"Dialogue: 0,0:00:0{int(start)}.00,0:00:0{int(end)}.00,Default,,0,0,0,,{text}\n"
            for start, end, text in CUES
        )
    )
    source = tmp_path / "Show - 01.ass"
    source.write_bytes(ass.encode("shift_jis"))
    assert not subtitle_has_genuine_overlaps(source)
    for ffmpeg in ("ffmpeg", "/nonexistent/ffmpeg"):
        output, result = convert_to_plain_srt(source, tmp_path / ffmpeg.replace("/", "_"), ffmpeg_path=ffmpeg)
        assert result["converted"], result
        assert parse_srt(output) == CUES
        assert output.read_bytes().decode("utf-8")  # Pudge writes UTF-8


# --- 2. missing blank line -------------------------------------------------


def test_missing_blank_line_between_cues_gives_two_cues(tmp_path: Path) -> None:
    path = tmp_path / "ep.srt"
    path.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n2\n00:00:03,000 --> 00:00:04,000\nさようなら\n",
        encoding="utf-8",
    )
    assert parse_srt(path) == [(1.0, 2.0, "こんにちは"), (3.0, 4.0, "さようなら")]


# --- 3. sidecar of episode 05.5 ------------------------------------------


def test_episode_05_does_not_take_the_subtitle_of_05_5(tmp_path: Path) -> None:
    video = tmp_path / "Show - 05.mkv"
    video.write_bytes(b"video")
    (tmp_path / "Show - 05.5.mkv").write_bytes(b"video")
    (tmp_path / "Show - 05.5.ja.ass").write_text(SRT, encoding="utf-8")
    assert sidecar_subtitle(video) is None
    for name in ("Show - 05.ja.srt", "Show - 05.jpn.ass", "Show - 05.ja.forced.srt"):
        own = tmp_path / name
        own.write_text(SRT, encoding="utf-8")
        assert sidecar_subtitle(video) == own.resolve()
        own.unlink()


# --- 4. numbered bonus files and years -------------------------------------


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("[G] Show - CM 01 (1080p).mkv", "CM"),
        ("[G] Show - CM01 [BD 1080p].mkv", "CM"),
        ("[G] Show - Menu 02 [BD 1080p].mkv", "Menu"),
        ("[G] Show - Omake 01 [BD 1080p].mkv", "Omake"),
        ("[G] Show - Trailer 01 [BD 1080p].mkv", "Trailer"),
        ("[G] Show - Recap 01 [BD 1080p].mkv", "Recap"),
        ("[G] Show - Picture Drama 02 [BD].mkv", "Picture Drama"),
        ("[G] Show S2 - Teaser 01 [BD].mkv", "Teaser"),
        ("[G] Show [Menu 01][1080p].mkv", "Menu"),
    ],
)
def test_numbered_bonus_file_of_a_tv_batch_is_an_extra(name: str, kind: str) -> None:
    anime = LibraryAnime(media_id=1, title="Show", status="CURRENT", episodes=12, format="TV")
    parsed = parse_release_name(name)
    # Extras like an NCOP: Library only, no subtitle search, Action required in a batch.
    assert parsed.episode is None and parsed.extra and not parsed.special
    assert parse_anime_filename("Show/" + name).episode is None
    assert extra_file_kind(anime, name) == kind


def test_named_sp_is_a_special_not_episode_one() -> None:
    parsed = parse_release_name("[G] Show - SP 01 [BD].mkv")  # anitopy only types "SP01"
    assert parsed.episode is None and parsed.special and not parsed.extra


@pytest.mark.parametrize(
    ("name", "episode"),
    [
        ("Menu no Hanashi - 03 [1080p].mkv", 3),  # the word in the title, before " - "
        ("[G] Recap Girls - 04 [1080p].mkv", 4),
        ("[G] Show - 07 - Recap [1080p].mkv", 7),  # episode 7's own title
        ("[G] Show - 13 (Recap) [1080p].mkv", 13),
        ("[G] One Piece - 1071 [1080p].mkv", 1071),
        ("[G] Show (2024) - 05 [1080p].mkv", 5),
    ],
)
def test_titles_with_those_words_and_long_runners_stay_episodes(name: str, episode: int) -> None:
    anime = LibraryAnime(media_id=1, title="Show", status="CURRENT", episodes=1200, format="TV")
    assert parse_anime_filename(name).episode == episode
    assert parse_release_name(name).episode == episode
    assert extra_file_kind(anime, name) == ""


def test_a_year_after_the_dash_is_not_an_episode() -> None:
    identity = parse_anime_filename("Show - 2024 [1080p].mkv")
    assert identity.episode is None and identity.year == 2024


# --- 5. scene missing from the video ---------------------------------------


def _dense(start: float, end: float) -> list[tuple[float, float, str]]:
    cues, t = [], start
    while t < end:
        cues.append((t, t + 3.2, f"台詞{len(cues)}"))
        t += 3.8  # 0.6 s gaps: nothing absorbs a 26 s jump
    return cues


def _segments(left: float, right: float) -> list[dict[str, object]]:
    base = {"support": 8, "mean_score": 3.0, "mean_coverage": 0.9, "windows": [], "kind": "stable"}
    return [
        {**base, "first_center": 0.0, "last_center": 350.0, "offset_seconds": left},
        {**base, "first_center": 350.0, "last_center": 999.0, "offset_seconds": right},
    ]


def test_clock_drop_without_a_gap_is_not_dragged_through_the_dialogue() -> None:
    cues = _dense(5.0, 700.0)
    segments, boundaries, diagnostics = _stabilize_decreasing_boundaries(
        cues, _segments(-7.0, -33.0), [350.0]
    )
    assert boundaries == [350.0]  # not ~700 s with every later cue on the old clock
    assert diagnostics[-1]["reason"] == "decreasing_boundary_move_limit"
    assert not [row for row in diagnostics if row.get("applied")]


def test_clock_drop_is_not_moved_back_to_the_opening_gap() -> None:
    cues = _dense(5.0, 100.0) + _dense(190.0, 700.0)  # a 90 s opening gap
    truth = [start + (-20.0 if start < 350.0 else -59.0) for start, _end, _text in cues]
    _segments_out, boundaries, diagnostics = _stabilize_decreasing_boundaries(
        cues, _segments(-20.0, -59.0), [350.0], sorted(truth)
    )
    assert boundaries == [350.0]  # not ~145 s (40 cues 39 s early)
    assert not any(
        row.get("reason") == "decreasing_boundary_anchored_to_wide_source_gap" for row in diagnostics
    )


def _cut_scene_episode(seed: int) -> tuple[list, list]:
    """The review's fuzz case: a 26 s scene with dialogue is missing at ~985 s."""
    rnd = random.Random(seed)
    duration = rnd.choice([1420, 1440, 1500])
    t = rnd.uniform(5, 30)
    base: list[tuple[float, float, str]] = []
    op_at = rnd.uniform(60, 300)
    while t < duration:
        if op_at <= t < op_at + 90:
            t = op_at + 90 + rnd.uniform(0, 5)
            continue
        length = rnd.uniform(0.8, 4.5)
        base.append((t, t + length, f"あ{len(base)}"))
        t += length + rnd.choice([rnd.uniform(0.1, 1.5), rnd.uniform(0.5, 4), rnd.uniform(2, 10)])
    offset = rnd.uniform(-60, 60)
    edits = []
    for _ in range(rnd.choice([0, 0, 1, 1, 2])):
        center = rnd.uniform(0.1 * duration, 0.9 * duration)
        edits.append((center, rnd.choice([1, 1, -1]) * rnd.uniform(2, 40)))
    edits.sort()

    def mapped(x: float) -> float | None:
        value = offset
        for center, length in edits:
            if length > 0 and x >= center:
                value += length
            if length < 0:
                if center <= x < center - length:
                    return None
                if x >= center - length:
                    value += length
        return x + value

    reference = []
    for start, end, _text in base:
        target = mapped(start)
        if target is None or target < 0 or rnd.random() < 0.1:
            continue
        jitter = rnd.gauss(0, 0.15)
        if rnd.random() < 0.15:
            mid = (start + end) / 2
            reference.append((target + jitter, target + (mid - start) + jitter, "x"))
            reference.append((target + (mid - start) + 0.05 + jitter, target + (end - start) + jitter, "x"))
        else:
            reference.append((target + jitter, target + (end - start) * rnd.uniform(0.8, 1.2) + jitter, "x"))
    for _ in range(rnd.randint(0, 25)):
        x = rnd.uniform(0, duration)
        reference.append((x, x + rnd.uniform(0.5, 3), "sign"))
    return base, reference


def test_cut_scene_shift_is_not_accepted_as_reliable(tmp_path: Path) -> None:
    source, reference = _cut_scene_episode(49)
    write_srt(source, tmp_path / "src.srt")
    write_srt(reference, tmp_path / "ref.srt")
    _output, info = align_subtitle_timelines(tmp_path / "src.srt", tmp_path / "ref.srt", tmp_path / "cache")
    # Before: accepted with the switch at 1419.75 s and 34 % of the cues 26 s late.
    assert not info.get("accepted") and not info.get("timeline_alignment_reliable")


# --- 6. a watermark cue ----------------------------------------------------


def test_watermark_cue_does_not_block_or_slow_subtitle_only_alignment(tmp_path: Path) -> None:
    rnd = random.Random(1)
    cues, t = [], 5.0
    while t < 1420 and len(cues) < 350:
        length = rnd.uniform(0.8, 4.0)
        cues.append((t, t + length, f"あいう{len(cues)}"))
        t += length + rnd.uniform(0.2, 6.0)
    write_srt([(s + 3.0, e + 3.0, "x") for s, e, _ in cues], tmp_path / "ref.srt")
    write_srt([*cues, (0.2, 35999.0, "字幕組")], tmp_path / "src.srt")
    started = time.monotonic()
    output, info = align_subtitle_timelines(tmp_path / "src.srt", tmp_path / "ref.srt", tmp_path / "cache")
    assert time.monotonic() - started < 4.0  # was ~7 s (windows ran to 10 h)
    assert info["reason"] == "applied" and info["offset_seconds"] == pytest.approx(3.0)
    written = parse_srt(output)
    assert (3.2, 35999.0 + 3.0, "字幕組") in written  # still in the output
    assert len(written) == len(cues) + 1


# --- 7. Ctrl+T on a line starting with "-" ---------------------------------


TRANSLATE_DASH = r"""
local mp = require 'mp'
local real = mp.command_native_async
mp.command_native_async = function(command, callback)
    if command.name == 'subprocess' then
        print('CALL\t' .. (table.concat(command.args, '\31'):gsub('\n', '\\n')))
    end
    return real(command, callback)
end
sim.fire('file-loaded')
sim.set('sub-text', '-ちょっと待って'); sim.play(0.5)
sim.set('sub-text', '-はい')
sim.press('Ctrl+t')
"""


@needs_lua
def test_mpv_translation_of_a_line_starting_with_a_dash(tmp_path: Path) -> None:
    from pudge.cli import build_parser

    out = run_mpv_scenario(tmp_path, TRANSLATE_DASH, scripts=[ANILIST], env={"PUDGE_PYTHON": "python3"})
    calls = [line.split("\t", 1)[1].split("\x1f") for line in out.splitlines() if line.startswith("CALL\t")]
    [args] = [call for call in calls if "--subtitle-translate" in call]
    assert "--subtitle-study-text=-はい" in args
    parsed = build_parser().parse_args(args[3:])  # argparse used to exit here
    assert parsed.subtitle_study_text == "-はい"
    assert "-ちょっと待って" in parsed.subtitle_study_context


# --- 8. tracking file outlives mpv ----------------------------------------


def _payload(video: str = "/media/Show - 05.mkv") -> TrackingPayload:
    return TrackingPayload(
        video=video, title="Show", media_id=77, episode=5, total_episodes=12, threshold=0.85, mapping_key="k"
    )


def test_old_tracking_files_are_removed_at_the_next_launch(tmp_path: Path) -> None:
    stale = create_tracking_file(tmp_path, _payload())
    recent = create_tracking_file(tmp_path, _payload("/media/Show - 06.mkv"))
    old = time.time() - 2 * 24 * 3600
    os.utime(stale, (old, old))
    current = create_tracking_file(tmp_path, _payload("/media/Show - 07.mkv"))
    assert not stale.exists() and recent.exists() and current.exists()


def test_tracking_file_is_kept_after_mpv_exits(tmp_path: Path, monkeypatch) -> None:
    from pudge.cli import build_parser, process_video
    from pudge.config import AppConfig
    from pudge.pipeline_cache import save_final_pipeline_result

    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.anilist.enabled = True
    cfg.anilist.access_token = "token"
    cfg.llm.enabled = False
    video = tmp_path / "Show - 05.mkv"
    subtitle = tmp_path / "cached.srt"
    video.write_bytes(b"video")
    subtitle.write_text(SRT, encoding="utf-8")
    save_final_pipeline_result(
        video, cfg, subtitle=subtitle, subtitle_id=None, dependency=subtitle, source="external"
    )
    monkeypatch.setattr("pudge.cli.find_embedded_japanese_subtitles", lambda *_a, **_k: [])
    launched: list[dict[str, str]] = []

    def fake_run(_command, **kwargs):
        launched.append(dict(kwargs.get("env_overrides") or {}))
        return 0

    monkeypatch.setattr("pudge.cli.run_mpv", fake_run)
    args = build_parser().parse_args(
        [str(video), "--fast-play", "--media-id", "77", "--media-episodes", "12", "--episode-hint", "5"]
    )
    args.config = tmp_path / "config.toml"
    assert process_video(video, args, cfg, None) == 0
    tracking = Path(launched[0]["PUDGE_ANILIST_TRACKING_FILE"])
    # The detached "count on close" helper reads it after mpv has exited.
    assert tracking.is_file()
