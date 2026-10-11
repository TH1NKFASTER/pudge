from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi


def make_api(tmp_path: Path) -> WebAppApi:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.config_path = tmp_path / "config.toml"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_mid_episode_resume_card_wins_over_ready_section(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    anime = LibraryAnime(
        media_id=630,
        title="Resume Priority",
        status="CURRENT",
        progress=0,
        episodes=12,
        media_status="RELEASING",
        next_airing_episode=2,
        next_airing_at=2_000_000_000,
    )
    api.manager.db.upsert_anime(anime)
    video = tmp_path / "Resume Priority - 01.mkv"
    video.write_bytes(b"video")
    api.manager.db.upsert_episode(
        LibraryEpisode(
            media_id=anime.media_id,
            title=anime.title,
            episode=1,
            video_path=video,
            state="ready",
        )
    )
    api.manager.db.record_playback(video, 434.643, 1440.085)

    home = api.get_state()["home"]

    assert [item["media_id"] for item in home["continue_watching"]] == [anime.media_id]
    assert home["new_ready"] == []
    assert home["completed_ready"] == []
    assert home["continue_watching"][0]["resume_start"] == 424.643


SHOW = {
    "media_id": 7,
    "title": "Show",
    "episodes": 12,
    "next_episode": 3,
    "progress": 2,
    "site_url": "https://anilist.co/anime/7",
    "ready_episodes": [3],
    "local": {"episode": 3, "state": "ready", "video_path": "/v/s3.mkv", "subtitle_path": "/v/s3.ja.srt"},
}
# mpv runs until window.__exited; the user stopped mid-episode (not counted as watched) and
# the backend's Home then lists the episode under Continue watching.
PLAYER = r"""
const base = __api.get_state;
__api.get_state = __api.get_state_fast = async () => {
  const state = await base();
  if (window.__exited) state.home = {...state.home, new_ready: [],
    continue_watching: [{...state.home.new_ready[0], episode: 3, video_path: '/v/s3.mkv', resume_start: 424.6, progress_percent: 30}]};
  return state;
};
__api.play = async () => ({ok: true, status: 'starting'});
__api.play_status = async () => (window.__exited ? {status: 'exited', watched: false, episode: 3} : {status: 'running', episode: 3});
"""


def test_player_exit_refreshes_mid_episode_state(page) -> None:
    result = page.run(
        r"""
        const sections = () => [...document.querySelectorAll('#currentContent [data-home-section]')].map(n => n.dataset.homeSection);
        await __click('#currentContent [data-play-path="/v/s3.mkv"]'); await __tick(1000);
        const playing = sections();
        window.__exited = true;
        await __tick(3000);
        return {playing, after: sections(), card: __text('#currentContent [data-home-section="section.continueWatching"]'),
                resume: t('label.continueEpisode', {episode: 3, time: formatClock(424.6)})};
        """,
        state={"home": {"new_ready": [SHOW]}},
        before=PLAYER,
    )
    out = result.value
    # Home moves the episode to Continue watching as soon as mpv closes.
    assert out["playing"] == ["section.newReady"] and out["after"] == ["section.continueWatching"]
    assert out["resume"] in out["card"]
