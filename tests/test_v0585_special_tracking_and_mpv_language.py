import argparse
import json
from pathlib import Path

from pudge import cli, i18n
from pudge.config import AppConfig
from pudge.models import AniListAnime, VideoIdentity
from tests.i18n_support import en, ru
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario


def _anime(*, episodes: int | None, media_format: str) -> AniListAnime:
    return AniListAnime(
        id=211711,
        titles=["Boku no Hero Academia: I am a hero too"],
        synonyms=[],
        season_year=2026,
        episodes=episodes,
        format=media_format,
    )


def test_one_entry_special_without_filename_episode_gets_progress_one() -> None:
    identity = VideoIdentity(title="Boku no Hero Academia: I am a hero too")
    assert cli._anilist_episode(identity, _anime(episodes=1, media_format="SPECIAL")) == 1


def test_multi_entry_special_without_filename_episode_remains_ambiguous() -> None:
    identity = VideoIdentity(title="Multi-part Special")
    assert cli._anilist_episode(identity, _anime(episodes=3, media_format="SPECIAL")) is None


def _mpv_environment(tmp_path: Path, monkeypatch, language: str) -> dict[str, str]:
    """The environment cli.process_video gives mpv (and the tracker script) for an interface language."""
    from pudge.cli import build_parser, process_video
    from pudge.pipeline_cache import save_final_pipeline_result

    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.database_path = tmp_path / "library.sqlite"
    cfg.anilist.enabled = False
    cfg.llm.enabled = False
    cfg.ui.language = language
    video, subtitle = tmp_path / "Show - 01.mkv", tmp_path / "show.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n字幕\n", encoding="utf-8")
    save_final_pipeline_result(
        video, cfg, subtitle=subtitle, subtitle_id=None, dependency=subtitle, source="external"
    )
    launched: list[dict[str, str]] = []
    monkeypatch.setattr("pudge.cli.run_mpv", lambda _command, **kw: launched.append(kw["env_overrides"]) or 0)
    assert process_video(video, build_parser().parse_args([str(video)]), cfg, None) == 0
    return launched[0]


# The OSD messages of the tracker: a manually counted file says how to count it when it
# loads; Ctrl+A without a tracking file starts counting and says tracking is unavailable.
OSD = """
for _, row in ipairs(sim.log) do if row.kind == 'osd' then print('OSD:' .. row.value) end end
"""


def _tracker_osd(tmp_path: Path, locale_file: str, *, tracking: bool) -> list[str]:
    env = {
        "PUDGE_LOCALE_FILE": locale_file,
        "PUDGE_ANILIST_AUTO_UPDATE": "0",
        "PUDGE_ANILIST_TRACKING_FILE": str(tmp_path / "tracking.json") if tracking else "",
    }
    scenario = "sim.fire('file-loaded')\n" if tracking else "sim.press('Ctrl+a')\n"
    out = run_mpv_scenario(tmp_path, scenario + OSD, scripts=[ANILIST], env=env)
    return [line[4:] for line in out.splitlines() if line.startswith("OSD:")]


@needs_lua
def test_mpv_tracker_uses_ui_language_and_defaults_to_english(tmp_path: Path, monkeypatch) -> None:
    # Audit 68Б: the tracker reads the catalog of the interface language (the locale file
    # cli.py passes); without it, English fallbacks.
    for language, text in (("en", en), ("ru", ru)):
        locale_file = _mpv_environment(tmp_path / language, monkeypatch, language)["PUDGE_LOCALE_FILE"]
        assert Path(locale_file) == i18n.catalog_path(language)
        assert _tracker_osd(tmp_path / language, locale_file, tracking=True) == [
            text("osd.anilistManualMode")
        ]
        assert _tracker_osd(tmp_path / language, locale_file, tracking=False) == [
            text("osd.anilistCountingEntry"),
            text("osd.anilistTrackingUnavailable"),
        ]
    for missing in ("", str(tmp_path / "missing.js")):
        assert _tracker_osd(tmp_path, missing, tracking=True) == ["AniList: manual mode — Ctrl+A to count"]
        assert _tracker_osd(tmp_path, missing, tracking=False) == [
            "AniList: counting this entry…",
            "AniList: tracking is unavailable for this file",
        ]
    # The script's English fallbacks are the catalog's English texts.
    assert en("osd.anilistManualMode") == "AniList: manual mode — Ctrl+A to count"
    assert en("osd.anilistTrackingUnavailable") == "AniList: tracking is unavailable for this file"
    assert ru("osd.anilistTrackingUnavailable") == "AniList: трекер недоступен для этого файла"


def test_python_anilist_osd_follows_ui_language(tmp_path: Path, capsys) -> None:
    tracking = tmp_path / "tracking.json"
    tracking.write_text(
        json.dumps(
            {
                "video": "ep.mkv",
                "title": "Show",
                "media_id": 1,
                "episode": 1,
                "threshold": 0.8,
                "mapping_key": "show",
            }
        ),
        encoding="utf-8",
    )
    args = argparse.Namespace(tracking_file=tracking, anilist_action="mark", anilist_id=None)
    config = AppConfig()
    config.anilist.access_token = ""
    config.ui.language = "en"
    assert cli._run_anilist_action(args, config) == 1
    assert capsys.readouterr().out.strip() == "OSD:" + en("osd.anilistTokenMissing")
    config.ui.language = "ru"
    assert cli._run_anilist_action(args, config) == 1
    assert capsys.readouterr().out.strip() == "OSD:" + ru("osd.anilistTokenMissing")
    assert en("osd.anilistTokenMissing") != ru("osd.anilistTokenMissing")
