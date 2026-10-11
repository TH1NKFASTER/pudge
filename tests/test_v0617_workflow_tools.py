from __future__ import annotations

import json
from pathlib import Path

from pudge import ocr
from pudge.config import AppConfig
from pudge.manager import AnimeManager
from pudge.manager_models import LibraryAnime, LibraryEpisode
from tests.i18n_support import en


def make_manager(tmp_path: Path) -> AnimeManager:
    config = AppConfig()
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.root_dir.mkdir()
    config.paths.cache_dir = tmp_path / "cache"
    config.paths.cache_dir.mkdir()
    config.config_path = tmp_path / "config.toml"
    config.qbittorrent.enabled = False
    return AnimeManager(config)


def test_manual_subtitle_is_copied_and_queued_with_high_priority(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.db.upsert_anime(LibraryAnime(media_id=10, title="Example"))
    video = tmp_path / "library" / "Example - 01.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(LibraryEpisode(media_id=10, title="Example", episode=1, video_path=video))
    subtitle = tmp_path / "manual.srt"
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n", encoding="utf-8")

    copied = manager.set_manual_subtitle(video, subtitle)

    assert copied.is_file()
    assert ".manual.ja.srt" in copied.name
    key = manager._manual_subtitle_state_key(video)
    assert manager.db.get_state(key) == str(copied)
    job = next(row for row in manager.db.subtitle_jobs() if row["video_path"] == str(video))
    assert int(job["priority"]) == 250


def test_subtitle_inbox_change_requeues_unresolved_jobs(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    manager.config.paths.subtitle_dirs = [inbox]
    manager.db.upsert_anime(LibraryAnime(media_id=11, title="Inbox Show"))
    video = tmp_path / "library" / "Inbox Show - 02.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=11, title="Inbox Show", episode=2, video_path=video, state="waiting_subtitles"
        )
    )
    manager.db.queue_subtitle_job(video, 11, 2, delay_seconds=3600, error="waiting")

    first = manager.scan_subtitle_inbox()
    assert first["files"] == 0
    (inbox / "Inbox Show - 02.ja.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\n日本語\n", encoding="utf-8"
    )
    changed = manager.scan_subtitle_inbox()

    assert changed["changed"] is True
    assert changed["files"] == 1
    assert changed["requeued"] >= 1
    job = next(row for row in manager.db.subtitle_jobs() if row["video_path"] == str(video))
    assert str(job["state"]) == "pending"


def test_episode_diagnostics_explain_missing_subtitles(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.db.upsert_anime(LibraryAnime(media_id=12, title="Diagnostic Show"))
    video = tmp_path / "library" / "Diagnostic Show - 03.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=12, title="Diagnostic Show", episode=3, video_path=video, state="waiting_text_subtitles"
        )
    )
    manager.db.queue_subtitle_job(video, 12, 3, error="Only image subtitles found")

    result = manager.diagnose_episode(12, 3)

    assert result["ready"] is False
    assert result["video_path"] == str(video)
    subtitle_check = next(item for item in result["checks"] if item["key"] == "subtitle")
    assert subtitle_check["ok"] is False
    assert "image subtitles" in subtitle_check["detail"]


def test_episode_diagnostics_do_not_expose_missing_internal_row_as_video_status(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.config.ui.language = "en"
    manager.db.upsert_anime(LibraryAnime(media_id=13, title="Not Downloaded Yet"))

    result = manager.diagnose_episode(13, 1)
    file_check = next(item for item in result["checks"] if item["key"] == "file")

    assert file_check["ok"] is False
    assert file_check["label"] == "Local video"
    assert file_check["detail"] == "Episode not downloaded yet"
    assert "No episode row" not in str(result)


def test_ocr_quality_gate_flags_weak_results_and_accepts_normal_results() -> None:
    weak = ocr.evaluate_ocr_quality([ocr.OCRCue(0, 1, "ABC")], 20)
    assert weak["accepted"] is False
    assert weak["status"] == "review"

    cues = [ocr.OCRCue(float(i), float(i + 1), f"これは日本語の字幕です{i}") for i in range(20)]
    good = ocr.evaluate_ocr_quality(cues, 22)
    assert good["accepted"] is True
    assert good["status"] == "accepted"


def test_why_not_ready_lets_the_user_choose_a_subtitle_file(tmp_path: Path, page) -> None:
    # A waiting card opens "Why not ready?" (the backend's real diagnosis); its "Choose
    # subtitle" button asks the backend for a manual file and shows the new diagnosis.
    manager = make_manager(tmp_path)
    manager.config.ui.language = "en"
    manager.db.upsert_anime(LibraryAnime(media_id=12, title="Diagnostic Show"))
    video = tmp_path / "library" / "Diagnostic Show - 03.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=12, title="Diagnostic Show", episode=3, video_path=video, state="waiting_text_subtitles"
        )
    )
    manager.db.queue_subtitle_job(video, 12, 3, error="Only image subtitles found")
    diagnosis = json.loads(json.dumps(manager.diagnose_episode(12, 3), default=str))
    card = {
        "media_id": 12,
        "title": "Diagnostic Show",
        "episodes": 12,
        "next_episode": 3,
        "site_url": "https://anilist.co/anime/12",
        "local": {"episode": 3, "state": "waiting_text_subtitles", "video_path": str(video)},
    }
    result = page.run(
        r"""
        await __click('#currentContent [data-action="diagnose-card"][data-media-id="12"]'); await __tick(20);
        const out = {title: __text('#modalTitle'), checks: __text('#modalBody .diagnostic-checks'), expected: t('action.whyNotReady')};
        await __click('#modalBody [data-action="choose-diagnostic-subtitle"]'); await __tick(20);
        out.open = $('modalBackdrop').classList.contains('open');
        return out;
        """,
        state={"home": {"waiting": [card]}, "current": [card]},
        api={"diagnose_episode": diagnosis, "choose_manual_subtitle": {"ok": True}},
    )
    out = result.value
    assert out["title"] == f"{out['expected']} — Diagnostic Show"
    assert "image subtitles" in out["checks"]
    assert result.calls_of("choose_manual_subtitle") == [[str(video)]]
    assert result.calls_of("diagnose_episode") == [[12, 3], [12, 3]] and out["open"] is True


def test_settings_repair_library_reports_what_it_fixed(page) -> None:
    # Settings > Maintenance (where the old Downloads page's tools live, see
    # test_v0663_ln_reader_search_and_epub.py) repairs the library and lists the counts.
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('settings', true); await __tick(20);
            const inMaintenance = Boolean($('repairLibrary').closest('#settingsMaintenance'));
            await __click('#repairLibrary'); await __tick(20);
            return {inMaintenance, page: ui.page, summary: __text('#repairSummary'), enabled: !$('repairLibrary').disabled,
                    expected: `${t('uiC.repair.staleSubtitles')}: 2 • ${t('uiC.repair.jobsCreated')}: 1`};
            """,
            lang=lang,
            before="__api.repair_library = async () => ({result: {stale_subtitles: 2, jobs_created: 1}, state: await __api.get_state()});",
        )
        out = result.value
        assert out["inMaintenance"] is True and out["page"] == "settings" and out["enabled"] is True
        assert out["summary"] == out["expected"]
        assert result.calls_of("repair_library") == [[]]
    assert en("settings.subtitleFolder") == "Subtitle Inbox"


def test_episode_diagnostics_localize_persisted_prepare_output_to_english(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.config.ui.language = "en"
    manager.db.upsert_anime(LibraryAnime(media_id=18, title="Daemons of the Shadow Realm"))
    video = tmp_path / "library" / "Daemons of the Shadow Realm - 18.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(
            media_id=18,
            title="Daemons of the Shadow Realm",
            episode=18,
            video_path=video,
            state="waiting_subtitles",
        )
    )
    manager.db.queue_subtitle_job(
        video,
        18,
        18,
        error=(
            "Anime: 'Daemons of the Shadow Realm' season=1 episode=18\n"
            "AniList: обновление прогресса отключено для этого запуска\n"
            "Jimaku: файл нужной серии не найден с достаточной уверенностью\n"
            "Японские субтитры пока не найдены\n"
            "PREPARE_STATUS=waiting_subtitles"
        ),
    )

    result = manager.diagnose_episode(18, 18)
    job = next(item for item in result["checks"] if item["key"] == "job")

    assert "AniList: progress updates disabled for this run" in job["detail"]
    assert "Jimaku: no sufficiently confident file found for the requested episode" in job["detail"]
    assert "Japanese subtitles not found yet" in job["detail"]
    assert "PREPARE_STATUS=waiting_subtitles" in job["detail"]
    assert not any("а" <= char.casefold() <= "я" for char in job["detail"])


def test_episode_diagnostics_keep_prepare_output_russian_in_russian_ui(tmp_path: Path) -> None:
    manager = make_manager(tmp_path)
    manager.config.ui.language = "ru"
    manager.db.upsert_anime(LibraryAnime(media_id=19, title="Test"))
    video = tmp_path / "library" / "Test - 01.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(media_id=19, title="Test", episode=1, video_path=video, state="waiting_subtitles")
    )
    manager.db.queue_subtitle_job(
        video, 19, 1, error="Японские субтитры пока не найдены\nPREPARE_STATUS=waiting_subtitles"
    )

    result = manager.diagnose_episode(19, 1)
    job = next(item for item in result["checks"] if item["key"] == "job")

    assert "Японские субтитры пока не найдены" in job["detail"]
