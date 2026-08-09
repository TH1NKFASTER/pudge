from anime_mpv.language import japanese_text_metrics


def test_japanese_text():
    text = "これは日本語の字幕です。今日はとてもいい天気ですね。" * 20
    assert japanese_text_metrics(text)["detected"] is True


def test_chinese_text_rejected_without_kana():
    text = "这是中文字幕内容，今天的天气很好。" * 20
    assert japanese_text_metrics(text)["detected"] is False


def test_english_text():
    text = "This is an English subtitle line." * 30
    assert japanese_text_metrics(text)["detected"] is False
