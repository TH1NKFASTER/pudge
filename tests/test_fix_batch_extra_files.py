"""Extra files of a batch (Dorohedoro's SubsPlease batch brought "Dorohedoro - OVA").

Pudge matched the OVA to the TV series without an episode number and searched subtitles for
it once a day forever ("Waiting for subs" on Home). An extra file of a download (an OVA,
special or movie in a TV batch, an NCOP/NCED) now goes to Action required: delete it or
keep it in the library.
"""

from __future__ import annotations

import json
from pathlib import Path

from pudge.manager_models import DownloadItem, LibraryAnime, LibraryEpisode
from test_web_app import make_api

MEDIA = 105228


def _no_subtitles(monkeypatch) -> None:
    monkeypatch.setattr("pudge.manager.japanese_subtitle_source", lambda *a, **k: ("none", None))


def _no_prepare(monkeypatch) -> None:
    def refuse(*args, **_kwargs):
        raise AssertionError(f"subtitle preparation started for {args[0]!r}")

    monkeypatch.setattr("pudge.manager.subprocess.Popen", refuse)


def _series(db, *, progress: int = 0) -> None:
    db.upsert_anime(
        LibraryAnime(
            media_id=MEDIA,
            title="Dorohedoro",
            status="CURRENT",
            episodes=3,
            media_status="FINISHED",
            progress=progress,
            format="TV",
        )
    )


def _batch(tmp_path: Path, names: list[str]) -> tuple[Path, DownloadItem]:
    folder = tmp_path / "[SubsPlease] Dorohedoro (01-03) (1080p) [Batch]"
    folder.mkdir()
    for name in names:
        (folder / name).write_bytes(b"video")
    return folder, _batch_item(folder)


def _batch_item(folder: Path) -> DownloadItem:
    return DownloadItem(
        torrent_hash="f9850c20",
        name=folder.name,
        state="uploading",
        progress=1.0,
        save_path=str(folder.parent),
        content_path=str(folder),
        media_id=MEDIA,
        is_batch=True,
        completed_on=1,
    )


def test_extra_files_of_a_batch_go_to_action_required(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    manager, db = api.manager, api.manager.db
    _series(db)
    _no_subtitles(monkeypatch)
    folder, item = _batch(
        tmp_path,
        [
            "[SubsPlease] Dorohedoro - 01 (1080p).mkv",
            "[SubsPlease] Dorohedoro - 02 (1080p).mkv",
            "[SubsPlease] Dorohedoro - OVA (1080p).mkv",
            "[SubsPlease] Dorohedoro - OVA 02 (1080p).mkv",
            "[SubsPlease] Dorohedoro - NCOP (1080p).mkv",
        ],
    )
    manager._register_completed_download(item)
    episodes = {row.video_path.name: row.episode for row in db.episodes(MEDIA)}
    # "OVA 02" is not episode 2 of the series.
    assert episodes == {
        "[SubsPlease] Dorohedoro - 01 (1080p).mkv": 1,
        "[SubsPlease] Dorohedoro - 02 (1080p).mkv": 2,
        "[SubsPlease] Dorohedoro - OVA (1080p).mkv": None,
        "[SubsPlease] Dorohedoro - OVA 02 (1080p).mkv": None,
        "[SubsPlease] Dorohedoro - NCOP (1080p).mkv": None,
    }
    extras = [path for path in sorted(folder.iterdir()) if "OVA" in path.name or "NCOP" in path.name]
    _no_prepare(monkeypatch)
    manager.process_subtitle_jobs(limit=10, preferred_paths=[p.resolve() for p in extras], wait_for_slot=True)
    jobs = {Path(row["video_path"]).name: (row["state"], row["action_code"]) for row in db.subtitle_jobs()}
    for path in extras:
        assert jobs[path.name] == ("needs_action", "review_extra_file")
    # A second poll of the same download does not re-number them or ask again from scratch.
    manager._register_completed_download(item)
    assert {row.video_path.name: row.episode for row in db.episodes(MEDIA)} == episodes
    jobs = {Path(row["video_path"]).name: row["state"] for row in db.subtitle_jobs()}
    assert [jobs[path.name] for path in extras] == ["needs_action"] * len(extras)


def _series_with_ova(tmp_path: Path, monkeypatch):
    api = make_api(tmp_path)
    db = api.manager.db
    _series(db, progress=2)
    _no_subtitles(monkeypatch)
    folder, item = _batch(
        tmp_path,
        [
            "[SubsPlease] Dorohedoro - 01 (1080p).mkv",
            "[SubsPlease] Dorohedoro - 02 (1080p).mkv",
            "[SubsPlease] Dorohedoro - 03 (1080p).mkv",
            "[SubsPlease] Dorohedoro - OVA (1080p).mkv",
        ],
    )
    api.manager._register_completed_download(item)
    for number in (1, 2, 3):
        video = (folder / f"[SubsPlease] Dorohedoro - {number:02d} (1080p).mkv").resolve()
        subtitle = folder / f"{number}.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nドロヘドロ\n", encoding="utf-8")
        db.upsert_episode(
            LibraryEpisode(
                media_id=MEDIA,
                title="Dorohedoro",
                episode=number,
                video_path=video,
                subtitle_path=subtitle.resolve(),
                subtitle_origin="jimaku",
                state="watched" if number < 3 else "ready",
                torrent_hash=item.torrent_hash,
            )
        )
        db.delete_subtitle_job(video)
    ova = (folder / "[SubsPlease] Dorohedoro - OVA (1080p).mkv").resolve()
    _no_prepare(monkeypatch)
    api.manager.process_subtitle_jobs(limit=5, preferred_paths=[ova], wait_for_slot=True)
    return api, folder, ova


def _home_titles(api) -> dict[str, list[str]]:
    home = api.get_state()["home"]
    return {key: [card.get("title") for card in rows] for key, rows in home.items() if rows}


def _ova_graph(db) -> None:
    db.store_relation_graph(
        {
            "root_id": MEDIA,
            "nodes": [
                {"media_id": MEDIA, "title": "Dorohedoro", "format": "TV", "episodes": 3},
                {"media_id": 110000, "title": "Dorohedoro: Ma no Omake", "format": "OVA", "episodes": 6},
                {"media_id": 110001, "title": "Dorohedoro (manga)", "format": "MANGA"},
            ],
            "edges": [
                {"source": MEDIA, "target": 110000, "relation_type": "SIDE_STORY"},
                {"source": MEDIA, "target": 110001, "relation_type": "ADAPTATION"},
            ],
        },
        refreshed_at=1,
        next_refresh_at=9999999999,
    )


def _ova_job(api, ova: Path):
    return next(row for row in api.manager.db.subtitle_jobs() if Path(row["video_path"]) == ova)


def test_home_asks_about_the_extra_file_and_keep_gives_it_its_own_title(tmp_path: Path, monkeypatch) -> None:
    api, _folder, ova = _series_with_ova(tmp_path, monkeypatch)
    _ova_graph(api.manager.db)
    home = api.get_state()["home"]
    [card] = home["needs_action"]
    assert (card["title"], card["action_code"]) == ("Dorohedoro", "review_extra_file")
    assert card["extra_file"] == {"name": ova.name, "kind": "OVA"}
    assert "waiting" not in _home_titles(api)

    assert api.extra_file_keep(str(ova))["ok"] is True
    assert ova.is_file()
    # The series' only related OVA: the file is that title now, waiting for its subtitles.
    row = api.manager.db.episode_by_path(ova)
    assert (row.media_id, row.episode) == (110000, None)
    job = _ova_job(api, ova)
    assert (job["state"], job["media_id"]) == ("pending", 110000)
    titles = _home_titles(api)
    assert "needs_action" not in titles
    assert titles["waiting"] == ["Dorohedoro: Ma no Omake"]
    assert "Dorohedoro" in titles.get("new_ready", []) + titles.get("completed_ready", [])
    # A later registration of the same batch does not take it back to the series.
    api.manager._register_completed_download(_batch_item(_folder))
    assert api.manager.db.episode_by_path(ova).media_id == 110000


def test_kept_extra_without_a_related_entry_is_a_title_of_its_own(tmp_path: Path, monkeypatch) -> None:
    api, _folder, ova = _series_with_ova(tmp_path, monkeypatch)
    assert api.extra_file_keep(str(ova))["ok"] is True
    row = api.manager.db.episode_by_path(ova)
    assert row.media_id is None
    assert _home_titles(api)["waiting"] == ["Dorohedoro - OVA"]


def test_kept_ncop_stays_in_the_library_only(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    _series(api.manager.db, progress=0)
    _no_subtitles(monkeypatch)
    folder, item = _batch(
        tmp_path, ["[SubsPlease] Dorohedoro - 01 (1080p).mkv", "[SubsPlease] Dorohedoro - NCOP (1080p).mkv"]
    )
    api.manager._register_completed_download(item)
    ncop = (folder / "[SubsPlease] Dorohedoro - NCOP (1080p).mkv").resolve()
    assert api.extra_file_keep(str(ncop))["ok"] is True
    # A manual Refresh retries every waiting file: the kept NCOP stays quiet.
    api.manager.db.force_requeue_unresolved_subtitle_jobs()
    _no_prepare(monkeypatch)
    api.manager.process_subtitle_jobs(limit=5, preferred_paths=[ncop], wait_for_slot=True)
    job = _ova_job(api, ncop)
    assert (job["state"], job["action_code"]) == ("needs_action", "extra_file_kept")
    assert all("NCOP" not in title for rows in _home_titles(api).values() for title in rows)


def test_a_downloaded_extra_is_flagged_as_soon_as_the_download_completes(tmp_path: Path, monkeypatch) -> None:
    api = make_api(tmp_path)
    _series(api.manager.db)
    _no_subtitles(monkeypatch)
    folder, item = _batch(tmp_path, ["[SubsPlease] Dorohedoro - OVA (1080p).mkv"])
    api.manager._register_completed_download(item)  # no subtitle job has run
    ova = (folder / "[SubsPlease] Dorohedoro - OVA (1080p).mkv").resolve()
    job = _ova_job(api, ova)
    assert (job["state"], job["action_code"]) == ("needs_action", "review_extra_file")
    [card] = api.get_state()["home"]["needs_action"]
    assert card["action_code"] == "review_extra_file"


def test_delete_removes_only_a_flagged_extra_file(tmp_path: Path, monkeypatch) -> None:
    api, folder, ova = _series_with_ova(tmp_path, monkeypatch)
    episode = (folder / "[SubsPlease] Dorohedoro - 03 (1080p).mkv").resolve()
    assert api.extra_file_delete(str(episode))["ok"] is False
    assert episode.is_file()

    assert api.extra_file_delete(str(ova))["ok"] is True
    assert not ova.exists() and episode.is_file()
    assert api.manager.db.episode_by_path(ova) is None
    assert "needs_action" not in _home_titles(api)


def test_extra_file_card_offers_delete_and_keep(page) -> None:
    path = "/lib/Dorohedoro/[SubsPlease] Dorohedoro - OVA (1080p).mkv"
    card = {
        "media_id": MEDIA,
        "title": "Dorohedoro",
        "action_code": "review_extra_file",
        "extra_file": {"name": Path(path).name, "kind": "OVA"},
        "local": {"episode": None, "video_path": path, "state": "waiting_subtitles"},
    }
    for lang, note in (("en", "Extra OVA came with the download"), ("ru", "С загрузкой пришло лишнее: OVA")):
        result = page.run(
            r"""
            renderCurrent();
            const note = __text('.extra-file-card .extra-file-note');
            const buttons = [...document.querySelectorAll('.extra-file-card .extra-file-actions button')].map(b => b.innerText);
            await __click('[data-action="extra-file-delete"]'); await __tick(20);
            await __click('[data-pudge-confirm="ok"]'); await __tick(20);
            renderCurrent();
            await __click('[data-action="extra-file-keep"]'); await __tick(20);
            return {note, buttons, toasts: __toasts()};
            """,
            lang=lang,
            state={"home": {"needs_action": [card]}},
            api={"extra_file_delete": {"ok": True}, "extra_file_keep": {"ok": True}},
        )
        assert result.value["note"] == note
        assert len(result.value["buttons"]) == 2
        assert result.calls_of("extra_file_delete") == [[path]]
        assert result.calls_of("extra_file_keep") == [[path]]
        assert json.dumps(result.value["toasts"], ensure_ascii=False)


def test_extra_file_card_note_wraps_and_keep_is_green() -> None:
    from css_support import css_rules

    rules = css_rules("settings.css")
    note = rules.declarations(".extra-file-card .extra-file-note")
    assert (note["white-space"], note["text-overflow"]) == ("normal", "clip")
    assert (
        rules.declarations('.extra-file-actions [data-action="extra-file-keep"]')["border-color"]
        == "var(--good)"
    )
