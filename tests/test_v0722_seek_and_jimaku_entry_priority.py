from __future__ import annotations

from pathlib import Path

from pudge.cli import _find_online_subtitles
from pudge.config import AppConfig
from pudge.filename import VideoIdentity
from pudge.models import AniListAnime, SubtitleCandidate
from pudge.providers.jimaku import JimakuClient, JimakuEntry, JimakuFile


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def test_arrow_seek_forces_fresh_paint_and_drops_stale_responses() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "seekJump=options.seekJump===true||" in source
    assert "ui.lnPairedSeekGeneration=generation" in source
    assert "generation!==Number(ui.lnPairedSeekGeneration||0)" in source
    assert "applyLnPairedPosition(updated,{seekJump:true,reason:'shortcut'})" in source
    assert "lnPairedResetWordProgress(reader)" in source


def test_exact_anime_title_entry_is_not_hidden_behind_four_id_duplicates(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.jimaku.api_key = "token"
    video = tmp_path / "[SubsPlease] Hyakkano - 31 (1080p) [31BF37F3].mkv"
    video.write_bytes(b"video")
    title = "Kimi no Koto ga Dai Dai Dai Dai Daisuki na 100-nin no Kanojo 3rd Season"
    target_entry = JimakuEntry(
        id=12234,
        name=title,
        english_name="The 100 Girlfriends Who Really Love You Season 3",
        japanese_name="君のことが大大大大大好きな100人の彼女 第3期",
        anilist_id=None,
        flags={},
    )
    linked_duplicates = [
        JimakuEntry(
            id=12000 + index,
            name=f"Hyakkano mirror {index}",
            english_name=None,
            japanese_name=None,
            anilist_id=200637,
            flags={},
        )
        for index in range(4)
    ]
    target_file = JimakuFile(
        url="https://jimaku.cc/entry/12234/download/episode-07.srt",
        name=(
            "[shincaps] Kimi no Koto ga Dai Dai Dai Dai Daisuki na 100-nin "
            "no Kanojo 3rd Season - 07 (AT-X 1440x1080 MPEG2 AAC).srt"
        ),
        size=1000,
        last_modified="",
    )
    requested_entries: list[int] = []

    class FakeJimaku:
        def __init__(self, *_args, **_kwargs):
            pass

        def search_entries(self, *, anilist_id=None, query=None):
            if anilist_id is not None:
                return list(linked_duplicates)
            return [target_entry] if query else []

        def rank_entries(self, entries, identity, anilist_id):
            return JimakuClient.rank_entries(
                object.__new__(JimakuClient), entries, identity, anilist_id
            )

        def files_for_episode(self, entry_id, _episode, alternative_episodes=()):
            requested_entries.append(entry_id)
            return [target_file] if entry_id == target_entry.id else []

        def rank_files(self, files, identity, target_video, **kwargs):
            return JimakuClient.rank_files(
                object.__new__(JimakuClient), files, identity, target_video, **kwargs
            )

        def close(self):
            pass

    subtitle = tmp_path / "episode-07.srt"
    subtitle.write_text("日本語", encoding="utf-8")

    def fake_materialize(_client, item, *_args, **_kwargs):
        return [
            SubtitleCandidate(
                path=subtitle,
                source="jimaku",
                score=item.score,
                name=item.name,
                verified_japanese=True,
            )
        ]

    monkeypatch.setattr("pudge.cli.JimakuClient", FakeJimaku)
    monkeypatch.setattr("pudge.cli.materialize_jimaku_files", fake_materialize)
    monkeypatch.setattr("pudge.cli._jimaku_episode_aliases", lambda *_args: (31,))
    anime = AniListAnime(
        id=200637,
        titles=[title, "君のことが大大大大大好きな100人の彼女 第3期"],
        synonyms=["Hyakkano 3"],
        season_year=2026,
        episodes=12,
        format="TV",
    )

    result = _find_online_subtitles(
        video,
        VideoIdentity(title="Hyakkano", episode=7),
        cfg,
        None,
        False,
        anime_hint=anime,
        skip_airing_lookup=True,
    )

    assert len(result) == 1
    assert result[0].name == target_file.name
    assert requested_entries[-1] == 12234
    assert result[0].score > cfg.matching.jimaku_min_score
