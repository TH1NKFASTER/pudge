from __future__ import annotations

from pathlib import Path

from anime_mpv.cli import _deduplicate_subtitle_candidates, _find_online_subtitles
from anime_mpv.config import AppConfig, LLMConfig
from anime_mpv.llm import OllamaClient
from anime_mpv.models import AniListAnime, JimakuEntry, SubtitleCandidate, VideoIdentity
from anime_mpv.pipeline_cache import (
    final_pipeline_cache_available,
    load_final_pipeline_result,
    save_final_pipeline_result,
)


def _srt(text: str) -> str:
    return f"1\n00:00:01,000 --> 00:00:02,000\n{text}\n"


def test_final_pipeline_cache_roundtrip_and_dependency_validation(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    video = tmp_path / "episode.mkv"
    subtitle = tmp_path / "cleaned.srt"
    video.write_bytes(b"video")
    subtitle.write_text(_srt("日本語"), encoding="utf-8")

    save_final_pipeline_result(
        video,
        cfg,
        subtitle=subtitle,
        subtitle_id=None,
        dependency=subtitle,
        source="external",
    )

    cached = load_final_pipeline_result(video, cfg)
    assert cached is not None
    assert cached["subtitle"] == str(subtitle.resolve())
    assert final_pipeline_cache_available(video, cfg)

    subtitle.write_text(_srt("変更"), encoding="utf-8")
    assert load_final_pipeline_result(video, cfg) is None


def test_llm_semantic_result_is_cached(tmp_path: Path, monkeypatch) -> None:
    japanese = tmp_path / "ja.srt"
    english = tmp_path / "en.srt"
    japanese.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n"
        "2\n00:00:05,000 --> 00:00:06,000\n元気ですか\n",
        encoding="utf-8",
    )
    english.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nHello\n\n"
        "2\n00:00:05,000 --> 00:00:06,000\nHow are you?\n",
        encoding="utf-8",
    )
    config = LLMConfig(
        enabled=True,
        model="test-model",
        embedded_reference_sample_count=2,
        embedded_reference_phrases_per_sample=1,
    )
    client = OllamaClient(config, tmp_path / "cache")
    calls = 0

    def fake_chat(_system: str, _user: str):
        nonlocal calls
        calls += 1
        return {
            "same_episode": True,
            "usable_for_timing": True,
            "similarity": 0.95,
            "matched_samples": 2,
            "total_samples": 2,
            "sample_scores": [0.95, 0.95],
            "reason": "ok",
        }

    monkeypatch.setattr(client, "_json_chat", fake_chat)
    first = client.compare_subtitle_semantics(japanese, english)
    second = client.compare_subtitle_semantics(japanese, english)
    forced = client.compare_subtitle_semantics(japanese, english, force=True)
    client.close()

    assert first["cached"] is False
    assert second["cached"] is True
    assert forced["cached"] is False
    assert calls == 2


def test_content_deduplication_keeps_one_identical_candidate(tmp_path: Path) -> None:
    first = tmp_path / "one.srt"
    second = tmp_path / "two.srt"
    first.write_text(_srt("同じ字幕"), encoding="utf-8")
    second.write_text(_srt("同じ字幕"), encoding="utf-8")
    candidates = [
        SubtitleCandidate(first, "local", 70.0, first.name),
        SubtitleCandidate(second, "jimaku", 80.0, second.name),
    ]

    result, removed = _deduplicate_subtitle_candidates(
        candidates,
        tmp_path / "cache",
        ffmpeg_path="ffmpeg",
    )

    assert removed == 1
    assert len(result) == 1
    assert result[0].path == second


def test_exact_anilist_jimaku_match_skips_name_search(tmp_path: Path, monkeypatch) -> None:
    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.jimaku.api_key = "token"
    video = tmp_path / "Anime - 05.mkv"
    video.write_bytes(b"video")
    calls: list[tuple[int | None, str | None]] = []

    class FakeJimaku:
        def __init__(self, *_args, **_kwargs):
            pass

        def search_entries(self, *, anilist_id=None, query=None):
            calls.append((anilist_id, query))
            return [
                JimakuEntry(
                    id=10,
                    name="Anime",
                    english_name="Anime",
                    japanese_name=None,
                    anilist_id=123,
                    flags={},
                )
            ]

        def rank_entries(self, entries, _identity, _anilist_id):
            return entries

        def files_for_episode(self, _entry_id, _episode):
            return []

        def rank_files(self, files, *_args, **_kwargs):
            return files

        def close(self):
            pass

    monkeypatch.setattr("anime_mpv.cli.JimakuClient", FakeJimaku)
    anime = AniListAnime(
        id=123,
        titles=["Anime"],
        synonyms=["Other title"],
        season_year=2026,
        episodes=12,
        format="TV",
    )

    result = _find_online_subtitles(
        video,
        VideoIdentity(title="Anime", episode=5),
        cfg,
        None,
        False,
        anime_hint=anime,
        skip_airing_lookup=True,
    )

    assert result == []
    assert calls == [(123, None)]


def test_process_video_uses_final_cache_without_discovery(tmp_path: Path, monkeypatch) -> None:
    from anime_mpv.cli import build_parser, process_video

    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.anilist.enabled = False
    cfg.llm.enabled = False
    video = tmp_path / "Anime - 05.mkv"
    subtitle = tmp_path / "cached.srt"
    video.write_bytes(b"video")
    subtitle.write_text(_srt("日本語"), encoding="utf-8")
    save_final_pipeline_result(
        video,
        cfg,
        subtitle=subtitle,
        subtitle_id=None,
        dependency=subtitle,
        source="external",
    )

    monkeypatch.setattr(
        "anime_mpv.cli.find_embedded_japanese_subtitles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("probe called")),
    )
    monkeypatch.setattr(
        "anime_mpv.cli.find_local_subtitles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("local search called")),
    )
    launched: list[list[str]] = []

    def fake_run(command, **_kwargs):
        launched.append(command)
        return 0

    monkeypatch.setattr("anime_mpv.cli.run_mpv", fake_run)
    args = build_parser().parse_args([str(video)])
    args.config = tmp_path / "config.toml"

    assert process_video(video, args, cfg, None) == 0
    assert launched
    sub_arg = next(item for item in launched[0] if item.startswith("--sub-file="))
    assert Path(sub_arg.split("=", 1)[1]).is_file()


def test_exact_anilist_movie_uses_unfiltered_files_and_overrides_filename_score(
    tmp_path: Path, monkeypatch
) -> None:
    from anime_mpv.models import JimakuFile

    cfg = AppConfig()
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.jimaku.api_key = "token"
    cfg.matching.jimaku_min_score = 45.0
    video = tmp_path / "Boku no Hero Academia Movie 1.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "generic-japanese.srt"
    subtitle.write_text(_srt("これは映画の日本語字幕です。" * 8), encoding="utf-8")
    requested_episodes: list[int | None] = []
    ranked_identities: list[VideoIdentity] = []

    movie_file = JimakuFile(
        url="https://example.test/movie.srt",
        name="劇場版ヒロアカ字幕.srt",
        size=subtitle.stat().st_size,
        last_modified="",
        score=2.0,
    )

    class FakeJimaku:
        def __init__(self, *_args, **_kwargs):
            pass

        def search_entries(self, *, anilist_id=None, query=None):
            assert query is None
            return [
                JimakuEntry(
                    id=2846,
                    name="Boku no Hero Academia THE MOVIE: Futari no Hero",
                    english_name="My Hero Academia: Two Heroes",
                    japanese_name=None,
                    anilist_id=100723,
                    flags={"movie": True},
                )
            ]

        def rank_entries(self, entries, _identity, _anilist_id):
            return entries

        def files_for_episode(self, _entry_id, episode):
            requested_episodes.append(episode)
            return [movie_file]

        def rank_files(self, files, identity, *_args, **_kwargs):
            ranked_identities.append(identity)
            return files

        def close(self):
            pass

    def fake_materialize(_client, item, identity, *_args, **_kwargs):
        assert identity.episode is None
        return [
            SubtitleCandidate(
                subtitle,
                "jimaku",
                item.score,
                item.name,
                verified_japanese=True,
            )
        ]

    monkeypatch.setattr("anime_mpv.cli.JimakuClient", FakeJimaku)
    monkeypatch.setattr("anime_mpv.cli.materialize_jimaku_files", fake_materialize)
    anime = AniListAnime(
        id=100723,
        titles=["Boku no Hero Academia THE MOVIE: Futari no Hero"],
        synonyms=["My Hero Academia: Two Heroes"],
        season_year=2018,
        episodes=1,
        format="MOVIE",
    )

    result = _find_online_subtitles(
        video,
        VideoIdentity(title="Boku no Hero Academia Movie", episode=1),
        cfg,
        None,
        False,
        anime_hint=anime,
        skip_airing_lookup=False,
    )

    assert requested_episodes == [None]
    assert ranked_identities[0].episode is None
    assert len(result) == 1
    assert result[0].score == 55.0
    assert result[0].details["movie_exact_entry_override"] is True
    assert result[0].details["exact_anilist_movie_entry"] is True
    assert result[0].details["requested_episode"] is None
