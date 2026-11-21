""" "Use subtitle file as is" next to "Choose subtitle file" (review SCEN-11).

A file picked with "Choose subtitle file" is aligned again (prepare-only runs
without --fast-play), which can shift a hand-fixed or already synced subtitle or
reject it to couldn't-sync. The new choice plays the chosen text file unchanged:
the episode is ready at once with the copy, no preparation job runs, and like
any manual pick it is left alone by automatic upgrades. "Choose subtitle file"
still aligns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pudge.manager import ManagerError
from pudge.manager_models import LibraryAnime, LibraryEpisode
from tests.test_v0617_workflow_tools import make_manager

SRT = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは\n\n2\n00:00:05,500 --> 00:00:07,000\nさようなら\n"


def _episode(tmp_path: Path):
    manager = make_manager(tmp_path)
    manager.db.upsert_anime(LibraryAnime(media_id=10, title="Example"))
    video = tmp_path / "library" / "Example - 01.mkv"
    video.write_bytes(b"video")
    manager.db.upsert_episode(
        LibraryEpisode(media_id=10, title="Example", episode=1, video_path=video, state="couldnt_sync")
    )
    subtitle = tmp_path / "Downloads" / "hand fixed.srt"
    subtitle.parent.mkdir()
    subtitle.write_text(SRT, encoding="utf-8")
    return manager, video, subtitle


def test_subtitle_used_as_is_is_ready_without_a_preparation_job(tmp_path: Path) -> None:
    manager, video, subtitle = _episode(tmp_path)
    manager.set_manual_subtitle(video, subtitle)  # an earlier "align it" choice is replaced
    assert manager.db.subtitle_jobs()

    used = manager.use_manual_subtitle_as_is(video, subtitle)

    assert used.read_bytes() == subtitle.read_bytes()  # byte for byte, timing untouched
    assert used.is_relative_to(manager.config.paths.cache_dir / "manual-subtitles")
    subtitle.unlink()  # the copy survives the original
    episode = manager.db.episode_by_path(video)
    assert episode.state == "ready"
    assert episode.subtitle_path == used and episode.subtitle_origin == "manual"
    assert manager.db.subtitle_jobs() == []
    assert manager.db.get_state(manager._manual_subtitle_state_key(video)) == ""
    latest = manager.db.latest_selected_subtitle(video)
    assert latest["source"] == "manual" and latest["details"]["as_is"] is True
    assert latest["details"]["final_path"] == str(used)
    assert manager.process_subtitle_jobs(limit=5) == 0
    assert manager.db.episode_by_path(video).subtitle_path == used


def test_only_text_subtitles_can_be_used_as_is(tmp_path: Path) -> None:
    manager, video, _subtitle = _episode(tmp_path)
    for name in ("image.sup", "vobsub.sub", "notes.txt"):
        other = tmp_path / "Downloads" / name
        other.write_bytes(b"PG")
        with pytest.raises(ManagerError) as error:
            manager.use_manual_subtitle_as_is(video, other)
        assert "SRT, ASS, SSA" in str(error.value), name
    assert manager.db.episode_by_path(video).subtitle_path is None
    for name in ("a.ass", "b.ssa", "c.vtt"):
        accepted = tmp_path / "Downloads" / name
        accepted.write_text(SRT, encoding="utf-8")
        assert manager.use_manual_subtitle_as_is(video, accepted).suffix == accepted.suffix


def test_web_api_uses_the_file_as_is_without_preparing(tmp_path: Path) -> None:
    from pudge.web_app import WebAppApi

    manager, video, subtitle = _episode(tmp_path)
    api = WebAppApi.__new__(WebAppApi)
    api.manager = manager
    api.config = manager.config
    api.logger = manager.logger
    api.get_state = lambda: {"state": True}
    dialogs: list[tuple] = []

    class Window:
        def create_file_dialog(self, *args, **kwargs):
            dialogs.append(kwargs["file_types"])
            return [str(subtitle)]

    api.window = Window()
    manager.process_subtitle_jobs = lambda **_k: pytest.fail("as is: nothing to prepare")
    result = api.choose_manual_subtitle(str(video), True)
    assert result["ok"] is True and result["as_is"] is True
    assert ".sup" not in dialogs[0][0]
    assert manager.db.episode_by_path(video).subtitle_origin == "manual"


def test_diagnostics_offer_both_choices(page, tmp_path: Path) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            ui.diagnosticContext = {mediaId: 12, episode: 3, anime: {media_id: 12, title: 'Show'},
                                    data: {video_path: '/videos/Show - 03.mkv', ready: false, checks: []}};
            $('modalBody').innerHTML = diagnosticHtml(ui.diagnosticContext.data);
            const align = document.querySelector('#modalBody [data-action="choose-diagnostic-subtitle"]');
            const asIs = document.querySelector('#modalBody [data-action="choose-diagnostic-subtitle-as-is"]');
            const out = {align: align?.textContent, alignTitle: align?.title, asIs: asIs?.textContent, asIsTitle: asIs?.title,
                         expected: [t('action.chooseSubtitle'), t('action.chooseSubtitleHint'),
                                    t('action.useSubtitleAsIs'), t('action.useSubtitleAsIsHint')]};
            await __click('#modalBody [data-action="choose-diagnostic-subtitle-as-is"]'); await __tick(20);
            return out;
            """,
            lang=lang,
            api={
                "choose_manual_subtitle": {"ok": True, "as_is": True},
                "diagnose_episode": {"ready": True, "checks": []},
            },
        )
        out = result.value
        assert [out["align"], out["alignTitle"], out["asIs"], out["asIsTitle"]] == out["expected"]
        assert all(out["expected"]) and len(set(out["expected"])) == 4
        assert result.calls_of("choose_manual_subtitle") == [["/videos/Show - 03.mkv", True]]
