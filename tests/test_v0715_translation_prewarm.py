from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.mpv_study import SubtitleStudyApi


def test_translation_prewarm_starts_at_resume_and_uses_exact_history(
    tmp_path: Path, monkeypatch
) -> None:
    subtitle = tmp_path / "episode.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n最初\n\n"
        "2\n00:00:05,000 --> 00:00:06,000\n次\n\n"
        "3\n00:00:09,000 --> 00:00:10,000\n最後\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, str, int | None]] = []

    class Service:
        config = SimpleNamespace(llm=SimpleNamespace(enabled=True))

        def translate_selection(
            self, text: str, context: str, *, media_id: int | None
        ) -> dict[str, object]:
            calls.append((text, context, media_id))
            return {"translation": text, "provider": "local_llm", "cached": False}

    api = SubtitleStudyApi.__new__(SubtitleStudyApi)
    api.service = Service()
    api.media_id = 77
    monkeypatch.setattr("pudge.mpv_study.time.sleep", lambda _seconds: None)

    result = api.prewarm_file(subtitle, start_seconds=5.5)

    assert [text for text, _context, _media_id in calls] == ["次", "最後", "最初"]
    assert calls[0][1] == "Previous Japanese subtitles:\n最初"
    assert calls[1][1] == "Previous Japanese subtitles:\n最初\n次"
    assert calls[2][1] == ""
    assert all(media_id == 77 for _text, _context, media_id in calls)
    assert result["translated"] == 3


def test_translation_prewarm_is_disabled_without_local_llm(tmp_path: Path) -> None:
    subtitle = tmp_path / "episode.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8"
    )
    api = SubtitleStudyApi.__new__(SubtitleStudyApi)
    api.service = SimpleNamespace(
        config=SimpleNamespace(llm=SimpleNamespace(enabled=False))
    )
    api.media_id = None

    assert api.prewarm_file(subtitle)["reason"] == "local_llm_disabled"
