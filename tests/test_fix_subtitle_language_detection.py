"""Subtitle language and encoding detection (review SYNC-4, SYNC-5).

1. A Chinese subtitle with Japanese OP/ED lyrics passed as Japanese: 10 kana and
   a kana ratio of 0.015 were enough. Most of its CJK lines carry no kana, so it
   is now rejected; Japanese and Japanese + Chinese bilingual files still pass.
2. Chinese file-name tags SC, TC, JPSC, [CHS&JPN], 简日双语, GB and BIG5 were
   "unknown" (``&`` / ``+`` were no separators).
3. EUC-JP decoded to Hangul mojibake (charset_normalizer chose EUC-KR) and
   ISO-2022-JP kept its escape sequences; a ``.ja`` name skipped the content
   check, so such files were "verified Japanese".
4. ``__MACOSX/._Show - 05.ja.srt`` (AppleDouble metadata of a zip made on a Mac)
   was a subtitle candidate.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from pudge.language import (
    decode_subtitle_bytes,
    is_japanese_subtitle,
    is_macos_metadata_path,
    japanese_text_metrics,
)
from pudge.subtitle_formats import subtitle_filename_language_profile

JA_LINES = [
    "おはよう、今日はどこへ行くの？",
    "まだ決めてないけど、海に行きたいな。",
    "それはいい考えだね。",
    "じゃあ、駅で待ち合わせしよう。",
]
ZH_LINES = [
    "你好，今天我们去哪里？",
    "我不知道，你决定吧。",
    "这个问题很难回答。",
    "他们还没有来吗？",
    "我们快点走吧，时间不多了。",
    "为什么你总是这样说话？",
]
LYRICS = ["青いバケツを空に投げた", "三時のおやつは月の上", "ラジオの猫が歌いだす", "まわれまわれ紙の風車"]


def _ts(value: float) -> str:
    hours, rest = divmod(value, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}".replace(".", ",")


def _srt(lines: list[str]) -> str:
    blocks = []
    for index, text in enumerate(lines, start=1):
        start = index * 3.0
        blocks.append(f"{index}\n{_ts(start)} --> {_ts(start + 2.5)}\n{text}")
    return "\n\n".join(blocks) + "\n"


def _chinese_with_japanese_op() -> str:
    # Distinct lines, as in a real episode: 30 lyric lines and 300 lines of dialogue.
    return _srt([f"{LYRICS[k % 4]} {k}" for k in range(30)] + [f"{ZH_LINES[k % 6]} {k}" for k in range(300)])


def test_chinese_subtitle_with_japanese_opening_lyrics_is_not_japanese(tmp_path: Path) -> None:
    text = _chinese_with_japanese_op()
    metrics = japanese_text_metrics(text)
    assert metrics["kana_line_ratio"] < 0.2
    assert metrics["detected"] is False
    plain = tmp_path / "[Group] Show - 05 [1080p].srt"
    plain.write_text(text, encoding="utf-8")
    marked = tmp_path / "Show - 05.ja.srt"
    marked.write_text(text, encoding="utf-8")
    assert is_japanese_subtitle(plain) is False
    # The ".ja" name no longer replaces reading the file.
    assert is_japanese_subtitle(marked) is False


def test_japanese_and_bilingual_japanese_chinese_files_still_pass(tmp_path: Path) -> None:
    japanese = tmp_path / "Show - 05.srt"
    japanese.write_text(
        _srt([f"{JA_LINES[k % 4]} {k}" for k in range(200)] + ["（銃声）", "先生！"] * 10), encoding="utf-8"
    )
    assert is_japanese_subtitle(japanese) is True
    # One Japanese and one Chinese line per cue (简日双语) is filtered later, not rejected.
    bilingual = tmp_path / "Show - 05 bilingual.srt"
    bilingual.write_text(
        _srt([f"{JA_LINES[k % 4]} {k}\n{ZH_LINES[k % 6]} {k}" for k in range(200)]),
        encoding="utf-8",
    )
    assert is_japanese_subtitle(bilingual) is True
    # A tiny but genuine ".ja" file still counts (a marker lowers the amount needed).
    short = tmp_path / "PV.ja.srt"
    short.write_text(_srt(["こんにちは"]), encoding="utf-8")
    assert is_japanese_subtitle(short) is True


def test_chinese_filename_tags_are_recognised() -> None:
    expected = {
        "Show - 05 [CHS&JPN].ass": "mixed_japanese_chinese",
        "Show - 05 [JPN+CHT].ass": "mixed_japanese_chinese",
        "Show - 05 [JPSC].ass": "mixed_japanese_chinese",
        "Show.05.简日双语.ass": "mixed_japanese_chinese",
        "[Sub][Show][05][简日双语][1080P].ass": "mixed_japanese_chinese",
        "Show - 05 [SC].ass": "chinese_only",
        "Show - 05 [TC][1080p].ass": "chinese_only",
        "Show - 05 [SC&JPN].ass": "mixed_japanese_chinese",
        "Show - 05 [GB_JP].ass": "mixed_japanese_chinese",
        "Show - 05 [BIG5].ass": "chinese_only",
        # SC/TC/GB on their own are no language: a group name, a size, a word.
        "[SC-Raws] Show - 01.ja.ass": "japanese_only",
        "[SC] Show - 01.ja.ass": "japanese_only",
        "Show - 01 [GB].srt": "unknown",
        "Show.05.sc.ass": "unknown",
        "Show TC - 05.srt": "unknown",
        "Show.05.繁体.srt": "chinese_only",
        # Unchanged:
        "Show.05.chs.ass": "chinese_only",
        "[Studio] Sayonara Lara [01][CHS, JPN].ass": "mixed_japanese_chinese",
        "[KitaujiSub] Sayonara Lara [01][JPN].ass": "japanese_only",
        "Show - 05.ja.srt": "japanese_only",
        "Show - 05 [4GB].srt": "unknown",
        "Gosick 05.srt": "unknown",
    }
    actual = {name: subtitle_filename_language_profile(name)["purity"] for name in expected}
    assert actual == expected


SAMPLE = (
    "1\n00:00:01,000 --> 00:00:03,000\nおはよう、今日はどこへ行くの？まだ決めてないけど、海に行きたいな。\n\n"
)


def test_euc_jp_and_iso_2022_jp_decode_to_japanese(tmp_path: Path) -> None:
    for encoding in ("euc_jp", "iso2022_jp"):
        data = (SAMPLE * 20).encode(encoding)
        assert decode_subtitle_bytes(data) == SAMPLE * 20, encoding
        # A read cut inside the last character keeps the encoding.
        assert decode_subtitle_bytes(data[:-3]).startswith(SAMPLE), encoding
        path = tmp_path / f"Show - 05.{encoding}.ja.srt"
        path.write_bytes(data)
        assert is_japanese_subtitle(path) is True
        unmarked = tmp_path / f"Show - 05 {encoding}.srt"
        unmarked.write_bytes(data)
        assert is_japanese_subtitle(unmarked) is True
    # Shift-JIS / CP932 / UTF-8 unchanged.
    for encoding in ("shift_jis", "cp932", "utf-8"):
        assert decode_subtitle_bytes((SAMPLE * 20).encode(encoding)) == SAMPLE * 20
    assert decode_subtitle_bytes("①髙ｱ漢字です".encode("cp932")) == "①髙ｱ漢字です"
    assert decode_subtitle_bytes("ｱｲｳ半角カナです".encode("shift_jis")) == "ｱｲｳ半角カナです"


def test_marked_file_without_japanese_text_is_rejected(tmp_path: Path) -> None:
    korean = tmp_path / "Show - 05.ja.srt"
    korean.write_text(_srt(["안녕하세요, 오늘 어디 가요?"] * 50), encoding="utf-8")
    assert is_japanese_subtitle(korean) is False
    # Bitmap subtitles cannot be read: the marker still decides.
    sup = tmp_path / "Show - 05.ja.sup"
    sup.write_bytes(b"PG\x00\x00")
    assert is_japanese_subtitle(sup) is True


def test_macos_metadata_members_are_skipped(tmp_path: Path) -> None:
    assert is_macos_metadata_path("__MACOSX/._Show - 05.ja.srt")
    assert is_macos_metadata_path("__MACOSX/Show/Show - 05.ja.srt")
    assert is_macos_metadata_path("Show/._Show - 05.ja.srt")
    assert not is_macos_metadata_path("Show/Show - 05.ja.srt")
    apple_double = tmp_path / "._Show - 05.ja.srt"
    apple_double.write_text(_srt(JA_LINES * 10), encoding="utf-8")
    assert is_japanese_subtitle(apple_double) is False


def _zip_with_metadata(path: Path) -> None:
    real = _srt(JA_LINES * 20)
    with zipfile.ZipFile(path, "w") as zf:
        # Sorted first by name, and the same score as the real file before the fix.
        zf.writestr(
            "__MACOSX/._Show - 05.ja.srt", b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        " + real.encode()
        )
        zf.writestr("._Show - 05.ja.srt", b"\x00\x05\x16\x07" + real.encode())
        zf.writestr("Show - 05.ja.srt", real.encode("utf-8"))


def test_local_zip_ignores_macos_metadata(tmp_path: Path) -> None:
    from pudge.local_search import _extract_best_from_zip
    from pudge.models import VideoIdentity

    archive = tmp_path / "subs.zip"
    _zip_with_metadata(archive)
    video = tmp_path / "Show - 05.mkv"
    video.write_bytes(b"v")
    identity = VideoIdentity(title="Show", episode=5)
    candidate = _extract_best_from_zip(archive, identity, video, tmp_path / "cache", True)
    assert candidate is not None
    assert candidate.name == "Show - 05.ja.srt"


def test_jimaku_zip_ignores_macos_metadata(tmp_path: Path) -> None:
    from pudge.models import VideoIdentity
    from pudge.providers.jimaku import _extract_archive_all

    archive = tmp_path / "subs.zip"
    _zip_with_metadata(archive)
    video = tmp_path / "Show - 05.mkv"
    video.write_bytes(b"v")
    out = tmp_path / "out"
    out.mkdir()
    rows = _extract_archive_all(archive, VideoIdentity(title="Show", episode=5), video, out, True)
    assert [name for _path, name, _score in rows] == ["Show - 05.ja.srt"]
    assert not any("._" in path.name for path in out.iterdir())


ASS_HEAD = """[Script Info]
Title: 某字幕组 日文字幕
ScriptType: v4.00+

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: 正文,方正准圆,60,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1
Style: 看板,方正准圆,50,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,8,10,10,10,1
"""
EVENTS = "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"


def _dialogue(start: float, text: str, style: str = "正文", layer: int = 0) -> str:
    def ts(value: float) -> str:
        return f"{int(value // 3600)}:{int(value % 3600 // 60):02d}:{value % 60:05.2f}"

    return f"Dialogue: {layer},{ts(start)},{ts(start + 2)},{style},,0,0,0,,{text}"


def test_japanese_ass_with_layered_kanji_signs_passes(tmp_path: Path) -> None:
    rows = [_dialogue(10 + 3 * k, f"{JA_LINES[k % 4]}{k}") for k in range(60)]
    signs = ["東京駅", "第一話", "警視庁", "立入禁止", "生徒会室"]
    # Each sign drawn in many layers (outline, shadow, glow ...): kanji-only lines.
    rows += [_dialogue(5 + k, f"{{\\pos(960,100)}}{signs[k % 5]}", "看板", layer=k % 12) for k in range(300)]
    path = tmp_path / "Show - 05.ass"
    path.write_text(ASS_HEAD + EVENTS + "\n".join(rows) + "\n", encoding="utf-8")
    assert is_japanese_subtitle(path) is True
    # A Chinese ASS with Japanese OP lyrics is still not Japanese.
    chinese = [_dialogue(1 + 3 * k, f"{LYRICS[k % 4]}{k}") for k in range(30)]
    chinese += [_dialogue(100 + 3 * k, f"{ZH_LINES[k % 6]}{k}") for k in range(300)]
    other = tmp_path / "Show - 05 zh.ass"
    other.write_text(ASS_HEAD + EVENTS + "\n".join(chinese) + "\n", encoding="utf-8")
    assert japanese_text_metrics(other.read_text(encoding="utf-8"))["kana_line_ratio"] < 0.15
    assert is_japanese_subtitle(other) is False


def test_marked_ass_with_embedded_fonts_is_read_past_the_fonts(tmp_path: Path) -> None:
    rows = [_dialogue(10 + 3 * k, f"{JA_LINES[k % 4]}{k}") for k in range(40)]
    # Aegisub writes [Fonts] before [Events]; 3 MB of uuencoded font data.
    fonts = (
        "\n[Fonts]\nfontname: font_0.ttf\n" + ("M" + "A!#$%&()*+,-./0123456789:;<=>?@" * 2 + "\n") * 45_000
    )
    path = tmp_path / "Show - 05.ja.ass"
    path.write_text(ASS_HEAD + fonts + EVENTS + "\n".join(rows) + "\n", encoding="utf-8")
    assert path.stat().st_size > 2_000_000
    assert is_japanese_subtitle(path) is True
