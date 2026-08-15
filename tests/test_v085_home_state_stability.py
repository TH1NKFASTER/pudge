from pathlib import Path

from pudge.web_app import WebAppApi


def test_local_episode_shadows_stale_single_download(tmp_path: Path) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    card = {"local": {"episode": 6, "video_path": str(video)}}

    assert WebAppApi._download_shadowed_by_local(
        card, {"episode": 6, "is_batch": False}
    )
    assert not WebAppApi._download_shadowed_by_local(
        card, {"episode": 7, "is_batch": False}
    )
    assert not WebAppApi._download_shadowed_by_local(
        card, {"episode": 6, "is_batch": True}
    )


def test_bitmap_without_action_job_stays_waiting() -> None:
    assert (
        WebAppApi._pending_local_action(
            {"state": "waiting_text_subtitles"},
            None,
            ocr_enabled=False,
        )
        is None
    )


def test_existing_ocr_action_survives_transient_job_state() -> None:
    action = WebAppApi._pending_local_action(
        {"state": "waiting_text_subtitles"},
        {
            "state": "pending",
            "action_code": "enable_subtitle_ocr",
            "last_error": "Only image subtitles are available",
        },
        ocr_enabled=False,
    )

    assert action == (
        "enable_subtitle_ocr",
        "Only image subtitles are available",
    )
