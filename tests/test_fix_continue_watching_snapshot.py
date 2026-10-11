"""Home keeps a watched-into episode in Continue watching on every state read (behaviour).

Playback heartbeats do not rebuild the cached Home snapshot. While an episode played, the
cached snapshot still showed it under New episodes ready and a fresh one under Continue
watching, so the window flicked between the two. Entering or leaving Continue watching now
invalidates the snapshot; ordinary heartbeats still do not."""

from __future__ import annotations

from pathlib import Path

from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.playback_save import save_playback
from test_v0630_resume_priority import make_api


def _sections(state: dict) -> list[str]:
    return [key for key, cards in state["home"].items() if any(card.get("media_id") == 195 for card in cards)]


def test_cached_and_fresh_home_agree_while_an_episode_plays(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    try:
        api.manager.db.upsert_anime(
            LibraryAnime(
                media_id=195,
                title="Airing Show",
                status="CURRENT",
                progress=1,
                episodes=24,
                media_status="RELEASING",
                next_airing_episode=3,
                next_airing_at=2_000_000_000,
            )
        )
        video = tmp_path / "Airing Show - 02.mkv"
        video.write_bytes(b"video")
        subtitle = tmp_path / "Airing Show - 02.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n台詞\n", encoding="utf-8")
        api.manager.db.upsert_episode(
            LibraryEpisode(
                media_id=195,
                title="Airing Show",
                episode=2,
                video_path=video,
                state="ready",
                subtitle_path=subtitle,
            )
        )
        db = api.manager.db

        def heartbeat(position: float, sequence: int) -> None:
            save_playback(db, video, position=position, duration=1440, session="s", sequence=sequence)

        assert _sections(api.get_state_fast()) == ["new_ready"]  # cached before playback
        heartbeat(10, 1)  # not resumable yet
        assert _sections(api.get_state_fast()) == ["new_ready"]
        heartbeat(120, 2)
        assert _sections(api.get_state_fast()) == ["continue_watching"] == _sections(api.get_state())
        version = db.get_state("ui_state_version", "")
        heartbeat(150, 3)  # still resumable: no rebuild for an ordinary heartbeat
        assert db.get_state("ui_state_version", "") == version
        heartbeat(1400, 4)  # the end of the episode: nothing left to resume
        assert _sections(api.get_state_fast()) == ["new_ready"] == _sections(api.get_state())
    finally:
        api.close()


def test_torrent_switch_shows_no_refreshing_rates_line(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            window.fetch = async () => new Response(JSON.stringify({ok: true, enabled: true, desired_enabled: true}));
            await __tick(50);
            const button = $('torrentToggleButton');
            return {text: __text('#torrentToggleButton'), lines: button.querySelectorAll('small').length,
                    on: t('ui.torrentsOn')};
            """,
            lang=lang,
            state={"settings": {"torrents_enabled": True}},
        )
        out = result.value
        # Torrents on, rates not known yet: just the state, no "Refreshing rates…" line.
        assert out["text"] == out["on"] and out["lines"] == 0
