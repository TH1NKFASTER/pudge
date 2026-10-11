from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pudge.review_episode_due import build_episode_due_review_cards, resolve_episode_review_subtitle
import pudge.review_episode_due as review_episode_due_module
from pudge.config import AppConfig, load_config, write_config
from pudge.light_novels import LightNovelService
from pudge.review_gate import EpisodeReviewIdentity, ReviewGateStore
from pudge.web_app import WebAppApi
import pudge.web_app as web_app_module
from css_support import css_rules
from study_page_support import CARD, GATE, GATE_VIEW, LN_BOOK


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
        "1\n00:00:01,000 --> 00:00:03,000\n猫が走る。\n\n2\n00:00:04,000 --> 00:00:06,000\n犬も走る。\n",
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
            media_id=210482,
            episode=1,
            video_path=self.video_path,
            subtitle_path=subtitle_path,
            embedded_subtitle_id=None,
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
    tmp_path: Path,
    monkeypatch,
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
    tmp_path: Path,
    monkeypatch,
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
    assert (
        api._review_gate_due_store.snapshot("jiten:test", EpisodeReviewIdentity(210482, 1))["granted"] is True
    )


def _episode_due_cards(tmp_path: Path) -> list[dict]:
    """Cards as review_gate_begin sends them in "all due words of the episode" mode."""
    subtitle = tmp_path / "episode.srt"
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:03,000\n猫が走る。\n\n2\n00:00:04,000 --> 00:00:06,000\n犬も走る。\n",
        encoding="utf-8",
    )
    cards = build_episode_due_review_cards(_FakeJiten(), subtitle, tmp_path / "cache")["cards"]
    return json.loads(json.dumps(cards, ensure_ascii=False))


def _all_due_gate(cards: list[dict]) -> str:
    return (
        GATE
        + f"""
    window.GATE_CARDS = {json.dumps(cards, ensure_ascii=False)};
    const plainBegin = __api.review_gate_begin;
    __api.review_gate_status = async () => ({{supported: true, blocking: true, granted: false, all_due_episode_words: true,
                                             required: 2, completed: 0, remaining: 2, episode: 3}});
    __api.review_gate_begin = async path => ({{...(await plainBegin(path)), all_due_episode_words: true}});
    """
    )


def test_review_gate_shows_the_episode_sentence_and_optional_image(tmp_path: Path, page) -> None:
    # Due words of the episode have no Jiten example: the gate shows the line of the episode
    # where the word occurs, and the Jiten card image when there is one.
    for lang in ("en", "ru"):
        result = page.run(
            GATE_VIEW
            + """
            const answer = () => ({example: __text('.pudge-review-gate-example'), image: gate().querySelector('.pudge-review-gate-image img')?.getAttribute('src') ?? null,
                                   imageShown: shown('.pudge-review-gate-image')});
            await startPlay(EPISODE); await __tick(20);
            await space(); const first = answer();
            await gradeKey(3); await space(); await __tick(10);
            await space(); const second = answer();
            return {first, second, label: PudgeI18n.t('gate.example')};
            """,
            lang=lang,
            before=_all_due_gate(_episode_due_cards(tmp_path)),
        )
        label = result.value["label"]
        assert result.value["first"] == {
            "example": f"{label}\n猫が走る。",
            "image": "https://img.test/cat.jpg",
            "imageShown": True,
        }
        assert result.value["second"] == {
            "example": f"{label}\n猫が走る。",
            "image": None,
            "imageShown": False,
        }
    # A large image cannot push the answer out of the card.
    image = css_rules("review_gate.css").declarations(".pudge-review-gate-image img")
    assert (image["max-width"], image["max-height"], image["object-fit"]) == ("100%", "320px", "contain")


def test_revealed_episode_due_word_keeps_its_kanji(tmp_path: Path, page) -> None:
    result = page.run(
        GATE_VIEW
        + """
        await startPlay(EPISODE); await __tick(20);
        const closed = frontWord().innerText;
        await space();
        const base = frontWord().cloneNode(true); base.querySelectorAll('rt').forEach(rt => rt.remove());
        return {closed, base: base.textContent.trim(), ruby: frontWord().querySelector('rt')?.textContent ?? null};
        """,
        before=_all_due_gate(_episode_due_cards(tmp_path)),
    )
    assert result.value == {"closed": "猫", "base": "猫", "ruby": "ねこ"}


def test_all_due_episode_words_setting_is_offered_with_the_gate(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            const box = $('s_review_gate_all_due_episode_words');
            const out = {label: box.closest('label').innerText, expected: PudgeI18n.t('ui.reviewAllQueuedWordsFrom'),
                         checked: box.checked, enabled: !box.disabled};
            await __input('#s_review_gate_all_due_episode_words', true);
            out.countEnabled = !$('s_review_gate_count').disabled;
            await __click('#saveSettings'); await __tick(10);
            return out;
            """,
            lang=lang,
            state={
                "settings": {
                    "review_gate_enabled": True,
                    "review_gate_all_due_episode_words": False,
                    "light_novels": {"jiten_api_key": "key", "study_backend": "jiten"},
                }
            },
            before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
        )
        out = result.value
        assert out["label"].strip() == out["expected"]
        assert (out["checked"], out["enabled"], out["countEnabled"]) == (
            False,
            True,
            False,
        )  # all due: no fixed count
        [[values]] = result.calls_of("save_settings")
        assert values["review_gate_all_due_episode_words"] is True


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
    tmp_path: Path,
    monkeypatch,
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
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("stored text sid should avoid rescanning")
        ),
    )

    def fake_run(args, **_kwargs):
        calls.append([str(value) for value in args])
        Path(args[-1]).write_text("1\n00:00:01,000 --> 00:00:02,000\nやれやれだぜ。\n", encoding="utf-8")
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


def test_review_gate_grade_keys_follow_the_active_action_set(page) -> None:
    # Binary review mode: the gate offers Fail / Pass; their keys (also on the numpad) select
    # the button, Space confirms it; a key of the hidden native set does nothing.
    result = page.run(
        GATE_VIEW
        + """
        await startPlay(EPISODE); await __tick(20);
        await space();
        const out = {grades: [...gate().querySelectorAll('[data-review-gate-grade]')].map(b => b.dataset.reviewGateGrade)};
        out.focused = document.activeElement?.dataset.reviewGateGrade ?? null;
        await __key('1', {code: 'Numpad1'}, 'body'); out.numpad = document.activeElement?.dataset.reviewGateGrade ?? null;
        await gradeKey(3); out.three = document.activeElement?.dataset.reviewGateGrade ?? null;
        await gradeKey(2); out.two = document.activeElement?.dataset.reviewGateGrade ?? null;
        await space(); await __tick(10);
        return out;
        """,
        before=GATE
        + """
        const binary = {provider: 'jiten', mode: 'binary', actions: [
          {id: 'fail', wire: 'again', tone: 'again', shortcut: '1'}, {id: 'pass', wire: 'good', tone: 'good', shortcut: '2'}]};
        const begin = __api.review_gate_begin;
        __api.review_gate_begin = async path => ({...(await begin(path)), review_actions: binary});
        """,
    )
    out = result.value
    assert out["grades"] == ["fail", "pass"]
    assert out["focused"] == "pass"  # Pass is preselected after the reveal
    assert (out["numpad"], out["three"], out["two"]) == ("fail", "fail", "pass")
    assert [call[1:4] for call in result.calls_of("review_gate_review")] == [[1, 0, "pass"]]


def test_word_card_grade_keys_select_and_space_confirms(page) -> None:
    result = page.run(
        """
        await __settle(openLightNovel(6)); await __tick(300);
        await __click('#lnReader [data-ln-token="0:2"]'); await __tick(20);
        await __key('3', {code: 'Digit3'}, 'body');
        const focused = document.activeElement?.dataset.pudgeStudyReview;
        await __key(' ', {code: 'Space'}, 'body'); await __tick(20);
        return {focused, open: document.getElementById('pudgeStudyCard').classList.contains('open')};
        """,
        before=LN_BOOK + "__api.study_action = async () => ({ok: true, outcome: 'confirmed'});",
        state={"settings": {"review_gate_ln_enabled": False}},
    )
    assert result.value == {"focused": "good", "open": False}
    [[action]] = result.calls_of("study_action")
    assert (action["action"], action["word_id"], action["reading_index"], action["grade"]) == (
        "review",
        103,
        0,
        "good",
    )


# The minute refresh keeps a warm queue of review cards (window.WARM answers it).
WARM = (
    CARD
    + """
window.WARM = {cards: [reviewCard(9, '古', 'ふる')], target: 1};
__api.review_gate_prefetch = async () => window.WARM;
__api.review_gate_status = async () => ({supported: true, blocking: true, granted: false, required: 1, completed: 0, remaining: 1, episode: 3});
__api.review_gate_begin = () => new Promise(() => {});                 // authorization still running
"""
)


def test_review_gate_paints_warm_cards_only_outside_the_episode_due_mode(page) -> None:
    open_gate = (
        GATE_VIEW
        + """
    await __tick(10);
    MODE;
    void startPlay('/lib/Show - 03.mkv'); await __tick(20);
    return {word: frontWord()?.innerText ?? null, loading: shown('.pudge-review-gate-loading')};
    """
    )
    # Normal gate: the warm card is on screen at once while the gate authorizes it.
    normal = page.run(open_gate.replace("MODE", ""), before=WARM).value
    assert normal == {"word": "古", "loading": False}
    # The backend switched to "all due words of the episode": the warm queue is dropped.
    switched = page.run(
        open_gate.replace(
            "MODE",
            "window.WARM = {reason: 'episode_due_mode', cards: [], target: 0}; window.dispatchEvent(new Event('focus')); await __tick(10)",
        ),
        before=WARM,
    ).value
    assert switched == {"word": None, "loading": True}
    # A gate opened in that mode never shows warm cards, even when some are left.
    all_due = page.run(
        open_gate.replace(
            "MODE",
            "__api.review_gate_status = async () => ({supported: true, blocking: true, granted: false, all_due_episode_words: true, required: 1, completed: 0, remaining: 1})",
        ),
        before=WARM,
    ).value
    assert all_due == {"word": None, "loading": True}


def test_word_card_appears_next_to_the_word_in_the_same_task(page) -> None:
    # The card used to be placed one frame later and flashed at its static place (top left).
    result = page.run(
        """
        await __settle(openLightNovel(6)); await __tick(300);
        const word = document.querySelector('#lnReader [data-ln-token="0:2"]');
        word.__rect = {x: 300, y: 200, width: 40, height: 30};
        word.click();                                                      // no frame runs before the check
        const card = document.getElementById('pudgeStudyCard');
        return {open: card.classList.contains('open'), left: card.style.left, top: card.style.top};
        """,
        before=LN_BOOK,
        state={"settings": {"review_gate_ln_enabled": False}},
        viewport={"width": 1300, "height": 900},
    )
    assert result.value == {"open": True, "left": "320px", "top": "240px"}
    # The grade chosen with its key is clearly marked.
    assert css_rules("reading_tools.css").get(".pudge-study-grade:focus", "outline") == "2px solid #eef5ff"
