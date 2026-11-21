"""Japanese + English subtitle files are a fallback, like Japanese + Chinese ones.

Dororo E03 (sync benchmark): "[SFSM] Dororo - 03.jp-en.ass" won over the same group's
"03.jp.ass" with the same timing - its English lines also match the English timing
track (activity 0.999 against 0.713) - and the viewer read English. Grand Blue E08
picked "...ja-en.ass" over "...ja.srt" the same way. Synthetic files only.
"""

from __future__ import annotations

from pathlib import Path

from pudge.models import JimakuFile, SubtitleCandidate, VideoIdentity
from pudge.providers.jimaku import JimakuClient, materialize_jimaku_files
from pudge.subtitle_formats import bilingual_english_profile, subtitle_filename_language_profile
from pudge.syncing import _rank_embedded_reference_candidates


def _ts(value: float) -> str:
    total = round(value * 1000)
    return f"{total // 3600000:02d}:{total // 60000 % 60:02d}:{total // 1000 % 60:02d},{total % 1000:03d}"


def _bilingual_cues(count: int = 60) -> list[tuple[float, float, str]]:
    cues = []
    for index in range(count):
        start = 10.0 + index * 4.0
        cues.append((start, start + 2.5, f"これは{index}番目の台詞です"))
        cues.append((start + 0.1, start + 2.4, f"This is line number {index}"))
    return cues


def test_filename_marks_japanese_english_files() -> None:
    for name in (
        "[SFSM] Dororo - 03.jp-en.ass",
        "[Group] Show - 08 [720p].ja-en.ass",
        "Show - 01.en.ja.srt",
        "Show - 01.jpn+eng.vtt",
    ):
        assert subtitle_filename_language_profile(name)["purity"] == "mixed_japanese_english", name
    for name in ("[SFSM] Dororo - 03.jp.ass", "Show.S01E01.WEBRip.Netflix.ja[cc].srt", "Hello-en.ass"):
        assert subtitle_filename_language_profile(name)["purity"] != "mixed_japanese_english", name
    assert subtitle_filename_language_profile("Show [CHS, JPN].ass")["purity"] == "mixed_japanese_chinese"


def test_dual_audio_release_names_are_not_japanese_english_subtitles() -> None:
    # Review: "JPN, ENG" / "JPN.ENG" / "en-dub" name the audio of a dual-audio release.
    for name in (
        "[Group] Title - 01 [1080p][Dual-Audio][JPN, ENG].ja.srt",
        "Title.S01E01.1080p.BluRay.JPN.ENG.DDP2.0.x264-GRP.ja.ass",
        "Title.01.ja.en-dub.ass",
        "Show - 01 [ENG, JPN].ass",
    ):
        profile = subtitle_filename_language_profile(name)
        assert profile["purity"] == "japanese_only", name
        assert profile["english_tag_unconfirmed"] is True, name


def test_parallel_english_lines_are_detected_but_romaji_songs_are_not() -> None:
    assert bilingual_english_profile(_bilingual_cues())["suspected_bilingual_english"] is True
    japanese = [(10.0 + i * 4.0, 12.5 + i * 4.0, f"これは{i}番目の台詞です") for i in range(200)]
    songs = [(900.0 + i * 3.0, 902.5 + i * 3.0, "kimi no koe ga kikoeru") for i in range(30)]
    assert bilingual_english_profile(japanese + songs)["suspected_bilingual_english"] is False


def test_a_short_with_romaji_lyrics_beside_its_lines_is_not_bilingual(tmp_path: Path) -> None:
    # Review: a 40-line Japanese short whose song has 20 romaji lines under the kanji ones.
    events = ["[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for i in range(40):
        events.append(f"Dialogue: 0,0:00:{i:02d}.00,0:00:{i:02d}.90,Default,,0,0,0,,これは{i}番目の台詞です")
    for i in range(20):
        a, b = f"0:01:{i * 2:02d}.00", f"0:01:{i * 2 + 1:02d}.80"
        events.append(f"Dialogue: 0,{a},{b},OP-Kanji,,0,0,0,,{{\\k20}}君の声が{{\\k30}}聞こえる")
        events.append(f"Dialogue: 0,{a},{b},Default,,0,0,0,,{{\\k20}}kimi no {{\\k30}}koe ga kikoeru the sky")
    subtitle = tmp_path / "Short - 01.ass"
    subtitle.write_text("\n".join(events) + "\n", encoding="utf-8")
    from pudge.subtitle_formats import subtitle_bilingual_english_profile

    profile = subtitle_bilingual_english_profile(subtitle)
    assert profile["suspected_bilingual_english"] is False, profile
    assert profile["english_cues"] == 0, profile


def test_jimaku_listing_ranks_the_japanese_only_file_of_the_group_first() -> None:
    client = object.__new__(JimakuClient)
    files = [
        JimakuFile(
            url="https://example.invalid/a", name="[SFSM] Dororo - 03.jp-en.ass", size=1, last_modified=""
        ),
        JimakuFile(
            url="https://example.invalid/b", name="[SFSM] Dororo - 03.jp.ass", size=1, last_modified=""
        ),
    ]
    ranked = client.rank_files(files, VideoIdentity(title="Dororo", episode=3), Path("Dororo - 03.mkv"))
    assert ranked[0].name.endswith("03.jp.ass")
    assert ranked[1].details["language_purity"] == "mixed_japanese_english"
    assert ranked[0].score > ranked[1].score + 50


class _Client:
    def __init__(self, path: Path) -> None:
        self.path = path

    def download(self, item: JimakuFile, cache_dir: Path) -> Path:
        return self.path


def test_unmarked_file_with_parallel_english_lines_is_a_fallback(tmp_path: Path) -> None:
    subtitle = tmp_path / "Show - 03.srt"
    subtitle.write_text(
        "\n\n".join(f"{i}\n{_ts(a)} --> {_ts(b)}\n{t}" for i, (a, b, t) in enumerate(_bilingual_cues(), 1))
        + "\n",
        encoding="utf-8",
    )
    item = JimakuFile(
        url="https://example.invalid/c", name=subtitle.name, size=1, last_modified="", score=100.0
    )
    [candidate] = materialize_jimaku_files(
        _Client(subtitle),  # type: ignore[arg-type]
        item,
        VideoIdentity(title="Show", episode=3),
        tmp_path / "Show - 03.mkv",
        tmp_path / "cache",
    )
    assert candidate.details["language_purity"] == "mixed_japanese_english"
    assert candidate.details["bilingual_english"] is True
    assert candidate.score < 100.0


def test_japanese_only_file_beats_the_bilingual_one_on_the_english_clock(tmp_path: Path) -> None:
    def item(name: str, purity: str, activity: float):
        candidate = SubtitleCandidate(
            path=tmp_path / name, source="jimaku", score=138.0, name=name, details={"language_purity": purity}
        )
        return (
            (1.0, activity, 0.0, 138.0),
            candidate,
            tmp_path / f"aligned-{name}.srt",
            {"timeline_alignment_reliable": True},
            {"available": True, "weighted": activity},
            {"reason": "ok"},
        )

    bilingual = item("[SFSM] Dororo - 03.jp-en.ass", "mixed_japanese_english", 0.9992)
    japanese = item("[SFSM] Dororo - 03.jp.ass", "japanese_only", 0.7129)
    ranked, meta = _rank_embedded_reference_candidates([bilingual, japanese], prefer_srt=True)
    assert ranked[0][1].name == "[SFSM] Dororo - 03.jp.ass"
    assert meta["selected_language_purity"] == "japanese_only"


def test_a_japanese_only_file_with_a_dual_audio_name_stays_japanese_only(tmp_path: Path) -> None:
    subtitle = tmp_path / "[Group] Show - 03 [1080p][Dual-Audio][JPN, ENG].ja.srt"
    lines = [(10.0 + i * 4.0, 12.5 + i * 4.0, f"これは{i}番目の台詞です") for i in range(60)]
    subtitle.write_text(
        "\n\n".join(f"{i}\n{_ts(a)} --> {_ts(b)}\n{t}" for i, (a, b, t) in enumerate(lines, 1)) + "\n",
        encoding="utf-8",
    )
    item = JimakuFile(
        url="https://example.invalid/d", name=subtitle.name, size=1, last_modified="", score=100.0
    )
    [candidate] = materialize_jimaku_files(
        _Client(subtitle),  # type: ignore[arg-type]
        item,
        VideoIdentity(title="Show", episode=3),
        tmp_path / "Show - 03.mkv",
        tmp_path / "cache",
    )
    assert candidate.details["language_purity"] == "japanese_only"
    assert candidate.details["bilingual_english"] is False
    assert candidate.score == 100.0
