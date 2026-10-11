"""Settings → Sound in mpv: turning spatial audio off plays surround audio as stereo (behaviour).

macOS renders 5.1/7.1 audio spatially in AirPods even with Spatial Audio off in Control
Center; mpv's own stereo downmix (--audio-channels=stereo) is what turned it off."""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge import web_app
from pudge.config import AppConfig, load_config, write_config
from pudge.web_app import WebAppApi


def _launch(tmp_path: Path, monkeypatch, *, stereo: bool, extra: list[str] | None = None) -> list[str]:
    from pudge.cli import build_parser, process_video
    from pudge.pipeline_cache import save_final_pipeline_result
    import pudge.first_experience as first_experience

    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.anilist.enabled = False
    cfg.llm.enabled = False
    cfg.playback.skip_segments_enabled = False
    cfg.playback.stereo_downmix = stereo
    cfg.tools.mpv_extra_args = list(extra or [])
    video = tmp_path / "Show - 02 DDP5.1.mkv"
    subtitle = tmp_path / "cached.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8")
    save_final_pipeline_result(
        video, cfg, subtitle=subtitle, subtitle_id=None, dependency=subtitle, source="external"
    )
    monkeypatch.setattr("pudge.cli.find_embedded_japanese_subtitles", lambda *_a, **_k: [])
    monkeypatch.setattr(
        first_experience,
        "mpv_study_script_plan",
        lambda *a, **k: {"exclusive": False, "selected": "", "scripts": []},
    )
    launched: list[list[str]] = []
    monkeypatch.setattr("pudge.cli.run_mpv", lambda command, **_k: launched.append(command) or 0)
    args = build_parser().parse_args([str(video)])
    args.config = tmp_path / "config.toml"
    assert process_video(video, args, cfg, None) == 0
    return launched[0]


def test_mpv_downmixes_to_stereo_only_when_spatial_audio_is_turned_off(tmp_path, monkeypatch):
    assert not any(
        arg.startswith("--audio-channels") for arg in _launch(tmp_path / "a", monkeypatch, stereo=False)
    )
    command = _launch(tmp_path / "b", monkeypatch, stereo=True)
    assert command.count("--audio-channels=stereo") == 1
    assert command.index("--audio-channels=stereo") < command.index("--")  # an mpv option, not a file
    # A channel layout the user set in mpv_extra_args wins.
    command = _launch(tmp_path / "c", monkeypatch, stereo=True, extra=["--audio-channels=5.1"])
    assert [arg for arg in command if arg.startswith("--audio-channels")] == ["--audio-channels=5.1"]


def test_the_setting_is_saved_in_config(tmp_path, monkeypatch):
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.paths.download_dirs = []
    cfg.paths.subtitle_dirs = []
    cfg.anilist.enabled = False
    cfg.nyaa.enabled = False
    cfg.qbittorrent.enabled = False
    cfg.aria2.enabled = False
    write_config(cfg, cfg.config_path)
    assert load_config(cfg.config_path).playback.stereo_downmix is False  # off unless asked for
    monkeypatch.setattr(web_app, "request_folder_access", lambda paths: {})
    api = WebAppApi(cfg.config_path)
    try:
        assert api.save_settings({"stereo_downmix": True})["settings"]["stereo_downmix"] is True
        assert load_config(cfg.config_path).playback.stereo_downmix is True
        api.save_settings({"rewind_seconds": 10})  # other saves keep it
        assert load_config(cfg.config_path).playback.stereo_downmix is True
    finally:
        api.close()


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_settings_offer_turning_off_spatial_audio(page, lang):
    result = page.run(
        """
        setPage('settings'); await __tick(20);
        await __click('[role="tab"][data-settings-category="advanced"]'); await __tick(10);
        const out = {shown: __visible('#settingsMpvAudio'), heading: __text('#settingsMpvAudio h3'),
                     expectedHeading: PudgeI18n.t('ui.mpvSound'), toggle: __text('#settingsMpvAudio label'),
                     expectedToggle: PudgeI18n.t('ui.turnOffSpatialAudio'), hint: __text('#settingsMpvAudio p.subtle'),
                     expectedHint: PudgeI18n.t('ui.turnOffSpatialAudioHint'), checked: $('s_stereo_downmix').checked};
        await __input('#s_stereo_downmix', true);
        await __click('#saveSettings'); await __tick(10);
        return out;
        """,
        lang=lang,
        state={"settings": {"stereo_downmix": False}},
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    out = result.value
    assert out["shown"] is True and out["checked"] is False
    assert out["heading"] == out["expectedHeading"] and out["toggle"] == out["expectedToggle"]
    assert out["hint"] == out["expectedHint"] and "stereo" in out["hint"].replace("стерео", "stereo")
    [[values]] = result.calls_of("save_settings")
    assert values["stereo_downmix"] is True
