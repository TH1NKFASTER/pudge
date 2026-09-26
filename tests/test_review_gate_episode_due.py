from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from pudge.review_episode_due import build_episode_due_review_cards, resolve_episode_review_subtitle
from pudge.models import EmbeddedSubtitle
import pudge.review_episode_due as review_episode_due_module
from pudge.config import AppConfig, load_config, write_config
from pudge.light_novels import LightNovelService
from pudge.review_gate import EpisodeReviewIdentity, ReviewGateStore
from pudge.web_app import WebAppApi
import pudge.web_app as web_app_module


class _State:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_state(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def set_state(self, key: str, value: str) -> None:
        self.values[key] = value


def test_review_gate_due_store_is_separate_and_supports_zero_due() -> None:
    state = _State()
    identity = EpisodeReviewIdentity(10, 3)
    normal = ReviewGateStore(state)
    due = ReviewGateStore(state, prefix="review_gate_episode_due:v1")

    normal.mark_confirmed("acct", identity, required=1, card_key="1:0")

    assert normal.status("acct", identity, required=1).granted is True
    assert due.snapshot("acct", identity)["confirmed_card_keys"] == []
    assert due.status("acct", identity, required=0).granted is True
    due.grant("acct", identity, required=0)
    assert due.snapshot("acct", identity)["granted"] is True


def test_all_due_setting_roundtrips_in_config(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.ui.review_gate_all_due_episode_words = True
    path = tmp_path / "config.toml"
    write_config(cfg, path)

    loaded = load_config(path)

    assert loaded.ui.review_gate_all_due_episode_words is True


def test_jiten_card_media_batch_exposes_optional_image_url() -> None:
    service = LightNovelService.__new__(LightNovelService)
    calls = []

    def request(route, payload):
        calls.append((route, payload))
        return {
            "items": [
                {
                    "wordId": 10,
                    "readingIndex": 0,
                    "image": {"url": "https://img.test/10.jpg"},
                    "audio": None,
                }
            ]
        }

    service._jiten_request = request  # type: ignore[method-assign]
    result = service.jiten_card_media([(10, 0)])

    assert calls[0][0] == "srs/card-media/batch"
    assert calls[0][1] == {"items": [{"wordId": 10, "readingIndex": 0}]}
    assert result[(10, 0)]["image"]["url"] == "https://img.test/10.jpg"


class _FakeJiten:
    def jiten_preparse(self, text: str, digest: str | None = None):
        assert text == "猫が走る。\n犬も走る。"
        return {
            "tokens": [
                [{"wordId": 10, "readingIndex": 0}, {"wordId": 20, "readingIndex": 1}],
                [{"wordId": 30, "readingIndex": 0}, {"wordId": 20, "readingIndex": 1}],
            ],
            "vocabulary": [
                {
                    "wordId": 10,
                    "readingIndex": 0,
                    "spelling": "猫",
                    "reading": "ねこ",
                    "meaningsChunks": [["cat"]],
                    "partsOfSpeech": ["noun"],
                    "frequencyRank": 100,
                },
                {
                    "wordId": 20,
                    "readingIndex": 1,
                    "spelling": "走る",
                    "reading": "はしる",
                    "meaningsChunks": [["to run"]],
                    "partsOfSpeech": ["verb"],
                    "frequencyRank": 200,
                },
                {
                    "wordId": 30,
                    "readingIndex": 0,
                    "spelling": "犬",
                    "reading": "いぬ",
                    "meaningsChunks": [["dog"]],
                    "partsOfSpeech": ["noun"],
                    "frequencyRank": 300,
                },
            ],
        }

    def jiten_live_states(self, pairs, force: bool = False):
        assert force is True
        due = {(10, 0), (20, 1)}
        return [
            {
                "wordId": word_id,
                "readingIndex": reading_index,
                "normalizedState": "due" if (word_id, reading_index) in due else "mature",
                "states": ["due"] if (word_id, reading_index) in due else ["mature"],
            }
            for word_id, reading_index in pairs
        ]

    def jiten_card_media(self, pairs):
        assert (10, 0) in pairs
        return {(10, 0): {"image": {"url": "https://img.test/cat.jpg"}}}


def test_episode_due_cards_intersect_episode_vocabulary_with_live_due_state(tmp_path: Path) -> None:
    subtitle = tmp_path / "episode.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n猫が走る。\n\n"
        "2\n00:00:04,000 --> 00:00:06,000\n犬も走る。\n",
        encoding="utf-8",
    )

    result = build_episode_due_review_cards(_FakeJiten(), subtitle, tmp_path / "cache")

    assert result["episode_pairs"] == 3
    assert result["due_pairs"] == 2
    assert [card["pudgeCardKey"] for card in result["cards"]] == ["10:0", "20:1"]
    assert result["cards"][0]["pudgeContextSentence"] == "猫が走る。"
    assert result["cards"][0]["pudgeContextImage"] == "https://img.test/cat.jpg"
    assert result["cards"][1]["pudgeEpisodeOccurrences"] == 2
    assert all(card["pudgeCardKey"] != "30:0" for card in result["cards"])


class _FakeDb(_State):
    def __init__(self, video_path: Path, subtitle_path: Path) -> None:
        super().__init__()
        self.video_path = video_path.resolve()
        self.episode = SimpleNamespace(
            media_id=210482, episode=1, video_path=self.video_path,
            subtitle_path=subtitle_path, embedded_subtitle_id=None,
        )

    def episode_by_path(self, path: Path):
        return self.episode if Path(path).resolve() == self.video_path else None


class _FakeLightNovels:
    def settings(self):
        return SimpleNamespace(study_backend="jiten")

    def study_provider_capabilities(self, backend: str):
        assert backend == "jiten"
        return {
            "configured": True,
            "strict_gate_supported": True,
            "account_key": "jiten:test",
        }

    def jiten_card_media(self, pairs):
        return {}

    def strict_review_submit(self, word_id, reading_index, grade, *, attempt_id):
        return {
            "ok": True,
            "word_id": int(word_id),
            "reading_index": int(reading_index),
            "grade": str(grade),
            "attempt_id": str(attempt_id),
        }


def test_all_due_begin_uses_dynamic_episode_target_and_authorizes_every_due_card(
    tmp_path: Path, monkeypatch,
) -> None:
    video = tmp_path / "ep.mkv"
    subtitle = tmp_path / "ep.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n猫。\n", encoding="utf-8")
    db = _FakeDb(video, subtitle)

    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        ui=SimpleNamespace(
            review_gate_enabled=True,
            review_gate_count=5,
            review_gate_all_due_episode_words=True,
        ),
        paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
        tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    api.manager = SimpleNamespace(db=db)
    api.light_novels = _FakeLightNovels()
    api.logger = SimpleNamespace(info=lambda *args, **kwargs: None)

    monkeypatch.setattr(
        web_app_module,
        "build_episode_due_review_cards",
        lambda *args, **kwargs: {
            "cards": [
                {"wordId": 1, "readingIndex": 0, "pudgeCardKey": "1:0"},
                {"wordId": 2, "readingIndex": 0, "pudgeCardKey": "2:0"},
            ],
            "episode_pairs": 9,
            "due_pairs": 2,
        },
    )

    result = api.review_gate_begin(str(video))

    assert result["all_due_episode_words"] is True
    assert result["required"] == 2
    assert result["completed"] == 0
    assert result["issued"] == 2
    assert {card["pudgeCardKey"] for card in result["cards"]} == {"1:0", "2:0"}
    assert api._review_gate_candidates["jiten:test:210482:1"] == {"1:0", "2:0"}



def test_all_due_reviews_use_separate_progress_and_grant_after_full_snapshot(
    tmp_path: Path, monkeypatch,
) -> None:
    video = tmp_path / "ep.mkv"
    subtitle = tmp_path / "ep.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n猫。\n", encoding="utf-8")
    db = _FakeDb(video, subtitle)
    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        ui=SimpleNamespace(
            review_gate_enabled=True,
            review_gate_count=1,
            review_gate_all_due_episode_words=True,
        ),
        paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
        tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    api.manager = SimpleNamespace(db=db)
    api.light_novels = _FakeLightNovels()
    api.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    monkeypatch.setattr(
        web_app_module,
        "build_episode_due_review_cards",
        lambda *args, **kwargs: {
            "cards": [
                {"wordId": 1, "readingIndex": 0, "pudgeCardKey": "1:0"},
                {"wordId": 2, "readingIndex": 0, "pudgeCardKey": "2:0"},
            ],
            "episode_pairs": 2,
            "due_pairs": 2,
        },
    )

    begin = api.review_gate_begin(str(video))
    assert begin["required"] == 2  # fixed review_gate_count=1 is ignored
    first = api.review_gate_review(str(video), 1, 0, "good", "a1")
    assert first["completed"] == 1
    assert first["granted"] is False
    second = api.review_gate_review(str(video), 2, 0, "easy", "a2")
    assert second["completed"] == 2
    assert second["granted"] is True

    # The old quota store is deliberately untouched by the new mode.
    identity = EpisodeReviewIdentity(210482, 1)
    assert api._review_gate_store.snapshot("jiten:test", identity)["granted"] is False
    assert api._review_gate_due_store.snapshot("jiten:test", identity)["granted"] is True

def test_all_due_begin_grants_episode_when_no_due_words(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "ep.mkv"
    subtitle = tmp_path / "ep.srt"
    video.write_bytes(b"video")
    subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\n猫。\n", encoding="utf-8")
    db = _FakeDb(video, subtitle)

    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        ui=SimpleNamespace(
            review_gate_enabled=True,
            review_gate_count=5,
            review_gate_all_due_episode_words=True,
        ),
        paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
        tools=SimpleNamespace(ffmpeg="ffmpeg", ffprobe="ffprobe"),
    )
    api.manager = SimpleNamespace(db=db)
    api.light_novels = _FakeLightNovels()
    api.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    monkeypatch.setattr(
        web_app_module,
        "build_episode_due_review_cards",
        lambda *args, **kwargs: {"cards": [], "episode_pairs": 4, "due_pairs": 0},
    )

    result = api.review_gate_begin(str(video))

    assert result["required"] == 0
    assert result["granted"] is True
    assert result["blocking"] is False
    assert result["cards"] == []
    assert api._review_gate_due_store.snapshot("jiten:test", EpisodeReviewIdentity(210482, 1))["granted"] is True


def test_review_gate_ui_uses_episode_sentence_fallback_and_optional_image() -> None:
    package = Path(__file__).parents[1] / "pudge"
    js = (package / "web" / "review_gate.js").read_text(encoding="utf-8")
    css = (package / "web" / "review_gate.css").read_text(encoding="utf-8")
    html = (package / "web" / "index.html").read_text(encoding="utf-8")

    assert "card?.exampleSentence?.text || card?.pudgeContextSentence" in js
    assert "card?.pudgeContextImage" in js
    assert ".pudge-review-gate-image" in css
    assert 'id="s_review_gate_all_due_episode_words"' in html
    assert "review_gate_all_due_episode_words:c('s_review_gate_all_due_episode_words')" in html


def test_episode_due_resolver_prefers_existing_external_subtitle(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "episode.mkv"
    external = tmp_path / "episode.ja.srt"
    video.write_bytes(b"video")
    external.write_text("1\n00:00:00,000 --> 00:00:01,000\n猫。\n", encoding="utf-8")

    monkeypatch.setattr(
        review_episode_due_module,
        "find_embedded_japanese_subtitles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("embedded probe must not run")),
    )

    resolved, meta = resolve_episode_review_subtitle(
        video_path=video,
        subtitle_path=external,
        embedded_subtitle_id=2,
        cache_dir=tmp_path / "cache",
    )

    assert resolved == external
    assert meta["source"] == "external"


def test_episode_due_resolver_extracts_selected_embedded_japanese_text_track_once(
    tmp_path: Path, monkeypatch,
) -> None:
    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    calls: list[list[str]] = []

    monkeypatch.setattr(
        review_episode_due_module,
        "probe_media",
        lambda *_args, **_kwargs: {
            "streams": [
                {"index": 4, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "eng"}},
                {"index": 7, "codec_type": "subtitle", "codec_name": "ass", "tags": {"language": "jpn"}},
            ]
        },
    )
    monkeypatch.setattr(
        review_episode_due_module,
        "find_embedded_japanese_subtitles",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("stored text sid should avoid rescanning")),
    )

    def fake_run(args, **_kwargs):
        calls.append([str(value) for value in args])
        Path(args[-1]).write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nやれやれだぜ。\n", encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(review_episode_due_module.subprocess, "run", fake_run)

    first, first_meta = resolve_episode_review_subtitle(
        video_path=video,
        subtitle_path=None,
        embedded_subtitle_id=2,
        cache_dir=tmp_path / "cache",
        ffmpeg_path="fake-ffmpeg",
        ffprobe_path="fake-ffprobe",
    )
    second, second_meta = resolve_episode_review_subtitle(
        video_path=video,
        subtitle_path=None,
        embedded_subtitle_id=2,
        cache_dir=tmp_path / "cache",
        ffmpeg_path="fake-ffmpeg",
        ffprobe_path="fake-ffprobe",
    )

    assert first == second
    assert first.is_file()
    assert first_meta["source"] == "embedded"
    assert first_meta["stream_index"] == 7
    assert first_meta["subtitle_id"] == 2
    assert first_meta["cached"] is False
    assert second_meta["cached"] is True
    assert len(calls) == 1
    assert calls[0][0] == "fake-ffmpeg"
    assert calls[0][calls[0].index("-map") + 1] == "0:7"


def test_all_due_begin_resolves_embedded_subtitle_before_scanning(tmp_path: Path, monkeypatch) -> None:
    video = tmp_path / "ep.mkv"
    extracted = tmp_path / "cache" / "jojo.srt"
    video.write_bytes(b"video")
    extracted.parent.mkdir(parents=True)
    extracted.write_text("1\n00:00:01,000 --> 00:00:02,000\nジョジョ。\n", encoding="utf-8")
    db = _FakeDb(video, tmp_path / "missing.srt")
    db.episode.embedded_subtitle_id = 9

    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(
        ui=SimpleNamespace(
            review_gate_enabled=True,
            review_gate_count=5,
            review_gate_all_due_episode_words=True,
        ),
        paths=SimpleNamespace(cache_dir=tmp_path / "cache"),
        tools=SimpleNamespace(ffmpeg="ffmpeg-custom", ffprobe="ffprobe-custom"),
    )
    api.manager = SimpleNamespace(db=db)
    api.light_novels = _FakeLightNovels()
    api.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    resolve_calls = []

    def fake_resolve(**kwargs):
        resolve_calls.append(kwargs)
        return extracted, {"source": "embedded", "stream_index": 11, "subtitle_id": 9}

    monkeypatch.setattr(web_app_module, "resolve_episode_review_subtitle", fake_resolve)
    monkeypatch.setattr(
        web_app_module,
        "build_episode_due_review_cards",
        lambda service, subtitle_path, cache_dir: {
            "cards": [{"wordId": 77, "readingIndex": 0, "pudgeCardKey": "77:0"}],
            "episode_pairs": 1,
            "due_pairs": 1,
        },
    )

    result = api.review_gate_begin(str(video))

    assert resolve_calls[0]["video_path"] == video.resolve()
    assert resolve_calls[0]["embedded_subtitle_id"] == 9
    assert resolve_calls[0]["ffmpeg_path"] == "ffmpeg-custom"
    assert resolve_calls[0]["ffprobe_path"] == "ffprobe-custom"
    assert result["required"] == 1
    assert result["cards"][0]["pudgeCardKey"] == "77:0"


def test_review_gate_keyboard_and_stale_warm_pool_contract() -> None:
    package = Path(__file__).parents[1] / "pudge"
    gate = (package / "web" / "review_gate.js").read_text(encoding="utf-8")
    reading = (package / "web" / "reading_tools.js").read_text(encoding="utf-8")
    html = (package / "web" / "index.html").read_text(encoding="utf-8")

    for code, grade in (("Digit1", "again"), ("Digit2", "hard"), ("Digit3", "good"), ("Digit4", "easy")):
        assert code in gate
        assert grade in gate
        assert code in reading
        assert grade in reading
    assert "focused.click()" in gate
    assert "focused.click()" in reading
    assert "snapshot?.reason === 'episode_due_mode'" in gate
    assert "initialStatus?.all_due_episode_words" in gate
    assert "window.__pudgeReviewGateWarmCards = [];" in gate
    assert "window.PudgeReadingTools?.study?.handleReviewKeydown?.(event)" in html


def test_jiten_study_card_is_positioned_offscreen_before_opening() -> None:
    package = Path(__file__).parents[1] / "pudge"
    reading = (package / "web" / "reading_tools.js").read_text(encoding="utf-8")
    css = (package / "web" / "reading_tools.css").read_text(encoding="utf-8")

    open_at = reading.index("pop.classList.add('open')")
    prelude = reading[max(0, open_at - 500):open_at]
    assert "pop.style.left = '-10000px'" in prelude
    assert "pop.style.top = '-10000px'" in prelude
    position_block = reading.split("function position(el, rect)", 1)[1].split("function closeStudyCard", 1)[0]
    assert "requestAnimationFrame(() =>" not in position_block
    assert ".pudge-study-grade:focus" in css
