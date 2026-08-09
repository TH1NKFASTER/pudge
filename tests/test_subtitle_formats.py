from anime_mpv.subtitle_formats import format_preference_bonus


def test_srt_is_preferred_over_ass():
    assert format_preference_bonus("episode.srt", True) > format_preference_bonus("episode.ass", True)


def test_format_preference_can_be_disabled():
    assert format_preference_bonus("episode.srt", False) == 0
    assert format_preference_bonus("episode.ass", False) == 0


def test_ass_is_converted_to_plain_srt_without_styles(tmp_path):
    from anime_mpv.subtitle_formats import convert_to_plain_srt

    source = tmp_path / "styled.ass"
    source.write_text(
        """[Script Info]\nTitle: test\n\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\nStyle: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,0,2,10,10,10,1\n\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:01.00,0:00:03.00,Default,,0,0,0,,{\\an8}{\\b1}日本語\\Nテスト\n""",
        encoding="utf-8",
    )

    output, result = convert_to_plain_srt(
        source,
        tmp_path / "cache",
        ffmpeg_path="/definitely/missing/ffmpeg",
    )

    assert result["converted"] is True
    assert output.suffix == ".srt"
    text = output.read_text(encoding="utf-8")
    assert "日本語" in text
    assert "テスト" in text
    assert "{\\an8}" not in text
    assert "<" not in text


def test_plain_subtitle_text_removes_isolated_hangul_prefix_before_japanese():
    from anime_mpv.subtitle_formats import plain_subtitle_text

    assert plain_subtitle_text("모巨大生物が出現しました") == "巨大生物が出現しました"
    assert plain_subtitle_text("한국어 자막") == "한국어 자막"


def test_plain_subtitle_text_removes_angle_wrappers_and_speaker_labels():
    from anime_mpv.subtitle_formats import plain_subtitle_text

    source = "＜（黒絵）\nずっと　自分が嫌いだった＞"
    assert plain_subtitle_text(source) == "ずっと　自分が嫌いだった"
    assert plain_subtitle_text("（南）赤石さん") == "赤石さん"


def test_plain_subtitle_text_preserves_standalone_stage_direction():
    from anime_mpv.subtitle_formats import plain_subtitle_text

    assert plain_subtitle_text("（ドアが開く）") == "（ドアが開く）"


def test_plain_subtitle_text_removes_embedded_furigana():
    from anime_mpv.subtitle_formats import plain_subtitle_text

    assert plain_subtitle_text("漢字（かんじ）です") == "漢字です"
    assert plain_subtitle_text("｜明日《あした》") == "明日"
    assert plain_subtitle_text(
        "<ruby><rb>東京</rb><rt>とうきょう</rt></ruby>"
    ) == "東京"


def test_clean_srt_for_playback_creates_clean_copy(tmp_path):
    from anime_mpv.subtitle_formats import clean_srt_for_playback

    source = tmp_path / "episode.srt"
    source.write_text(
        "1\n00:00:34,301 --> 00:00:37,804\n＜（黒絵）\nずっと　自分が嫌いだった＞\n\n"
        "2\n00:00:48,315 --> 00:00:50,317\n（南）赤石さん\n",
        encoding="utf-8",
    )

    output, result = clean_srt_for_playback(source, tmp_path / "cache")

    assert result["cleaned"] is True
    assert output != source
    assert source.read_text(encoding="utf-8").startswith("1\n")
    text = output.read_text(encoding="utf-8")
    assert "＜" not in text
    assert "＞" not in text
    assert "（黒絵）" not in text
    assert "（南）" not in text
    assert "ずっと　自分が嫌いだった" in text
    assert "赤石さん" in text


def test_write_srt_separates_exactly_touching_cues(tmp_path):
    from anime_mpv.subtitle_formats import write_srt

    output = tmp_path / "touching.srt"
    write_srt(
        [
            (37.804, 42.476, "醜くて\nひねくれてて　全然かわいくない"),
            (42.476, 47.080, "恋なんて恐怖でしかない\nそう思ってたのに"),
        ],
        output,
    )

    text = output.read_text(encoding="utf-8")
    assert "00:00:37,804 --> 00:00:42,376" in text
    assert "00:00:42,476 --> 00:00:47,080" in text


def test_write_srt_removes_real_overlaps_for_mpv(tmp_path):
    from anime_mpv.subtitle_formats import write_srt

    output = tmp_path / "overlap.srt"
    write_srt(
        [
            (10.000, 15.000, "Первая реплика"),
            (14.500, 18.000, "Параллельная реплика"),
        ],
        output,
    )

    text = output.read_text(encoding="utf-8")
    assert "00:00:10,000 --> 00:00:14,400" in text
    assert "00:00:14,500 --> 00:00:18,000" in text


def test_clean_srt_for_playback_invalidates_old_cached_timing(tmp_path):
    from anime_mpv.subtitle_formats import clean_srt_for_playback

    source = tmp_path / "episode.srt"
    source.write_text(
        "1\n00:00:37,804 --> 00:00:42,476\nПервая\n\n"
        "2\n00:00:42,476 --> 00:00:47,080\nВторая\n",
        encoding="utf-8",
    )

    output, result = clean_srt_for_playback(source, tmp_path / "cache")

    assert result["cleaned"] is True
    cleaned = output.read_text(encoding="utf-8")
    assert "00:00:37,804 --> 00:00:42,376" in cleaned
    assert "00:00:42,476 --> 00:00:47,080" in cleaned


def test_write_srt_never_starts_exactly_at_zero(tmp_path):
    from anime_mpv.subtitle_formats import write_srt

    output = tmp_path / "zero-start.srt"
    write_srt([(0.0, 1.0, "最初")], output)

    text = output.read_text(encoding="utf-8")
    assert "00:00:00,100 --> 00:00:01,100" in text


def test_write_srt_never_emits_sub_300ms_cue_after_collision(tmp_path):
    from anime_mpv.subtitle_formats import parse_srt, write_srt

    output = tmp_path / "short-after-overlap.srt"
    write_srt(
        [
            (1.000, 1.350, "第一"),
            (1.100, 1.147, "第二"),
        ],
        output,
        preserve_order=True,
    )

    cues = parse_srt(output)
    assert len(cues) == 2
    assert all(end - start >= 0.299 for start, end, _text in cues)
    assert cues[0][0] < cues[1][0]
