"""iPhone/iPad library cards say what the Mac's cards say, and fit.

The phone invented its own card texts ("Watched 3 of 25", "Next: ep 8 · <date>", "Ep 4
downloading on the Mac · 42%", "· preparing subtitles on the Mac", "Chapter 3 of 12",
"Audiobook linked", …) that were cut or wrapped badly in the small cards. PudgeCore
(CardStatusText / MacCardText, with XCTest in LibraryCardsTests.swift) now ports the Mac
card lines with the Mac's English wording (pudge/web/locales.en.js); the Mac sends the few
facts the phone lacked (the download's state, the Action required reason, a movie flag,
a light novel's characters and year).
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest
from source_policy import source

from pudge.database import Database

CORE = "ios/PudgeCore/Sources/PudgeCore/LibraryCards.swift"
CORE_TESTS = "ios/PudgeCore/Tests/PudgeCoreTests/LibraryCardsTests.swift"
CONTRACTS = "ios/PudgeCore/Sources/PudgeCore/MediaContracts.swift"
GRID = "ios/Pudge/UI/LibraryGrid.swift"
WATCH = "ios/Pudge/UI/WatchScreen.swift"
READ = "ios/Pudge/UI/ReadScreen.swift"
LIBRARY = "ios/Pudge/UI/LibraryScreen.swift"


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def _locales() -> dict:
    text = source("pudge/web/locales.en.js")
    return json.loads(text[text.index("= {") + 2 :].rstrip().rstrip(";"))


# ------------------------------------------------------------------ the Mac's payload


def test_waiting_cards_carry_the_download_state_and_the_action_reason(service):
    home = {
        "needs_action": [
            {"media_id": 1, "title": "Jimaku", "action_code": "configure_jimaku"},
            {
                "media_id": 2,
                "title": "Extras",
                "action_code": "review_extra_file",
                "extra_files": [{"kind": "OP"}, {"kind": "OP"}, {"kind": "ED"}],
            },
            {
                "media_id": 3,
                "title": "One extra",
                "action_code": "review_extra_file",
                "extra_file": {"kind": ""},
            },
        ],
        "waiting": [
            {
                "media_id": 4,
                "title": "Paused",
                "presentation": {"status": "downloading", "progress_percent": 10},
                "download": {"state": "pausedDL", "progress": 0.42},
            },
            {"media_id": 5, "title": "Raw", "local": {"episode": 2, "state": "couldnt_sync"}},
        ],
        "download_available": [
            {
                "media_id": 6,
                "title": "Get",
                "progress": 3,
                "next_episode": 4,
                "download": {"state": "downloading", "progress": 0.5},
            },
            {"media_id": 7, "title": "Off", "queued_release": {"title": "x"}},
            {"media_id": 8, "title": "Done", "download": {"state": "seeding", "progress": 1}},
            {"media_id": 9, "title": "Broken", "download": {"state": "error", "progress": 0.2}},
        ],
    }
    cards = {card["mediaId"]: card for card in service.media._home_shelves(home)["waiting"]}
    assert (cards[1]["status"], cards[1]["actionCode"], cards[1]["extraKinds"]) == (
        "needs_action",
        "configure_jimaku",
        [],
    )
    assert cards[2]["extraKinds"] == ["OP", "OP", "ED"] and cards[3]["extraKinds"] == ["OVA"]
    # The torrent's own progress and state, as the Mac card shows them.
    assert cards[4]["downloadKind"] == "paused" and cards[4]["downloadFraction"] == pytest.approx(0.42)
    # No presentation: the episode's own state (the Mac card reads local.state then).
    assert cards[5]["status"] == "couldnt_sync" and cards[5]["downloadKind"] is None
    assert (cards[6]["downloadKind"], cards[6]["downloadFraction"]) == ("downloading", 0.5)
    assert cards[7]["queuedRelease"] is True and cards[7]["downloadKind"] is None
    assert cards[8]["downloadKind"] == "finished" and cards[9]["downloadKind"] == "error"
    # Torrents switched off in the Mac window: a running download is paused there.
    off = {c["mediaId"]: c for c in service.media._home_shelves(home, torrents_enabled=False)["waiting"]}
    assert off[6]["downloadKind"] == "paused" and off[8]["downloadKind"] == "finished"
    assert off[9]["downloadKind"] == "error"


def test_torrents_setting_comes_from_the_mac_state(service):
    api = SimpleNamespace(get_state_fast=lambda: {"settings": {"torrents_enabled": False}})
    assert service.media._torrents_enabled(api) is False
    assert service.media._torrents_enabled(SimpleNamespace(get_state_fast=lambda: 1 / 0)) is True
    assert service.media._torrents_enabled(SimpleNamespace(get_state_fast=lambda: {})) is True


def test_anime_groups_say_whether_the_title_is_a_movie(service, tmp_path):
    with service.db.connect() as conn:
        for media, title in [(21, "Film"), (22, "Show")]:
            conn.execute(
                "INSERT INTO anime(media_id,title,status,progress,episodes,updated_at) VALUES(?,?,'CURRENT',0,12,0)",
                (media, title),
            )
            video = tmp_path / f"{media}.mkv"
            video.write_bytes(b"x")
            subtitle = tmp_path / f"{media}.srt"
            subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
            conn.execute(
                "INSERT INTO episodes(media_id,title,episode,video_path,subtitle_path,state,updated_at) VALUES(?,?,1,?,?,'ready',0)",
                (media, "Ep 1", str(video), str(subtitle)),
            )
    library = [
        SimpleNamespace(
            media_id=21, title="Film", progress=0, episodes=1, media_status="FINISHED", format="MOVIE"
        ),
        SimpleNamespace(
            media_id=22, title="Show", progress=0, episodes=12, media_status="FINISHED", format="TV"
        ),
    ]
    service.api = SimpleNamespace(
        light_novels=SimpleNamespace(
            settings=lambda: SimpleNamespace(study_backend="jiten", review_mode="binary")
        ),
        manager=SimpleNamespace(
            db=SimpleNamespace(anime_list=lambda: library, personal_release_schedule=lambda media: False)
        ),
        _personal_schedule_snapshot=lambda anime: {"enabled": False},
        get_state_fast=lambda: {"home": {}},
    )
    movies = {group["title"]: group["movie"] for group in service.media.anime()["items"]}
    assert movies == {"Film": True, "Show": False}


def test_light_novels_carry_the_mac_cards_characters_and_year(service):
    with service.db.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ln_books(id INTEGER PRIMARY KEY,title TEXT,volume INT,current_chapter INT,"
            "current_offset REAL,finished INT,cover_url TEXT,updated_at REAL)"
        )
        conn.execute("INSERT INTO ln_books VALUES(1,'狼と香辛料 2',2,0,0,0,'',2)")
        conn.execute("INSERT INTO ln_books VALUES(2,'狼と香辛料 1',1,0,0,1,'',1)")
    service.api = SimpleNamespace(
        light_novels=SimpleNamespace(
            settings=lambda: SimpleNamespace(study_backend="jiten", review_mode="binary"),
            state=lambda: {
                "books": [
                    {"id": 1, "character_count": 123456, "anilist_year": 2006},
                    {"id": 2, "character_count": 0, "anilist_year": "junk"},
                ]
            },
        )
    )
    items = {item["id"]: item for item in service.media.light_novels()["items"]}
    assert (items[1]["characters"], items[1]["year"]) == (123456, 2006)
    assert (items[2]["characters"], items[2]["year"]) == (None, None)


def test_swift_contracts_have_the_new_optional_keys(service):
    contracts = source(CONTRACTS)
    card = _between(contracts, "public struct MacHomeCard:", "\n}")
    keys = set(service.media._home_shelves({"caught_up": [{"media_id": 1}]})["caughtUp"][0])
    assert set(re.findall(r"^    public let (\w+):", card, re.M)) == keys
    for field in (
        "downloadKind: String?",
        "queuedRelease: Bool?",
        "actionCode: String?",
        "extraKinds: [String]?",
    ):
        assert f"public let {field}" in card, field
    assert "public let movie: Bool?" in _between(contracts, "public struct AnimeGroup:", "\n}")
    novel = _between(contracts, "public struct LightNovelItem:", "\n}")
    assert "public let characters: Int?" in novel and "public let year: Int?" in novel


# ------------------------------------------------------------------ the phone's cards


def test_core_uses_the_mac_wording():
    core = source(CORE)
    locales = _locales()
    for key in (
        "label.upToDate",
        "label.readyAll",
        "label.readyMovie",
        "label.waitingSubs",
        "label.waitingTextSubs",
        "label.notReady",
        "home.couldnTSyncRawSubtitles",
        "home.waitingForJapaneseSpeechVerification",
        "home.downloadPaused",
        "home.waitingForDownload",
        "home.downloadError",
        "section.needsAction",
        "release.queuedTorrentsOff",
        "action.downloadNextEpisodes",
        "action.downloadAnime",
        "ui.configureJimaku",
        "ui.grantFolderAccess",
        "ui.enableOcr",
        "ui.openSettings",
        "ui.subtitleRetryExhausted",
        "media.allFinished",
        "torrent.state.waiting",
        "torrent.state.stalled",
        "torrent.state.unknown",
        "ui.paused",
    ):
        assert f'"{locales[key]}"' in core, key
    # Templates with placeholders, as the Swift interpolation writes them.
    for key, swift in (
        ("label.airingIn", '"Episode \\(episode) in \\(MacCardText.remaining(at - now))"'),
        ("label.continueEpisode", '"Episode \\(number) • resume at \\(at)"'),
        ("label.continueMovie", '"Movie • resume at \\(at)"'),
        ("label.downloading", '"Downloading: \\(percent(fraction))%"'),
        ("label.nextEpisodeNumber", '"Next: episode \\($0)"'),
        ("label.episodeNotReady", '"Episode \\($0) is not ready"'),
        ("label.readyEpisodes", '"Episodes \\(MacCardText.episodeRanges(numbers))"'),
        ("ln.continueVolume", '"Continue · vol. \\(volume)"'),
        ("media.timeLeft", '"\\(MacCardText.clock(remaining)) left"'),
        ("media.unfinished", '"\\(unfinished) unfinished"'),
        ("ui.extraFileFromDownload", '"Extra \\('),
        ("ui.extraFilesFromDownload", '"Extras came with the download: \\('),
    ):
        template = locales[key]
        assert swift in core, key
        assert re.sub(r"\{\w+\}", "", template).split()[0] in swift, key
    # The invented phrases are gone.
    for invented in (
        "on the Mac ·",
        "downloading on the Mac",
        "preparing subtitles on the Mac",
        "available to download on the Mac",
        '"Next: ep \\(',
        "Up to date",
        "Watched \\(",
        "Not started",
        "Chapter \\(",
        "Page \\(",
        '" · Downloaded"',
        "Not downloaded",
        "func watched(",
        "func shelf(",
    ):
        assert invented not in core, invented


def test_cards_have_no_invented_lines():
    watch = source(WATCH)
    grid_card = _between(watch, "struct AnimeGridCard: View", "struct PlanningCard: View")
    mac_card = _between(watch, "struct MacHomeAnimeCard: View", "extension MediaModel {")
    for card in (grid_card, mac_card):
        assert "Watched \\(" not in card and "subtitle:" not in card
        assert "now: now)" in card or "now: Date().timeIntervalSince1970)" in card
    assert (
        "ready: group.episodes.filter { !$0.watched && !media.finishedHere($0) }.map(\\.number)" in grid_card
    )
    assert "movie: group.movie == true" in grid_card
    read = source(READ)
    assert "Audiobook linked" not in read and "Text on \\(" not in read
    assert 'titleSymbol: book.hasAudio ? "headphones" : nil' in read  # the Mac's 🎧 beside the title
    assert "compact: !showTitle" in read and read.count("CardStatusText.series(") == 2
    assert "CardStatusText.franchise(" in read
    library = source(LIBRARY)
    book = _between(library, "struct BookCard: View", "struct BookSeriesCard: View")
    assert "PudgeTheme.size(book.sizeBytes)" not in book
    assert "CardStatusText.audiobookSeries(" in library


def test_card_lines_end_in_an_ellipsis_and_rows_stay_even():
    grid = _between(source(GRID), "struct LibraryCard<", "extension LibraryCard where")
    assert ".lineLimit(2).truncationMode(.tail)" in grid  # the title, -webkit-line-clamp:2
    assert ".lineLimit(1).truncationMode(.tail)" in grid  # the middle line, nowrap + ellipsis
    assert ".lineLimit(status.lines).truncationMode(.tail)" in grid
    assert ".frame(width: width, height: 78)" in grid  # one meta height: equal cards in a row
    assert "minimumScaleFactor" not in grid  # the Mac's CSS does not shrink text
    assert "subtitle ?? status.detail" in grid
    core = source(CORE)
    assert "self.detail = detail; self.lines = max(1, min(2, lines))" in core


def test_core_tests_cover_every_card_kind():
    tests = source(CORE_TESTS)
    for name in (
        "testAnimeStatus",
        "testMacWordingHelpers",
        "testMacHomeCardStatus",
        "testReadingStatus",
        "testShelfStatus",
        "testAudiobookStatus",
    ):
        assert f"func {name}()" in tests, name
    for text in (
        '"Episode 4 • resume at 10:00"',
        '"Episodes 8-10,12"',
        '"Episode 12 • Final"',
        '"Episode 9 in 3d"',
        '"Up-to-date"',
        '"Waiting for subs"',
        '"Download next episodes"',
        '"Volume 2 · 123k chars · 42% · 2019"',
        '"Volume 4 · 180 pages · 6%"',
        '"Continue · vol. 3"',
        '"Franchise · 2 series"',
        '"12:34 / 1:02:03 · 20% · 49:29 left · Chapter 3 · 2 files"',
    ):
        assert text in tests, text
