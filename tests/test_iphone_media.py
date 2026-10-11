from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from pudge.database import Database
from pudge.iphone_media import MediaError, compact_tokens, prepare_video, streak, utf16_to_index
from test_iphone_audio_repository import allowed
from media_tools import requires_ffmpeg


@pytest.fixture
def service(tmp_path):
    from pudge.iphone_service import IPhoneService

    return IPhoneService(Database(tmp_path / "db.sqlite"), tmp_path / "mobile")


def test_tokens_are_compact_utf16_rows_with_knowledge_state():
    text = "𠮟る猫"
    group = [
        {"start": 3, "end": 4, "wordId": 9, "readingIndex": 0},
        {"start": 0, "end": 3, "wordId": 5, "readingIndex": 1, "card": {"normalizedState": "due"}},
        {"start": 4, "end": 2},
    ]
    vocabulary = [{"wordId": 9, "readingIndex": 0, "states": ["known"]}]
    assert compact_tokens(group, vocabulary, text) == [[0, 3, 5, 1, 2], [3, 4, 9, 0, 3]]
    assert utf16_to_index(text) == [0, 0, 1, 2, 3]


def test_streak_counts_back_from_today_or_yesterday():
    days = [
        {"day": "2026-10-01", "seconds": 5},
        {"day": "2026-10-02", "seconds": 0},
        {"day": "2026-10-03", "seconds": 10},
        {"day": "2026-10-04", "seconds": 3},
        {"day": "2026-10-05", "seconds": 0},
    ]
    assert streak(days) == 2
    assert streak(days[:-1]) == 2
    assert streak([]) == 0


def _api(**values):
    defaults = dict(
        light_novels=SimpleNamespace(
            settings=lambda: SimpleNamespace(study_backend="jiten", review_mode="binary")
        )
    )
    return SimpleNamespace(**{**defaults, **values})


def test_chapter_view_marks_images_and_keeps_paragraph_tokens(service):
    payload = {
        "chapter_index": 2,
        "title": "Two",
        "parsing": False,
        "paragraphs": ["猫が鳴く", "[[PUDGE_LN_IMAGE_URL:a.png]]"],
        "tokens": [[{"start": 0, "end": 1, "wordId": 1, "readingIndex": 0}], []],
        "vocabulary": [],
    }
    service.api = _api(light_novel_chapter=lambda book, chapter: payload)
    view = service.media.handle("GET", "/api/v2/media/ln/3/chapters/2", {}, dict)
    assert view == {
        "parsing": False,
        "index": 2,
        "title": "Two",
        "paragraphs": [
            {"text": "猫が鳴く", "image": False, "tokens": [[0, 1, 1, 0, 0, 11]], "ruby": []},
            {"text": "", "image": True, "tokens": [], "ruby": [], "imageId": None},
        ],
    }
    service.api = _api(
        light_novel_chapter_parse_status=lambda book, chapter: {"ready": False, "parsing": True}
    )
    assert (
        service.media.handle("GET", "/api/v2/media/ln/3/chapters/2", {"poll": ["1"]}, dict)["parsing"] is True
    )


def test_study_action_never_reports_an_unknown_outcome_as_saved(service):
    sent = []
    service.api = _api(
        study_action=lambda payload: sent.append(payload) or {"ok": False, "outcome": "unknown"}
    )
    result = service.media.study_action(
        {"action": "review", "wordId": 4, "readingIndex": 1, "grade": "pass", "attemptId": "a" * 32}
    )
    assert result["outcome"] == "unknown" and result["ok"] is False
    assert sent[0]["attempt_id"] == "a" * 32 and sent[0]["grade"] == "pass" and sent[0]["word_id"] == 4
    service.api = _api(study_action=lambda payload: {"ok": True})
    assert (
        service.media.study_action({"action": "add", "wordId": 4, "attemptId": "b" * 32})["outcome"]
        == "saved"
    )
    with pytest.raises(MediaError):
        service.media.study_action({"action": "add", "wordId": 4, "attemptId": "x"})


def test_study_options_use_the_configured_review_mode(service):
    service.api = _api(
        study_decks=lambda backend: [{"id": 12, "name": "Main"}],
        study_deck_preference=lambda backend: {"deck_id": "12"},
    )
    options = service.media.study_options()
    assert [g["id"] for g in options["grades"]] == ["fail", "pass"]
    assert options["decks"] == [{"id": "12", "name": "Main"}] and options["deckId"] == "12"


def test_word_details_flatten_meanings_and_readings(service):
    detail = {
        "spelling": "猫",
        "reading": "猫[ねこ]",
        "meanings": [["cat", "feline"], "pet"],
        "readings": [{"reading": "ねこ"}],
        "dictionary_complete": True,
        "frequencyRank": 300,
    }
    service.api = _api(
        study_word=lambda card: detail, study_state=lambda payload: {"states": ["young"], "studyDeckIds": [3]}
    )
    word = service.media.study_word({"wordId": 5, "readingIndex": 0})
    assert word["meanings"] == ["cat", "feline", "pet"] and word["readings"] == ["ねこ"]
    assert (
        word["reading"] == "猫"
        and word["state"] == 1
        and word["inDeck"] is True
        and word["frequencyRank"] == 300
    )


def test_manga_regions_use_a_top_left_origin(service, tmp_path):
    service.api = _api(
        manga=SimpleNamespace(
            text_regions=lambda book, page, cached_only: {
                "page_index": page,
                "status": "ready",
                "regions": [
                    {
                        "x": 0.1,
                        "y": 0.7,
                        "width": 0.2,
                        "height": 0.2,
                        "text": "何だ",
                        "orientation": "vertical",
                    },
                    {"text": " "},
                ],
            }
        )
    )
    text = service.media.manga_text(1, 4)
    assert text["page"] == 4 and len(text["regions"]) == 1
    region = text["regions"][0]
    assert region["vertical"] and region["y"] == pytest.approx(0.1) and region["height"] == pytest.approx(0.2)


def test_manga_pages_are_read_from_the_archive(service, tmp_path):
    archive = tmp_path / "book.cbz"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("002.png", b"png-two")
        handle.writestr("001.jpg", b"jpg-one")
    pages = ["001.jpg", "002.png"]
    service.api = _api(
        manga=SimpleNamespace(_book=lambda book: {"path": str(archive)}, _pages=lambda path: pages)
    )
    assert service.media.manga_page(1, 1) == (b"png-two", "image/png")
    assert service.media.manga_page(1, 99) == (b"png-two", "image/png")


def _episode(service, tmp_path, subtitle_text: str):
    video = tmp_path / "ep.mkv"
    video.write_bytes(b"x")
    subtitle = tmp_path / "ep.srt"
    subtitle.write_text(subtitle_text, encoding="utf-8")
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,updated_at) VALUES(5,'Show','CURRENT',1,0)"
        )
        return conn.execute(
            "INSERT INTO episodes(media_id,title,episode,video_path,subtitle_path,state,updated_at) VALUES(5,'Ep',2,?,?,'ready',0)",
            (str(video), str(subtitle)),
        ).lastrowid


def test_ready_episodes_list_and_tokenized_cues(service, tmp_path):
    episode = _episode(
        service,
        tmp_path,
        "1\n00:00:01,000 --> 00:00:02,500\n猫だ\n走る\n\n2\n00:00:03,000 --> 00:00:04,000\nうん\n",
    )
    parsed_texts = []

    def parse(text):
        parsed_texts.append(text)
        return {
            "paragraphs": text.split("\n"),
            "vocabulary": [],
            "tokens": [
                [{"start": 0, "end": 1, "wordId": 7, "readingIndex": 0}],
                [{"start": 0, "end": 2, "wordId": 8, "readingIndex": 0}],
            ],
        }

    service.api = _api(
        light_novels=SimpleNamespace(parse_study_text=parse),
        config=SimpleNamespace(
            paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
            tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        ),
    )
    items = service.media.episodes()["items"]
    assert [(i["id"], i["title"], i["number"], i["prepared"]) for i in items] == [(episode, "Show", 2, False)]
    cues = service.media.episode_subtitles(episode)
    assert cues["tokenized"] and parsed_texts == ["猫だ 走る\nうん"]  # a cue line break keeps its length
    assert cues["cues"][0] == {
        "start": 1000,
        "end": 2500,
        "text": "猫だ\n走る",
        "tokens": [[0, 1, 7, 0, 0, 11]],
        "ruby": [],
    }
    # Cached per subtitle revision.
    assert service.media.episode_subtitles(episode) == cues and len(parsed_texts) == 1
    service.media.episode_position(episode, {"seconds": 61.5, "duration": 1400})
    with service.db.connect() as conn:
        row = conn.execute(
            "SELECT playback_position,playback_duration FROM episodes WHERE id=?", (episode,)
        ).fetchone()
    assert (row[0], row[1]) == (61.5, 1400)
    planning = service.media.planning()
    assert planning["current"][0]["readyEpisodes"] == 1 and planning["planned"] == []


@requires_ffmpeg
def test_prepared_mp4_copies_h264_and_keeps_japanese_audio(tmp_path):
    source = tmp_path / "source.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=160x90:rate=10:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=1",
            "-map",
            "0",
            "-map",
            "1",
            "-map",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "mp3",
            "-metadata:s:a:0",
            "language=eng",
            "-metadata:s:a:1",
            "language=jpn",
            str(source),
        ],
        check=True,
    )
    seen = []
    result = prepare_video(source, tmp_path / "out", ffmpeg="ffmpeg", ffprobe="ffprobe", progress=seen.append)
    probe = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(result["path"])],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    kinds = [(s["codec_type"], s["codec_name"]) for s in probe["streams"]]
    assert kinds == [("video", "h264"), ("audio", "aac")]
    assert probe["streams"][0]["codec_tag_string"] == "avc1"
    assert 900 <= result["durationMs"] <= 1200
    again = prepare_video(source, tmp_path / "out", ffmpeg="ffmpeg", ffprobe="ffprobe")
    assert again["path"] == result["path"]


def test_media_routes_over_https(service, tmp_path):
    import http.client
    import ssl
    import uuid
    from pudge.iphone_http import start_iphone_server

    archive = tmp_path / "book.cbz"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("001.jpg", b"page-bytes")
    service.api = _api(
        consumption_statistics_query=lambda scope, limit, offset: {
            "summary": {"total_seconds": 100, "active_days": 1, "sessions": 2},
            "breakdown": {"anime": 100},
            "days": [{"day": "2026-10-05", "seconds": 100, "breakdown": {"anime": 100}}],
            "works": [{"title": "Show", "kind": "anime", "seconds": 100}],
        },
        manga=SimpleNamespace(_book=lambda book: {"path": str(archive)}, _pages=lambda path: ["001.jpg"]),
        study_parse_text=lambda text: (_ for _ in ()).throw(
            RuntimeError("Jiten API token is not configured")
        ),
    )
    server, thread, pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
        context.check_hostname = False
        invite = service.invite(f"https://127.0.0.1:{server.server_port}", pin)
        token = allowed(
            service,
            {
                "invitation": invite["invitation"],
                "deviceId": str(uuid.uuid4()),
                "attemptId": str(uuid.uuid4()),
                "name": "Phone",
            },
        )["token"]

        def call(method, route, body=None):
            connection = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context)
            headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
            connection.request(method, route, json.dumps(body) if body is not None else None, headers)
            response = connection.getresponse()
            data = response.read()
            connection.close()
            return response.status, response.getheader("Content-Type"), data

        status, _kind, data = call("GET", "/api/v2/media/stats?period=7d")
        assert status == 200 and json.loads(data)["streak"] == 1
        assert call("GET", "/api/v2/media/manga/1/pages/0")[:3] == (200, "image/jpeg", b"page-bytes")
        status, _kind, data = call("POST", "/api/v2/media/study/parse", {"text": "猫"})
        assert status == 422 and json.loads(data)["error"] == "Jiten API token is not configured"
        assert call("GET", "/api/v2/media/nothing")[0] == 404
        connection = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context)
        connection.request("GET", "/api/v2/media/stats")
        assert connection.getresponse().status == 401
        connection.close()
    finally:
        server.shutdown()
        assert server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


def test_unread_post_body_does_not_break_the_next_kept_alive_request(service, tmp_path, monkeypatch):
    """A POST whose route ignores its body ({} from the phone) must not turn the next
    request on the same connection into ``{}GET ...`` (HTTP 501 on the phone)."""
    import http.client
    import ssl
    import uuid
    from pudge.iphone_http import start_iphone_server

    service.api = _api(
        consumption_statistics_query=lambda scope, limit, offset: {
            "summary": {},
            "breakdown": {},
            "days": [],
            "works": [],
        }
    )
    monkeypatch.setattr(type(service.media), "prepare_episode", lambda self, episode: {"state": "queued"})
    server, thread, pin = start_iphone_server(service, host="127.0.0.1", port=0)
    try:
        context = ssl.create_default_context(cafile=str(service.root / "tls-cert.pem"))
        context.check_hostname = False
        invite = service.invite(f"https://127.0.0.1:{server.server_port}", pin)
        token = allowed(
            service,
            {
                "invitation": invite["invitation"],
                "deviceId": str(uuid.uuid4()),
                "attemptId": str(uuid.uuid4()),
                "name": "Phone",
            },
        )["token"]
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        connection = http.client.HTTPSConnection("127.0.0.1", server.server_port, context=context)
        connection.request("POST", "/api/v2/media/episodes/7/prepare", "{}", headers)
        first = connection.getresponse()
        assert first.status == 200 and json.loads(first.read()) == {"state": "queued"}
        connection.request("GET", "/api/v2/media/stats?period=7d", None, headers)
        second = connection.getresponse()
        assert second.status == 200
        second.read()
        connection.close()
    finally:
        server.shutdown()
        assert server.wait_for_requests(timeout=5)
        server.server_close()
        thread.join(5)


@requires_ffmpeg
def test_read_along_sentences_carry_word_offsets(service, tmp_path):
    from tests.test_iphone_read_along import _setup

    _setup(service, tmp_path)
    with service.db.connect() as conn:
        conn.execute("ALTER TABLE ln_chapters ADD COLUMN text_hash TEXT")
        conn.execute("UPDATE ln_chapters SET text_hash='h1'")
    paragraphs = ["「おはよう。」と言った。", "[[PUDGE_LN_IMAGE_URL:x.png]]", "彼は笑った！本当？"]
    parsed = {
        "paragraphs": paragraphs,
        "vocabulary": [],
        "tokens": [
            [
                {"start": 1, "end": 5, "wordId": 11, "readingIndex": 0},
                {"start": 8, "end": 10, "wordId": 12, "readingIndex": 0},
            ],
            [],
            [
                {"start": 0, "end": 1, "wordId": 13, "readingIndex": 0},
                {"start": 6, "end": 8, "wordId": 14, "readingIndex": 0},
            ],
        ],
    }
    service.api = SimpleNamespace(
        light_novels=SimpleNamespace(_cached_parse=lambda digest, provider=None: parsed)
    )
    service.refresh_catalog()
    manifest = service.catalog()["items"][0]
    assert manifest["lightNovelId"] == 7  # the iPhone opens this light novel's reader with the audio
    read_along = manifest["readAlong"]
    output = service.resource(read_along["resourceId"], read_along["sha256"])
    sentences = json.loads(output.read_text(encoding="utf-8"))["chapters"][0]["sentences"]
    # おはよう starts the text (offset 0); 言っ is in the second sentence at offset 5 (おはようと).
    assert sentences[0]["w"] == [[1, 5, 0, 11, 0]]
    assert sentences[1]["w"] == [[1, 3, 5, 12, 0]]
    assert sentences[2]["w"] == [[0, 1, 8, 13, 0]]
    assert sentences[3]["w"] == [[0, 2, 13, 14, 0]]


def test_paragraph_flags_and_furigana_follow_the_mac_reader_settings():
    from pudge.iphone_media import paragraph_view

    text = "食べる猫を見た"
    group = [
        {
            "start": 0,
            "end": 3,
            "wordId": 1,
            "readingIndex": 0,
            "reading": "たべる",
            "card": {"states": ["known"]},
        },
        {
            "start": 3,
            "end": 4,
            "wordId": 2,
            "readingIndex": 0,
            "rubies": [{"start": 3, "end": 4, "text": "ねこ"}],
        },
        {
            "start": 5,
            "end": 6,
            "wordId": 3,
            "readingIndex": 0,
            "reading": "ミ",
            "card": {"states": ["young"]},
        },
    ]
    settings = SimpleNamespace(furigana_states="new,learning,young", underline_states="new,young")
    view = paragraph_view(text, group, [], settings)
    assert [row[5] & 7 for row in view["tokens"]] == [
        4,
        3,
        3,
    ]  # known: mature, no furigana/mark; new and young: both
    assert [(row[5] >> 3) & 15 for row in view["tokens"]] == [6, 1, 2]  # JitenMPV mastered, new, young (+1)
    # Bits 7-10: the Mac reader's word state (WORD_STATES index + 1): "known" is Jiten's
    # mature, a word without a card is unknown (0), young.
    assert [row[5] >> 7 for row in view["tokens"]] == [4, 0, 3]
    assert view["ruby"] == [[0, 1, "た"], [3, 4, "ねこ"], [5, 6, "み"]]


def test_episode_list_matches_the_ready_section(service, tmp_path):
    ready = _episode(service, tmp_path, "1\n00:00:01,000 --> 00:00:02,000\nうん\n")
    with service.db.connect() as conn:
        for number, extra in [
            (1, "state='ready'"),
            (3, "state='watched',watched_at=1,delete_after=5"),
            (4, "state='ready',delete_after=5"),
        ]:
            video = tmp_path / f"{number}.mkv"
            video.write_bytes(b"x")
            row = conn.execute(
                "INSERT INTO episodes(media_id,title,episode,video_path,subtitle_path,state,updated_at) VALUES(5,'Ep',?,?,?,'ready',0)",
                (number, str(video), str(tmp_path / "ep.srt")),
            ).lastrowid
            conn.execute(f"UPDATE episodes SET {extra} WHERE id=?", (row,))
    service.api = _api()
    items = service.media.episodes()["items"]
    # Progress 1: episode 1 is already watched on AniList; 3 is watched, 4 is scheduled for deletion.
    assert [i["id"] for i in items] == [ready] and len(items[0]["subtitleRevision"]) == 16


@requires_ffmpeg
def test_embedded_subtitle_uses_the_stream_of_the_mpv_sid(service, tmp_path):
    video = tmp_path / "dual.mkv"
    english, japanese = tmp_path / "en.srt", tmp_path / "ja.srt"
    english.write_text("1\n00:00:00,100 --> 00:00:00,900\nHello\n", encoding="utf-8")
    japanese.write_text("1\n00:00:00,100 --> 00:00:00,900\nこんにちは\n", encoding="utf-8")
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=64x36:rate=5:duration=1",
            "-i",
            str(english),
            "-i",
            str(japanese),
            "-map",
            "0",
            "-map",
            "1",
            "-map",
            "2",
            "-c:v",
            "libx264",
            "-c:s",
            "srt",
            "-metadata:s:s:0",
            "language=eng",
            "-metadata:s:s:1",
            "language=jpn",
            str(video),
        ],
        check=True,
    )
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,updated_at) VALUES(5,'Show','CURRENT',0,0)"
        )
        # mpv sid 2 = second subtitle track (absolute stream 2), not stream 2 = first subtitle.
        episode = conn.execute(
            "INSERT INTO episodes(media_id,title,episode,video_path,embedded_subtitle_id,subtitle_origin,state,updated_at) "
            "VALUES(5,'Ep',1,?,2,'embedded','ready',0)",
            (str(video),),
        ).lastrowid
    service.api = _api(
        light_novels=SimpleNamespace(
            parse_study_text=lambda text: (_ for _ in ()).throw(RuntimeError("offline"))
        ),
        config=SimpleNamespace(
            paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
            tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe"),
        ),
    )
    cues = service.media.episode_subtitles(episode)
    assert [c["text"] for c in cues["cues"]] == ["こんにちは"] and cues["tokenized"] is False


def test_review_gate_is_compacted_for_the_phone(service, tmp_path):
    episode = _episode(service, tmp_path, "1\n00:00:01,000 --> 00:00:02,000\nうん\n")
    calls = []
    card = {
        "wordId": 9,
        "readingIndex": 1,
        "wordText": "食[た]べる",
        "readings": [{"readingIndex": 1, "text": "食[た]べる"}],
        "definitions": [{"meanings": ["to eat", "to eat"]}, {"meanings": ["to live on"]}],
        "exampleSentence": {"text": "**食べる**よ"},
    }
    status = {
        "enabled": True,
        "blocking": True,
        "granted": False,
        "required": 3,
        "completed": 1,
        "remaining": 2,
        "review_actions": {
            "actions": [
                {"id": "fail", "label_en": "Fail", "tone": "again"},
                {"id": "pass", "label_en": "Pass", "tone": "good"},
            ]
        },
        "cards": [card],
    }
    service.api = _api(
        review_gate_begin=lambda path: status,
        review_gate_review=lambda *args: (
            calls.append(args) or {**status, "cards": [], "remaining": 1, "outcome": "confirmed"}
        ),
    )
    view = service.media.gate_begin(episode)
    assert (
        view["blocking"] and view["remaining"] == 2 and [g["id"] for g in view["grades"]] == ["fail", "pass"]
    )
    assert view["cards"] == [
        {
            "wordId": 9,
            "readingIndex": 1,
            "word": "食べる",
            "reading": "たべる",
            "meanings": ["to eat", "to live on"],
            "sentence": "食べるよ",
            "frequencyRank": None,
        }
    ]
    after = service.media.gate_review(
        episode, {"wordId": 9, "readingIndex": 1, "grade": "pass", "attemptId": "c" * 32}
    )
    assert after["remaining"] == 1 and after["outcome"] == "confirmed"
    assert calls[0][1:] == (9, 1, "pass", "c" * 32) and calls[0][0].endswith("ep.mkv")


def test_anime_groups_follow_the_personal_weekly_schedule(service, tmp_path):
    with service.db.connect() as conn:
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,episodes,updated_at) VALUES(8,'Class','CURRENT',3,22,0)"
        )
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,episodes,updated_at) VALUES(9,'Plain','CURRENT',1,12,0)"
        )
        conn.execute(
            "INSERT INTO anime(media_id,title,status,progress,episodes,updated_at) VALUES(10,'Waiting','CURRENT',2,12,0)"
        )
        paths = {}
        for media, number in [(8, 4), (8, 5), (8, 6), (9, 1), (9, 2)]:
            video = tmp_path / f"{media}-{number}.mkv"
            video.write_bytes(b"x")
            subtitle = tmp_path / f"{media}-{number}.srt"
            subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nx\n", encoding="utf-8")
            paths[(media, number)] = str(video)
            conn.execute(
                "INSERT INTO episodes(media_id,title,episode,video_path,subtitle_path,state,updated_at) VALUES(?,?,?,?,?,'ready',0)",
                (media, f"Ep {number}", number, str(video), str(subtitle)),
            )
    library = [
        SimpleNamespace(media_id=8, title="Class", progress=3, episodes=22),
        SimpleNamespace(media_id=9, title="Plain", progress=1, episodes=12),
        SimpleNamespace(media_id=10, title="Waiting", progress=2, episodes=12),
    ]
    snapshots = {
        8: {
            "enabled": True,
            "status": "new_ready",
            "next_episode": 4,
            "next_unlock_at": 100.0,
            "ready_episodes": [4],
        },
        10: {
            "enabled": True,
            "status": "caught_up",
            "next_episode": 3,
            "next_unlock_at": 2000.0,
            "ready_episodes": [],
        },
    }
    service.api = _api(
        manager=SimpleNamespace(
            db=SimpleNamespace(
                anime_list=lambda: library, personal_release_schedule=lambda media: media in snapshots
            )
        ),
        _personal_schedule_snapshot=lambda anime: snapshots.get(anime.media_id, {"enabled": False}),
        _local_episode_for_relative=lambda anime, number: SimpleNamespace(
            video_path=Path(paths[(anime.media_id, number)])
        ),
    )
    groups = {g["title"]: g for g in service.media.anime()["items"]}
    # Weekly schedule: only the unlocked episode 4, not 5 and 6.
    assert [e["number"] for e in groups["Class"]["episodes"]] == [4]
    assert groups["Class"]["schedule"] == {"status": "new_ready", "nextEpisode": 4, "nextUnlockAt": 100.0}
    assert [e["number"] for e in groups["Plain"]["episodes"]] == [2] and groups["Plain"]["schedule"] is None
    assert groups["Waiting"]["episodes"] == [] and groups["Waiting"]["schedule"]["status"] == "caught_up"
    assert [e["number"] for e in service.media.episodes()["items"]] == [4, 2]


def test_live_states_are_compacted_with_flags(service):
    service.api = _api(
        study_states=lambda payload: {
            "ok": True,
            "states": [
                {"wordId": 1, "readingIndex": 0, "states": ["mature"], "normalizedState": "learning"},
                {"wordId": 2, "readingIndex": 1, "states": [], "normalizedState": "new"},
            ],
        }
    )
    rows = service.media.study_states({"pairs": [[1, 0], [2, 1], ["x"]]})["rows"]
    # An older cache's "learning" name: the raw states decide, as in the Mac reader (mature, word state 4).
    assert rows == [[1, 0, 3, 7 | 3 << 3 | 4 << 7], [2, 1, 0, 3 | 1 << 3 | 1 << 7]]


def test_jitenmpv_look_reads_the_theme_or_custom_colours(tmp_path):
    from pudge.iphone_media import subtitle_look

    assert subtitle_look(tmp_path / "missing.json")["source"] == "default"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"Theme": "Underline", "FontSize": 52}), encoding="utf-8")
    look = subtitle_look(config)
    assert look["source"] == "jiten-mpv" and look["fontSize"] == 52
    assert look["states"][0]["underlineColor"] == "#a566ef" and "underline" not in look["states"][2]
    config.write_text(
        json.dumps(
            {
                "Theme": "Custom",
                "CustomThemeColors": {
                    "Young": {
                        "text_color": "#123456",
                        "outline_color": "#000000",
                        "outline_size": 2,
                        "bold": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    look = subtitle_look(config)
    assert look["states"][1] == {
        "text": "#123456",
        "outline": "#000000",
        "outlineSize": 2.0,
        "bold": True,
        "italic": False,
        "underline": False,
    }
    assert look["states"][0]["text"] == "#a566ef"  # missing custom states keep the Default theme


def test_jiten_state_collapses_like_jitenmpv():
    from pudge.iphone_media import JITEN_STATES, jiten_state, token_flags

    assert JITEN_STATES[jiten_state({"states": ["due", "young"]})] == "due"
    assert JITEN_STATES[jiten_state({"states": ["young", "suspended"]})] == "suspended"
    assert JITEN_STATES[jiten_state({"states": ["never-forget"]})] == "mastered"
    assert JITEN_STATES[jiten_state({})] == "new"
    assert (token_flags({"states": ["mature"]}, set(), set()) >> 3) & 15 == JITEN_STATES.index("mature") + 1


def test_episode_watched_on_iphone_counts_like_mpv(service, tmp_path, monkeypatch):
    episode = _episode(service, tmp_path, "1\n00:00:01,000 --> 00:00:02,000\nうん\n")
    calls = []

    class Client:
        def __init__(self, endpoint, access_token):
            pass

        def update_progress(self, media_id, number, total, **options):
            calls.append((media_id, number, total))
            return {"updated": True, "progress": number, "status": "CURRENT"}

        def close(self):
            pass

    monkeypatch.setattr("pudge.providers.anilist.AniListClient", Client)
    anilist = SimpleNamespace(
        access_token="t",
        endpoint="e",
        add_if_missing=False,
        update_when_rewatching=False,
        completed_to_rewatching_on_episode_one=False,
        complete_current_final=True,
        complete_rewatching_final=True,
    )
    service.api = _api(
        manager=SimpleNamespace(db=service.db),
        config=SimpleNamespace(anilist=anilist, agent=SimpleNamespace(delete_after_watched_hours=24)),
    )
    short = service.media.episode_watched(episode, {"duration": 1400, "activeSeconds": 600})
    assert short["ok"] is False and short["outcome"] == "not_enough" and calls == []
    done = service.media.episode_watched(episode, {"duration": 1400, "activeSeconds": 1000})
    assert done["ok"] and calls == [(5, 2, None)]
    with service.db.connect() as conn:
        row = conn.execute("SELECT state,delete_after FROM episodes WHERE id=?", (episode,)).fetchone()
        progress = conn.execute("SELECT progress FROM anime WHERE media_id=5").fetchone()[0]
    assert row["state"] == "watched" and row["delete_after"] is not None and progress == 2
    assert service.media.episodes()["items"] == []


def test_light_novels_carry_series_and_franchise_grouping(service):
    with service.db.connect() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ln_books(id INTEGER PRIMARY KEY,title TEXT,volume INT,current_chapter INT,current_offset REAL,finished INT,cover_url TEXT,updated_at REAL)"
        )
        conn.execute("INSERT INTO ln_books VALUES(1,'狼と香辛料 2',2,0,0,0,'',2)")
        conn.execute("INSERT INTO ln_books VALUES(2,'狼と香辛料 1',1,0,0,1,'',1)")
    service.api = _api(
        light_novels=SimpleNamespace(
            state=lambda: {
                "books": [
                    {
                        "id": 1,
                        "series_key": "anilist:9",
                        "series_title": "狼と香辛料",
                        "franchise_key": "f:9",
                        "franchise_title": "Spice",
                        "reading_progress": 0.4,
                    },
                    {
                        "id": 2,
                        "series_key": "anilist:9",
                        "series_title": "狼と香辛料",
                        "franchise_key": "f:9",
                        "franchise_title": "Spice",
                    },
                ]
            }
        )
    )
    items = {item["id"]: item for item in service.media.light_novels()["items"]}
    assert (
        items[1]["seriesKey"] == "anilist:9"
        and items[1]["franchiseTitle"] == "Spice"
        and items[1]["progress"] == 0.4
    )
    assert items[2]["progress"] == 1.0 and items[2]["volume"] == 1
