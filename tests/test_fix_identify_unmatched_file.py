"""Identify… a file Pudge could not match to an AniList entry (review SCEN-6).

"[SubsPlease] Dorohedoro - OVA" in the person's own batch folder became a title of its own
(media_id and episode empty): its Home card did nothing ("Why not ready?" needed an AniList
id) and nothing in the window could attach the file to an entry. Now "Why not ready?" opens
from the file for such a card and offers "Identify…": an AniList search (titles of the local
list first, they work while AniList refuses requests) and an episode; the choice is stored
with the file, later scans keep it, and its subtitles are prepared for that entry.
Pages run in the node harness (tests/page_harness.py)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pudge.library import user_episode_identity
from pudge.manager_models import LibraryAnime
from test_web_app import make_api
from tests.i18n_support import en, ru

SUB = "1\n00:00:01,000 --> 00:00:03,000\nこんにちは。\n"
OVA_ENTRY = {
    "media_id": 112233,
    "media_kind": "anime",
    "title": "Dorohedoro: Ma no Omake",
    "native_title": "ドロヘドロ 魔のおまけ",
    "format": "OVA",
    "episodes": 6,
    "year": 2020,
    "cover": "javascript:alert(1)",
    "site_url": "https://anilist.co/anime/112233",
    "media_status": "FINISHED",
}


def _library_with_ova(tmp_path: Path):
    api = make_api(tmp_path)
    db = api.manager.db
    db.upsert_anime(
        LibraryAnime(
            media_id=38668,
            title="Dorohedoro",
            status="COMPLETED",
            media_status="FINISHED",
            episodes=12,
            format="TV",
        )
    )
    folder = api.config.library.root_dir / "[SubsPlease] Dorohedoro (01-12) (1080p) [Batch]"
    folder.mkdir(parents=True)
    for number in range(1, 3):
        video = folder / f"[SubsPlease] Dorohedoro - {number:02d} (1080p).mkv"
        video.write_bytes(b"video")
        video.with_suffix(".ja.srt").write_text(SUB, encoding="utf-8")
    ova = folder / "[SubsPlease] Dorohedoro - OVA (1080p).mkv"
    ova.write_bytes(b"video")
    api.manager.scan_library()
    return api, ova.resolve()


def test_a_file_without_an_entry_is_diagnosed_and_identified(tmp_path: Path) -> None:
    api, ova = _library_with_ova(tmp_path)
    db = api.manager.db
    row = db.episode_by_path(ova)
    assert (row.media_id, row.episode) == (None, None)  # the case before Identify…

    data = api.diagnose_video_file(str(ova))
    assert data["media_id"] is None and data["video_path"] == str(ova)
    assert data["identify_allowed"] is True and data["ready"] is False
    assert data["checks"][0] == {
        "key": "anilist",
        "ok": False,
        "label": en("manager.diagAnilistNotMatched"),
        "detail": ova.name,
    }

    result = api.identify_episode_file(str(ova), OVA_ENTRY["media_id"], 1, OVA_ENTRY)
    assert result["ok"] is True and result["title"] == OVA_ENTRY["title"]
    row = db.episode_by_path(ova)
    assert (row.media_id, row.episode) == (112233, 1)
    anime = db.get_anime(112233)
    assert anime.title == OVA_ENTRY["title"] and anime.episodes == 6 and anime.format == "OVA"
    assert anime.status == "" and anime.cover_url == ""  # not on the list; no non-web cover
    jobs = [dict(job) for job in db.subtitle_jobs() if job["video_path"] == str(ova)]
    assert [(job["media_id"], job["episode"], job["state"]) for job in jobs] == [(112233, 1, "pending")]
    assert user_episode_identity(db, ova) == (112233, 1)

    # Later scans (cached or not) keep the person's choice.
    api.manager._scan_library_uncached()
    api.scan_library()
    row = db.episode_by_path(ova)
    assert (row.media_id, row.episode) == (112233, 1)
    # Its diagnostics now come from the entry, like any matched episode.
    again = api.diagnose_video_file(str(ova))
    assert again["media_id"] == 112233 and again["checks"][0]["ok"] is True


def test_identify_checks_the_entry_and_the_episode(tmp_path: Path) -> None:
    api, ova = _library_with_ova(tmp_path)
    assert api.identify_episode_file(str(ova), 0, 1) == {
        "ok": False,
        "error": en("error.identify.entryRequired"),
    }
    assert api.identify_episode_file(str(ova), 112233, None, OVA_ENTRY) == {
        "ok": False,
        "error": en("error.identify.episodeRequired"),
    }
    assert api.identify_episode_file(str(ova), 112233, 9, OVA_ENTRY) == {
        "ok": False,
        "error": en("error.identify.episodeOutOfRange", title=OVA_ENTRY["title"], episode=9),
    }
    missing = str(ova.parent / "gone.mkv")
    assert api.identify_episode_file(missing, 112233, 1) == {
        "ok": False,
        "error": en("error.identify.fileNotFound"),
    }
    # A one-episode entry needs no episode; a movie keeps none.
    single = {**OVA_ENTRY, "media_id": 5, "episodes": 1, "title": "Single OVA"}
    assert api.identify_episode_file(str(ova), 5, "", single)["episode"] == 1
    movie = {**OVA_ENTRY, "media_id": 6, "episodes": 1, "format": "MOVIE", "title": "Movie"}
    assert api.identify_episode_file(str(ova), 6, 3, movie)["episode"] is None
    # A file that is ready is not identified again from here.
    episode_one = next(row for row in api.manager.db.episodes(38668) if row.episode == 1)
    assert episode_one.state == "ready"
    assert api.identify_episode_file(str(episode_one.video_path), 112233, 1, OVA_ENTRY) == {
        "ok": False,
        "error": en("error.identify.fileReady"),
    }
    assert ru("error.identify.episodeRequired") == "Укажите серию этого файла"


def test_search_offers_local_titles_first_and_survives_an_anilist_error(tmp_path: Path) -> None:
    api, _ova = _library_with_ova(tmp_path)

    def refused(query):
        raise RuntimeError("AniList answered 403")

    api.planning_search_anilist = refused
    answer = api.identify_episode_search("Dorohedoro - OVA")
    assert [row["media_id"] for row in answer["rows"]] == [38668]
    assert answer["rows"][0]["in_library"] is True and answer["error"] == "AniList answered 403"

    api.planning_search_anilist = lambda query: [
        {**OVA_ENTRY},
        {**OVA_ENTRY, "media_id": 38668},  # already offered from the local list
        {**OVA_ENTRY, "media_id": 999, "media_kind": "manga", "title": "Dorohedoro (manga)"},
    ]
    answer = api.identify_episode_search("Dorohedoro")
    assert [(row["media_id"], row["in_library"]) for row in answer["rows"]] == [
        (38668, True),
        (112233, False),
    ]
    assert answer["error"] == ""


@pytest.mark.parametrize("lang", ["en", "ru"])
def test_the_home_card_opens_diagnostics_and_identify(page, lang: str) -> None:
    path = "/Users/me/Anime/[SubsPlease] Dorohedoro - OVA (1080p).mkv"
    waiting = {
        "media_id": None,
        "title": "Dorohedoro - OVA",
        "cover": "",
        "site_url": "",
        "next_episode": None,
        "local": {
            "episode": None,
            "video_path": path,
            "subtitle_path": "",
            "subtitle_source": "none",
            "state": "local",
        },
    }
    diagnosis = {
        "media_id": None,
        "title": "Dorohedoro - OVA",
        "episode": None,
        "ready": False,
        "video_path": path,
        "state": "local",
        "identify_allowed": True,
        "checks": [{"key": "anilist", "ok": False, "label": "x", "detail": "y"}],
    }
    result = page.run(
        r"""
        renderDataPages(true); await __tick(50);
        const card = document.querySelector('.waiting-card');
        const out = {siteUrl: card.dataset.siteUrl, path: card.dataset.videoPath};
        await __click('.waiting-card'); await __flush();
        out.title = __text('#modalTitle');
        out.buttons = [...document.querySelectorAll('#modalBody .diagnostic-actions button')].map(b => b.dataset.action);
        await __click('[data-action="identify-diagnostic"]'); await __flush();
        out.query = document.getElementById('identifyQuery').value;
        out.rows = [...document.querySelectorAll('#identifyResults .identify-row .title')].map(n => n.textContent);
        await __input('#identifyEpisode', '1');
        await __click('[data-action="identify-pick"][data-index="1"]'); await __flush(); await __tick(20);
        out.toast = __toasts().at(-1);
        out.expected = {toast: PudgeI18n.t('toast.fileIdentified', {title: 'Dorohedoro: Ma no Omake'}),
                        title: `${t('action.whyNotReady')} — Dorohedoro - OVA`, identify: t('action.identifyFile')};
        out.identifyLabel = __text('[data-action="identify-diagnostic"]') || null;
        return out;
        """,
        lang=lang,
        state={"home": {"waiting": [waiting]}},
        before=(
            "window.diagnosis = " + json.dumps(diagnosis) + ";"
            "__api.diagnose_video_file = async () => ({...window.diagnosis});"
            "__api.identify_episode_search = async () => ({rows: ["
            "{media_id: 38668, title: 'Dorohedoro', format: 'TV', episodes: 12, in_library: true},"
            "{media_id: 112233, title: 'Dorohedoro: Ma no Omake', format: 'OVA', episodes: 6, in_library: false}], error: ''});"
            "__api.identify_episode_file = async () => { window.diagnosis = {...window.diagnosis, media_id: 112233,"
            " identify_allowed: true}; return {ok: true, media_id: 112233, episode: 1,"
            " title: 'Dorohedoro: Ma no Omake', state: ui.state}; };"
        ),
    )
    out = result.value
    assert out["siteUrl"] == "" and out["path"] == path  # no https://anilist.co/anime/null
    assert out["title"] == out["expected"]["title"]
    assert "identify-diagnostic" in out["buttons"] and "find-diagnostic-release" not in out["buttons"]
    assert out["query"] == "Dorohedoro - OVA"
    assert out["rows"] == ["Dorohedoro", "Dorohedoro: Ma no Omake"]
    assert result.calls_of("diagnose_video_file")[0] == [path]
    [[video, media_id, episode, entry]] = result.calls_of("identify_episode_file")
    assert (video, media_id, episode, entry["title"]) == (path, 112233, 1, "Dorohedoro: Ma no Omake")
    assert out["toast"] == out["expected"]["toast"]
    assert len(result.calls_of("diagnose_video_file")) == 2  # the dialog reopens for the new identity
