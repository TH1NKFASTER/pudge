"""iPhone/iPad Anime: Caught up (and Waiting on the Mac) list the Mac Home's titles.

GET media/anime only listed titles with ready episodes, so the phone's Caught up showed a few
titles while the Mac's listed every watching title the user is up to date with. The payload now
carries the Mac Home snapshot's Caught up / Needs action / Waiting / Available to download cards
under an optional ``home`` key (``items`` unchanged); PudgeCore AnimeShelves arranges them.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from source_policy import source

from pudge.database import Database

CORE = "ios/PudgeCore/Sources/PudgeCore/LibraryCards.swift"
CONTRACTS = "ios/PudgeCore/Sources/PudgeCore/MediaContracts.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/LibraryCardsTests.swift"
WATCH = "ios/Pudge/UI/WatchScreen.swift"
MODEL = "ios/Pudge/Features/Media/MediaModel.swift"


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def _api(home, library=()):
    return SimpleNamespace(
        light_novels=SimpleNamespace(
            settings=lambda: SimpleNamespace(study_backend="jiten", review_mode="binary")
        ),
        manager=SimpleNamespace(
            db=SimpleNamespace(
                anime_list=lambda: list(library), personal_release_schedule=lambda media: False
            )
        ),
        _personal_schedule_snapshot=lambda anime: {"enabled": False},
        get_state_fast=home if callable(home) else (lambda: {"home": home}),
    )


def _ready_title(service, tmp_path, media_id, title):
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,episodes,updated_at) VALUES(?,?,'CURRENT',0,12,0)",
            (media_id, title),
        )
        video = tmp_path / f"{media_id}.mkv"
        video.write_bytes(b"x")
        subtitle = tmp_path / f"{media_id}.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
        conn.execute(
            "INSERT INTO episodes(media_id,title,episode,video_path,subtitle_path,state,updated_at) VALUES(?,?,1,?,?,'ready',0)",
            (media_id, "Ep 1", str(video), str(subtitle)),
        )


HOME = {
    "new_ready": [{"media_id": 1, "title": "Ready"}],
    "needs_action": [
        {"media_id": 7, "title": "Odd file", "next_episode": 2, "presentation": {"status": "ready"}}
    ],
    "waiting": [
        {
            "media_id": 5,
            "title": "Downloading",
            "progress": 2,
            "episodes": 12,
            "next_episode": 3,
            "local": None,
            "presentation": {"status": "downloading", "progress_percent": 42},
        },
        {"media_id": None, "title": "Loose file", "presentation": {"status": "waiting_subtitles"}},
        {
            "media_id": 6,
            "title": "Subs",
            "next_episode": 4,
            "local": {"episode": 4, "state": "waiting_subtitles"},
            "presentation": {"status": "waiting_subtitles"},
        },
    ],
    "download_available": [
        {"media_id": 8, "title": "Old show", "next_episode": 1, "presentation": {"status": "missing"}}
    ],
    "caught_up": [
        {
            "media_id": 3,
            "title": "Airing",
            "progress": 7,
            "episodes": 12,
            "next_airing_episode": 8,
            "next_airing_at": 1_800_000_000,
            "personal_schedule": {"enabled": False},
        },
        {
            "media_id": 4,
            "title": "Weekly",
            "progress": 2,
            "episodes": 24,
            "next_airing_episode": 20,
            "next_airing_at": 1_700_000_000.0,
            "personal_schedule": {"enabled": True, "status": "caught_up", "next_episode": 3},
        },
        {"media_id": 9, "title": "No date", "progress": 5, "episodes": None, "next_airing_at": None},
        {"media_id": 3, "title": "Airing again"},
        {"kind": "watch_sequence", "items": [{"media_id": 10}]},
        "junk",
    ],
}


def test_anime_payload_carries_the_mac_home_caught_up_and_waiting_cards(service, tmp_path):
    _ready_title(service, tmp_path, 1, "Ready")
    service.api = _api(HOME)
    answer = service.media.anime()
    # The existing groups stay as they were.
    assert [group["title"] for group in answer["items"]] == ["Ready"]
    home = answer["home"]
    assert [card["mediaId"] for card in home["caughtUp"]] == [3, 4, 9]
    airing, weekly, undated = home["caughtUp"]
    assert airing == {
        "mediaId": 3,
        "title": "Airing",
        "progress": 7,
        "total": 12,
        "section": "caught_up",
        "status": "",
        "nextEpisode": 8,
        "nextAt": 1_800_000_000.0,
        "downloadFraction": None,
        "downloadKind": None,
        "queuedRelease": False,
        "actionCode": None,
        "extraKinds": [],
    }
    # A personal schedule: its next episode and unlock (the Mac put the unlock in next_airing_at).
    assert (weekly["nextEpisode"], weekly["nextAt"], weekly["total"]) == (3, 1_700_000_000.0, 24)
    assert (undated["nextEpisode"], undated["nextAt"], undated["total"]) == (None, None, None)
    # Needs action, Waiting for preparation, Available to download: the Mac's order; no media id: skipped.
    waiting = home["waiting"]
    assert [(card["mediaId"], card["section"], card["status"]) for card in waiting] == [
        (7, "needs_action", "needs_action"),
        (5, "waiting", "downloading"),
        (6, "waiting", "waiting_subtitles"),
        (8, "download_available", "missing"),
    ]
    assert waiting[1]["downloadFraction"] == pytest.approx(0.42) and waiting[1]["nextEpisode"] == 3
    assert waiting[2]["nextEpisode"] == 4 and waiting[2]["downloadFraction"] is None


def test_no_home_key_without_a_mac_home_snapshot(service, tmp_path):
    _ready_title(service, tmp_path, 1, "Ready")
    for home in (lambda: 1 / 0, lambda: {}, lambda: {"home": None}):
        service.api = _api(home)
        answer = service.media.anime()
        assert "home" not in answer and [group["title"] for group in answer["items"]] == ["Ready"]
    service.api = _api({})
    assert service.media.anime()["home"] == {"caughtUp": [], "waiting": []}


def _swift(relative: str) -> str:
    return source(relative)


def _swift_fields(text: str, struct: str) -> list[str]:
    body = text.split(f"public struct {struct}:", 1)[1].split("\n}", 1)[0]
    return re.findall(r"^    public (?:let|var) (\w+): [^{\n]+$", body, re.M)


def test_swift_contract_matches_the_payload_keys(service):
    cards = service.media._home_shelves(HOME)
    contracts = _swift(CONTRACTS)
    assert set(_swift_fields(contracts, "MacHomeShelves")) == set(cards)
    assert set(_swift_fields(contracts, "MacHomeCard")) == set(cards["caughtUp"][0])
    # New keys are optional for an older Mac.
    assert "let home: MacHomeShelves?" in _swift(MODEL)


def test_swift_anime_tab_uses_the_mac_shelves():
    model = _swift(MODEL)
    assert "macHome = list.home" in model and "macHome = nil" in model
    watch = _swift(WATCH)
    # The Mac's shelves, with a watch on this device counted at once (AnimeProgressOverlay).
    assert "AnimeShelves.arrange(groups, mac: media.shownMacHome)" in watch
    assert "AnimeProgressOverlay.apply(macHome," in model
    assert "MacHomeAnimeCard(media: media, card: card" in watch
    assert (
        "AnimeGridCard(media: media, launcher: launcher, group: group, width: layout.card, mac: entry.mac)"
        in watch
    )
    # A Mac-only title opens its page with an empty list and the reason.
    assert "media.macHomeCard(value.mediaId)" in watch and '"No episodes ready yet"' in watch
    core = _swift(CORE)
    assert "case continueWatching, newReady, completedReady, waitingOnMac, caughtUp" in core
    assert 'case .waitingOnMac: return "Waiting on the Mac"' in core
    tests = _swift(CORE_TESTS)
    for name in (
        "testCaughtUpListsEveryMacTitleInTheMacOrder",
        "testOlderMacKeepsTheGroupsWithNothingLeftAsCaughtUp",
        "testMacHomeCardStatus",
    ):
        assert f"func {name}()" in tests
