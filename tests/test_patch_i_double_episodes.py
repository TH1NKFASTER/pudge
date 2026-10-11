"""Patch I (audit 17А): two episodes in one file ("Show - 01-02")."""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge import cli
from pudge.anilist_tracking import TrackingPayload, create_tracking_file, read_tracking_file
from pudge.config import AppConfig, write_config
from pudge.database import Database
from pudge.double_episode import prepare_double_episode
from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode, NyaaRelease
from pudge.models import SubtitleCandidate
from pudge.providers import nyaa
from pudge.release_parser import double_episode_span
from pudge.subtitle_formats import parse_srt, write_srt
from tests.lua_harness import ANILIST, needs_lua, run_mpv_scenario

DOUBLE = "[SubsPlease] Show - 01-02 (1080p) [ABCD1234].mkv"


# Names


@pytest.mark.parametrize(
    "name,span",
    [
        (DOUBLE, (1, 2)),
        ("[Erai-raws] Show - 05 ~ 06 [1080p][Multiple Subtitle].mkv", (5, 6)),
        ("[Group] Show - 01+02 [1080p].mkv", (1, 2)),
        ("Show S01E03-E04 1080p WEB-DL.mkv", (3, 4)),
        ("[Group] Show (07-08) [1080p]", (7, 8)),
        ("[Group] Show 2nd Season - 01-02 [1080p]", (1, 2)),
        ("[Group] Show - 01-12 [1080p] [Batch]", None),  # a pack
        ("[Group] Show - 01-03 [1080p]", None),  # three episodes
        ("[Group] Show - 01-02 [Batch]", None),
        ("[SubsPlease] Mushoku Tensei 2 - 03 (1080p).mkv", None),  # sequel title + one episode
        ("[Group] Show 1-2 [1080p]", None),  # no episode marker
        ("[Group] Show - 00-01 [1080p]", None),
        ("[Group] Show S00E01-E02", None),
        ("[Group] Show - 03-04 (BD 1080p) Vol.2", None),
        ("[Group] Show - 03 [1080p].mkv", None),
    ],
)
def test_double_episode_names(name, span) -> None:
    assert double_episode_span(name) == span


def test_rows_get_the_second_episode_when_written(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=1, title="Show", status="CURRENT", episodes=12))
    double, single, unknown = (
        tmp_path / DOUBLE,
        tmp_path / "[SubsPlease] Show - 03 (1080p).mkv",
        tmp_path / "x" / DOUBLE,
    )
    db.upsert_episode(LibraryEpisode(1, "Show", 1, double))
    db.upsert_episode(LibraryEpisode(1, "Show", 3, single))
    db.upsert_episode(LibraryEpisode(1, "Show", None, unknown))
    assert (db.episode_by_path(double).media_episode_end, db.episode_by_path(double).last_episode) == (2, 2)
    assert (
        db.episode_by_path(single).media_episode_end is None and db.episode_by_path(single).last_episode == 3
    )
    assert db.episode_by_path(unknown).media_episode_end is None
    db.upsert_download(DownloadItem("a" * 40, DOUBLE, "downloading", 0.1, "/x", "/x", media_id=1, episode=1))
    db.upsert_download(
        DownloadItem("b" * 40, DOUBLE, "downloading", 0.1, "/x", "/x", media_id=1, episode=5, is_batch=True)
    )
    assert db.download_by_hash("a" * 40).media_episode_end == 2
    assert db.download_by_hash("b" * 40).media_episode_end is None
    # A confirmed identity keeps the span from the new first episode.
    db.attach_download_metadata("a" * 40, 1, 3, False)
    assert db.download_by_hash("a" * 40).media_episode_end == 4


# Search


def _release(title: str, **extra) -> NyaaRelease:
    values = dict(
        title=title,
        link="",
        torrent_url="",
        info_hash=("a" if "01-02" in title else "b") * 40,
        size_text="1 GiB",
        size_bytes=1024**3,
        seeders=50,
        leechers=1,
        downloads=10,
        published="",
        trusted=True,
        remake=False,
        category_id="1_2",
        group="SubsPlease",
    )
    values.update(extra)
    return NyaaRelease(**values)


def test_double_is_offered_for_its_first_episode_and_ranks_below_a_single() -> None:
    anime = LibraryAnime(media_id=1, title="Show", episodes=12)
    # The feed marks "01-02" as a pack: still a batch for batch searches, let through in
    # episode searches (other packs are not).
    pack_title = "[SubsPlease] Show (01-12) (1080p) [Batch]"

    class Feed:
        def releases(self, *_args):
            return [
                _release(DOUBLE, is_batch=True),
                _release(pack_title, is_batch=True, info_hash="c" * 40),
                _release("[SubsPlease] Show - 01 (1080p) [ABCD1234].mkv"),
            ]

    search = dict(
        trusted_groups=["SubsPlease"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=0,
        target_episode_max_bytes=40 * 1024**3,
    )
    episode_hits = {
        item.title for item in nyaa.search_shana_ranked(Feed(), anime, episode=1, batch=False, **search)
    }
    batch_hits = {
        item.title for item in nyaa.search_shana_ranked(Feed(), anime, episode=None, batch=True, **search)
    }
    assert episode_hits == {DOUBLE, "[SubsPlease] Show - 01 (1080p) [ABCD1234].mkv"}
    assert batch_hits == {DOUBLE, pack_title}
    assert nyaa._release_is_eligible_for_episode(DOUBLE, 1, anime)
    assert not nyaa._release_is_eligible_for_episode(DOUBLE, 2, anime)
    assert nyaa.release_double_episode(
        "[Group] Show - 13-14 [1080p]", 1, anime, alternative_episodes=(13,)
    ) == (13, 14)

    kwargs = dict(
        episode=1,
        batch=False,
        trusted_groups=["SubsPlease"],
        preferred_groups=[],
        blocked_groups=[],
        preferred_resolution="1080p",
        min_seeders=1,
        target_episode_min_bytes=0,
        target_episode_max_bytes=4 * 1024**3,
    )
    double = nyaa.score_release(_release(DOUBLE), anime, **kwargs)
    single = nyaa.score_release(_release("[SubsPlease] Show - 01 (1080p) [ABCD1234].mkv"), anime, **kwargs)
    assert "double-episode=1-2" in double.reasons and "ep=1" in double.reasons
    assert "episode-pack" not in double.reasons
    assert double.numbering_status == "resolved" and double.raw_release_episode == 1
    assert single.score - double.score == pytest.approx(30.0)
    pack = nyaa.score_release(
        _release("[SubsPlease] Show - 01-02 (1080p)", is_batch=True),
        anime,
        **{**kwargs, "episode": None, "batch": True},
    )
    assert "batch" in pack.reasons  # a 2-episode entry still downloads as a pack


def _manager(tmp_path: Path):
    from pudge.manager import AnimeManager

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.root_dir = tmp_path / "library"
    cfg.paths.cache_dir = tmp_path / "cache"
    return AnimeManager(cfg)


def test_auto_download_prefers_singles_and_waits_ten_minutes_for_them(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    anime = LibraryAnime(media_id=1, title="Show", episodes=12)
    double = _release(DOUBLE, reasons=["ep=1", "double-episode=1-2"])
    single = _release("[Other] Show - 01 [1080p]", reasons=["ep=1"])

    assert manager._prefer_single_episode_releases(anime, 1, [double, single]) == [single]
    # Just published: wait for a single-episode release.
    fresh = _release(
        DOUBLE,
        reasons=["ep=1", "double-episode=1-2"],
        published=time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime()),
    )
    assert manager._prefer_single_episode_releases(anime, 1, [fresh]) == []
    old = _release(
        DOUBLE,
        reasons=["ep=1", "double-episode=1-2"],
        published=time.strftime("%a, %d %b %Y %H:%M:%S +0000", time.gmtime(time.time() - 700)),
    )
    assert manager._prefer_single_episode_releases(anime, 1, [old]) == [old]
    # Unknown publish time: counted from when Pudge first saw it.
    unknown = _release(DOUBLE, reasons=["ep=1", "double-episode=1-2"], info_hash="c" * 40)
    assert manager._prefer_single_episode_releases(anime, 1, [unknown]) == []
    manager.db.set_state("double_episode_seen:" + "c" * 40, str(time.time() - 601))
    assert manager._prefer_single_episode_releases(anime, 1, [unknown]) == [unknown]


# Library


def test_a_double_file_or_download_covers_both_episodes(tmp_path: Path) -> None:
    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=1, title="Show", status="CURRENT", episodes=12))
    video = tmp_path / DOUBLE
    video.write_bytes(b"v")
    db.upsert_episode(LibraryEpisode(1, "Show", 1, video, state="ready"))
    assert db.has_episode(1, 1) and db.has_episode(1, 2) and not db.has_episode(1, 3)
    assert db.ready_episode(1, 2).video_path == video
    exact = tmp_path / "[Other] Show - 02 [1080p].mkv"
    db.upsert_episode(LibraryEpisode(1, "Show", 2, exact, state="ready"))
    assert db.ready_episode(1, 2).video_path == exact  # an exact single wins
    db.delete_episode_record(exact)
    assert db.episode_by_path(video).media_episode_end == 2
    assert db.set_part_boundary(video, 1421.5) and db.episode_by_path(video).part_boundary == 1421.5

    db.upsert_download(
        DownloadItem(
            "h" * 40,
            "[SubsPlease] Show - 03-04 (1080p).mkv",
            "downloading",
            0.2,
            "/x",
            "/x",
            media_id=1,
            episode=3,
        )
    )
    # A later sync without the span keeps it.
    db.upsert_download(
        DownloadItem("h" * 40, "renamed", "downloading", 0.4, "/x", "/x", media_id=1, episode=3)
    )
    assert db.has_episode(1, 4) and not db.has_episode(1, 5)
    assert db.download_by_hash("h" * 40).media_episode_end == 4

    # Episode one counted at the boundary: the file is not queued for deletion,
    # even with enough watch time for 65 % of the whole file.
    db.raise_anime_progress(1, 1)
    db.record_playback(video, 2000, 2880, active_seconds=2000)
    assert db.repair_missing_cleanup_schedule(24) == 0 and db.episode_by_path(video).delete_after is None
    assert db.schedule_cleanup(tmp_path / "moved" / DOUBLE, 24, media_id=1, episode=2) == 1
    assert db.get_anime(1).progress == 2
    # Lowering AniList progress below episode two returns the file.
    assert db.reconcile_anilist_progress(1, 1) == 1 and db.episode_by_path(video).state == "ready"


def test_completed_double_download_registers_one_row_for_two_episodes(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.db.upsert_anime(LibraryAnime(media_id=1, title="Show", status="CURRENT", episodes=12))
    anime = manager.db.get_anime(1)
    (tmp_path / DOUBLE).write_bytes(b"video")
    item = DownloadItem(
        "h" * 40, DOUBLE, "uploading", 1.0, str(tmp_path), str(tmp_path / DOUBLE), media_id=1, episode=1
    )
    assert manager._media_episode_from_release(anime, 1, release_name=str(tmp_path / DOUBLE)) == 1
    assert manager._download_file_media_episode(anime, item, None, tmp_path / DOUBLE) == 1
    assert manager._completed_download_video_files(item) == [(tmp_path / DOUBLE).resolve()]
    folder = tmp_path / "pack"
    folder.mkdir()
    for number in (1, 2, 3):
        (folder / f"[Group] Show - 0{number} [1080p].mkv").write_bytes(b"v")
    folder_item = DownloadItem(
        "f" * 40,
        "[Group] Show - 01-02 [1080p]",
        "uploading",
        1.0,
        str(folder),
        str(folder),
        media_id=1,
        episode=1,
    )
    folder_item.release_episode = 1
    assert [path.name for path in sorted(manager._completed_download_video_files(folder_item))] == [
        "[Group] Show - 01 [1080p].mkv",
        "[Group] Show - 02 [1080p].mkv",
    ]
    # "01-02" as two files: each file keeps its own number inside the span.
    two_files = DownloadItem(
        "h" * 40, "[Group] Show - 01-02 [1080p]", "uploading", 1.0, "/x", "/x", media_id=1, episode=1
    )
    assert (
        manager._download_file_media_episode(anime, two_files, 2, tmp_path / "[Group] Show - 02 [1080p].mkv")
        == 2
    )
    assert (
        manager._download_file_media_episode(anime, two_files, 3, tmp_path / "[Group] Show - 03 [1080p].mkv")
        is None
    )


# Subtitles


def _srt(path: Path, cues) -> Path:
    return write_srt([(start, end, f"台詞{index}") for index, (start, end) in enumerate(cues)], path)


def _episode_cues(length: float) -> list[tuple[float, float]]:
    return [(10.0 + step * 60.0, 12.0 + step * 60.0) for step in range(int((length - 12.0) // 60.0) + 1)]


def test_each_episode_is_aligned_on_its_half_and_joined(tmp_path: Path) -> None:
    first_cues = _episode_cues(1300)
    second_cues = _episode_cues(1300)
    first = SubtitleCandidate(_srt(tmp_path / "ep1.srt", first_cues), "jimaku", 90, "ep1.srt", episode=1)
    second = SubtitleCandidate(_srt(tmp_path / "ep2.srt", second_cues), "jimaku", 90, "ep2.srt", episode=2)
    true_second_start = 1420.0
    duration = true_second_start + second_cues[-1][1] + 160.0
    cuts: list[tuple[float, float | None]] = []

    def cut(video, start, end, output):
        cuts.append((start, end))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"{start}", encoding="utf-8")
        return output

    def align(part, candidates):
        start = float(part.read_text(encoding="utf-8"))
        chosen = candidates[0]
        if part.name == "part-a.mkv":
            cues = [(s + 2.0, e + 2.0, t) for s, e, t in parse_srt(first.path)]
        else:  # the aligner finds where episode two really is
            cues = [
                (s + true_second_start - start, e + true_second_start - start, t)
                for s, e, t in parse_srt(second.path)
            ]
        return (
            chosen,
            write_srt(cues, tmp_path / f"aligned-{part.stem}.srt"),
            {"sync_was_successful": True, "engine": "fake"},
        )

    result = prepare_double_episode(
        tmp_path / DOUBLE,
        [first],
        [second],
        duration=duration,
        work_dir=tmp_path / "work",
        cut=cut,
        align=align,
        to_srt=lambda path: path,
    )

    assert result.subtitle is not None and result.boundary == pytest.approx(true_second_start, abs=0.01)
    first_end = first_cues[-1][1] + 2.0
    assert cuts[0] == (0.0, min(duration, first_cues[-1][1] + 240.0))
    assert cuts[1] == (pytest.approx(first_end - 30.0), None)
    merged = parse_srt(result.subtitle)
    assert len(merged) == len(first_cues) + len(second_cues)
    assert merged[0][0] == pytest.approx(12.0)
    assert merged[len(first_cues)][0] == pytest.approx(true_second_start + 10.0, abs=0.01)
    assert result.alignment["double_episode"]["boundary"] == pytest.approx(true_second_start, abs=0.01)


def test_missing_or_misfit_second_episode_is_not_joined(tmp_path: Path) -> None:
    first = SubtitleCandidate(
        _srt(tmp_path / "ep1.srt", _episode_cues(1300)), "jimaku", 90, "ep1.srt", episode=1
    )
    long_second = SubtitleCandidate(
        _srt(tmp_path / "ep2.srt", _episode_cues(2000)), "jimaku", 90, "ep2.srt", episode=2
    )

    def cut(video, start, end, output):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("0", encoding="utf-8")
        return output

    def align(part, candidates):
        return candidates[0], candidates[0].path, {}

    args = dict(duration=2880.0, work_dir=tmp_path / "work", cut=cut, align=align, to_srt=lambda path: path)
    assert (
        prepare_double_episode(tmp_path / DOUBLE, [first], [], **args).reason == "no_second_episode_subtitles"
    )
    assert (
        prepare_double_episode(tmp_path / DOUBLE, [first], [long_second], **args).reason
        == "second_episode_does_not_fit"
    )


def test_prepare_never_aligns_one_episode_to_the_whole_double_file(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.anilist.enabled = False
    cfg.llm.enabled = False
    video = tmp_path / DOUBLE
    video.write_bytes(b"video")
    merged = _srt(tmp_path / "merged.srt", [(10, 12), (1430, 1432)])
    monkeypatch.setattr(cli, "find_embedded_japanese_subtitles", lambda *a, **k: [])
    monkeypatch.setattr(
        cli, "find_local_subtitles", lambda *a, **k: pytest.fail("normal candidate path used")
    )
    monkeypatch.setattr(
        cli,
        "_prepare_double_episode_subtitles",
        lambda *a, **k: (merged, {"sync_was_successful": True, "reference_alignment_reliable": True}, 1420.0),
    )
    args = cli.build_parser().parse_args(
        [str(video), "--prepare-only", "--episode-hint", "1", "--episode-end-hint", "2", "--offline"]
    )
    assert cli.process_video(video, args, cfg, None) == 0
    out = capsys.readouterr().out
    assert "PREPARED_PART_BOUNDARY=1420.000" in out and "PREPARE_STATUS=ready" in out
    assert cli._double_episode_span_from_args(SimpleNamespace(episode_hint=1, episode_end_hint=3)) is None


def test_manager_passes_the_span_and_stores_the_boundary(tmp_path: Path, monkeypatch) -> None:
    from desktop_isolation import patch_webapp_popen

    api = _api(tmp_path)
    db = api.manager.db
    db.upsert_anime(LibraryAnime(media_id=9, title="Show", status="CURRENT", episodes=12))
    video = (tmp_path / DOUBLE).resolve()
    video.write_bytes(b"video")
    prepared = _srt(tmp_path / "prepared.srt", [(10, 12), (1430, 1432)])
    db.upsert_episode(LibraryEpisode(9, "Show", 1, video, state="waiting_subtitles"))
    db.queue_subtitle_job(video, 9, 1)
    prepare_commands: list[list[str]] = []

    class Prepare:
        returncode = 0

        def __init__(self, command, **_kwargs):
            prepare_commands.append(command)

        def poll(self):
            return 0

        def communicate(self):
            return (
                f"PREPARED_SUBTITLE={prepared}\nPREPARED_PART_BOUNDARY=1420.000\nPREPARE_STATUS=ready\n",
                "",
            )

    monkeypatch.setattr("pudge.manager.subprocess.Popen", Prepare)
    assert api.manager.process_subtitle_jobs(limit=1) == 1
    [command] = [command for command in prepare_commands if "--episode-hint" in command]
    assert command[command.index("--episode-hint") + 1] == "1"
    assert command[command.index("--episode-end-hint") + 1] == "2"  # subtitles for both episodes
    row = db.episode_by_path(video)
    assert row.state == "ready" and row.part_boundary == pytest.approx(1420.0)

    # Playback tells the tracker where episode two starts.
    class Player:
        pid = 123

        def poll(self):
            return None

    play_commands: list[list[str]] = []
    patch_webapp_popen(monkeypatch, lambda command, **_kwargs: play_commands.append(command) or Player())
    api.play(str(video))
    [play] = play_commands
    assert play[play.index("--episode-end-hint") + 1] == "2"
    assert play[play.index("--part-boundary") + 1] == "1420.000"


# Watching


def _config(tmp_path: Path) -> AppConfig:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.root_dir = tmp_path
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.anilist.access_token = "token"
    cfg.playback.enabled = True
    return cfg


def test_each_episode_counts_at_its_own_end(tmp_path: Path, monkeypatch) -> None:
    cfg = _config(tmp_path)
    video = tmp_path / DOUBLE
    video.write_bytes(b"video")
    db = Database(cfg.library.database_path)
    db.upsert_anime(LibraryAnime(media_id=7, title="Show", status="CURRENT", episodes=12))
    db.upsert_episode(LibraryEpisode(7, "Show", 1, video, state="ready"))
    tracking = create_tracking_file(
        cfg.paths.cache_dir,
        TrackingPayload(
            video=str(video),
            title="Show",
            media_id=7,
            episode=1,
            total_episodes=12,
            threshold=5 / 6,
            mapping_key="k",
            episode_end=2,
            part_boundary=1420.0,
        ),
    )
    assert read_tracking_file(tracking).episode_end == 2
    updates: list[int] = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def update_progress(self, media_id, progress, *_args, **_kwargs):
            updates.append(progress)
            return {"updated": True, "progress": progress, "status": "CURRENT"}

        def close(self):
            pass

    monkeypatch.setattr(cli, "AniListClient", Client)
    args = lambda part: SimpleNamespace(
        tracking_file=tracking, anilist_action="update", anilist_id=None, manual=False, part=part
    )  # noqa: E731

    db.record_playback(video, 1425, 2880, active_seconds=600)  # not enough of episode one
    assert cli._run_anilist_action(args(1), cfg) == 3 and updates == []
    db.record_playback(video, 1430, 2880, active_seconds=400)  # 1000 s >= 65 % of 1420 s
    assert cli._run_anilist_action(args(1), cfg) == 0 and updates == [1]
    assert db.get_anime(7).progress == 1 and db.episode_by_path(video).state == "ready"

    db.record_playback(video, 2500, 2880, active_seconds=500)  # 1500 s: not 65 % of episode two yet
    assert cli._run_anilist_action(args(0), cfg) == 3
    db.record_playback(video, 2800, 2880, active_seconds=500)  # 2000 s = 1000 + 65 % of 1460 s
    assert cli._run_anilist_action(args(0), cfg) == 0 and updates == [1, 2]
    row = db.episode_by_path(video)
    assert row.state == "watched" and row.delete_after is not None and db.get_anime(7).progress == 2


@needs_lua
@pytest.mark.parametrize(
    "scenario,expect",
    [
        ("sim.set('time-pos', 1410); sim.fire('file-loaded'); sim.play(15)", 1),  # crossed while playing
        ("sim.set('time-pos', 1500); sim.fire('file-loaded'); sim.play(15)", 0),  # resumed inside episode two
        (
            "sim.set('time-pos', 1400); sim.fire('file-loaded'); sim.play(2); sim.press('Ctrl+a')",
            1,
        ),  # manual, episode one
    ],
)
def test_tracker_counts_episode_one_when_episode_two_starts(
    tmp_path: Path, scenario: str, expect: int
) -> None:
    out = run_mpv_scenario(
        tmp_path,
        f"""
        sim.set('path', '/media/{DOUBLE}')
        sim.set('duration', 2880)
        {scenario}
        print('PART_ONE=' .. #sim.commands('%-%-part 1'))
    """,
        scripts=[ANILIST],
        env={
            "PUDGE_ANILIST_TRACKING_FILE": "/tmp/t.json",
            "PUDGE_ANILIST_AUTO_UPDATE": "1",
            "PUDGE_ANILIST_PART_BOUNDARY": "1420.000",
        },
    )
    assert f"PART_ONE={expect}" in out


# Home, iPhone, statistics


def _api(tmp_path: Path):
    from pudge.web_app import WebAppApi

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    return WebAppApi(cfg.config_path)


def test_home_shows_both_numbers_until_each_is_watched(tmp_path: Path) -> None:
    api = _api(tmp_path)
    db = api.manager.db
    db.upsert_anime(LibraryAnime(media_id=9, title="Show", status="CURRENT", episodes=12))
    video = tmp_path / DOUBLE
    video.write_bytes(b"v")
    subtitle = _srt(tmp_path / "show.ja.srt", [(10, 12)])
    db.upsert_episode(LibraryEpisode(9, "Show", 1, video, subtitle_path=subtitle, state="ready"))

    def ready():
        anime = {item.media_id: item for item in db.anime_list()}
        return [card["ready_episodes"] for card in api._downloaded_payloads(anime)]

    assert ready() == [[1, 2]]
    db.raise_anime_progress(9, 1)
    assert ready() == [[2]]
    db.raise_anime_progress(9, 2)
    assert ready() == []


def test_statistics_title_names_both_episodes(tmp_path: Path) -> None:
    from pudge.consumption import ConsumptionLedger

    db = Database(tmp_path / "library.sqlite3")
    db.upsert_anime(LibraryAnime(media_id=9, title="Show", status="CURRENT", episodes=12))
    video = tmp_path / DOUBLE
    video.write_bytes(b"v")
    db.upsert_episode(LibraryEpisode(9, "Show", 1, video, state="ready"))
    ref = ConsumptionLedger(db).anime_episode_media(video)
    assert ref is not None and ref.title == "Show · 1–2"


def test_iphone_lists_the_double_until_both_are_watched(tmp_path: Path, monkeypatch) -> None:
    from pudge.iphone_service import IPhoneService

    db = Database(tmp_path / "library.sqlite3")
    service = IPhoneService(db, tmp_path / "mobile")
    db.upsert_anime(LibraryAnime(media_id=9, title="Show", status="CURRENT", episodes=12))
    video = tmp_path / DOUBLE
    video.write_bytes(b"v")
    subtitle = _srt(tmp_path / "show.ja.srt", [(10, 12)])
    db.upsert_episode(LibraryEpisode(9, "Show", 1, video, subtitle_path=subtitle, state="ready"))
    db.set_part_boundary(video, 1420.0)
    marked: list[int] = []

    class Library:
        def __getattr__(self, name):
            return getattr(db, name)

        def mark_personal_release_watched(self, media_id, number, **_kwargs):
            marked.append(number)
            return True

    anilist = SimpleNamespace(access_token="", endpoint="e")
    service.api = SimpleNamespace(
        manager=SimpleNamespace(db=Library()),
        config=SimpleNamespace(anilist=anilist, agent=SimpleNamespace(delete_after_watched_hours=24)),
    )

    def listed():
        return [
            (item["number"], item.get("numberEnd"), item.get("partBoundary"))
            for item in service.media.episodes()["items"]
        ]

    assert listed() == [(1, 2, 1420.0)]
    db.raise_anime_progress(9, 1)
    assert listed() == [(1, 2, 1420.0)]  # the second episode is still unwatched
    episode_id = service.media.episodes()["items"][0]["id"]
    db.raise_anime_progress(9, 2)
    assert listed() == []
    # Watched on the phone (first episode already counted): a personal schedule counts both releases.
    with db.connect() as conn:
        conn.execute("UPDATE anime SET progress=1 WHERE media_id=9")
    result = service.media.episode_watched(episode_id, {"duration": 2880, "activeSeconds": 2000})
    assert result["ok"] is True and marked == [1, 2]
