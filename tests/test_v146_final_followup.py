from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.css_support import css_rules

ROOT = Path(__file__).resolve().parents[1]


def test_reader_scrolls_natively(page) -> None:
    # The reader's wheel listener only notes manual navigation; it never takes over scrolling.
    result = page.run(
        r"""
        $('lnReaderShell').classList.add('open');
        const scroll = $('lnReaderScroll');
        scroll.scrollTop = 300;
        const wheel = new WheelEvent('wheel', {deltaY: 120, bubbles: true, cancelable: true});
        scroll.dispatchEvent(wheel);
        await __tick(500);
        return {prevented: wheel.defaultPrevented, top: scroll.scrollTop};
        """,
    )
    assert result.value == {"prevented": False, "top": 300}


COVER_DRAG = r"""
setPage('lightnovels'); await __tick(50);
const cover = document.querySelector('#lightNovelsContent [data-ln-book="1"] img.ln-card-cover');
cover.__rect = {x: 100, y: 100, width: 120, height: 170};
const sel = '#lightNovelsContent [data-ln-book="1"] img.ln-card-cover';
const pointer = (type, x, y) => __fire(sel, type, {button: 0, pointerId: 7, pointerType: 'mouse', clientX: x, clientY: y}, 'PointerEvent');
const peek = () => document.querySelectorAll('.pudge-cover-peek').length;
"""


def test_cover_preview_waits_for_a_real_drag(page) -> None:
    result = page.run(
        COVER_DRAG
        + r"""
        await pointer('pointerdown', 150, 150);
        const pressed = peek();
        await pointer('pointermove', 151, 150);                  // a jitter of 1 px is not a drag
        const jitter = peek();
        await pointer('pointermove', 180, 150);
        const dragging = {peek: peek(), preview: PudgeCoverPreview.isOpen()};
        await pointer('pointermove', 320, 150);                  // far enough: the preview opens
        const opened = PudgeCoverPreview.isOpen();
        await pointer('pointerup', 320, 150);
        PudgeCoverPreview.close();
        await pointer('pointerdown', 150, 150);                  // a plain click shows nothing
        await pointer('pointerup', 150, 150);
        await __tick(300);
        return {pressed, jitter, dragging, opened, click: {peek: peek(), preview: PudgeCoverPreview.isOpen()}};
        """,
        api={
            "light_novel_state": {
                "version": 1,
                "settings": {},
                "books": [
                    {
                        "id": 1,
                        "title": "Novel",
                        "volume": 1,
                        "character_count": 1000,
                        "cover_url": "covers/novel.jpg",
                    }
                ],
            }
        },
    )
    out = result.value
    assert (out["pressed"], out["jitter"]) == (0, 0)
    assert out["dragging"] == {"peek": 1, "preview": False}
    assert out["opened"] is True
    assert out["click"] == {"peek": 0, "preview": False}


def test_anime_cards_have_no_webkit_paint_containment() -> None:
    # content-visibility / contain on anime cards made WebKit drop their painting while scrolling.
    for sheet in ["index.html", *sorted(path.name for path in (ROOT / "pudge" / "web").glob("*.css"))]:
        for prop in ("content-visibility", "contain"):
            assert css_rules(sheet).get(".anime-card", prop, media=None) is None, (sheet, prop)


# Home: a downloaded episode that waits for Japanese subtitles.
WAITING = {
    "media_id": 42,
    "title": "Show",
    "next_episode": 3,
    "media_status": "RELEASING",
    "japanese_subtitles_required": True,
    "site_url": "https://anilist.co/anime/42",
    "local": {"video_path": "/library/Show - 03.mkv", "episode": 3, "state": "waiting_subtitles"},
}


def test_subtitle_requirement_toggle_updates_the_card_at_once(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const section = () => document.querySelector('#currentContent [data-media-id="42"]')?.closest('section')?.querySelector('h2, h3')?.innerText;
            const before = {text: __text('#currentContent [data-media-id="42"]'), section: section()};
            __api.get_state_fast = __api.get_state = () => new Promise(() => {});   // the full state is slow
            await __fire('#currentContent [data-media-id="42"]', 'contextmenu', {clientX: 20, clientY: 20}, 'MouseEvent');
            const item = document.querySelector('#contextMenu [data-context-action=toggle-jp-subs]');
            const label = item.innerText;
            item.click(); await __tick(50);
            return {before, label, after: {text: __text('#currentContent [data-media-id="42"]'), section: section()}, toasts: __toasts(),
                    texts: {toggle: t('ui.doNotRequireJapaneseSubtitles'), optional: t('ui.japaneseSubtitlesAreNowOptional'),
                            waiting: t('label.waitingSubs'), ready: t('section.newReady')}};
            """,
            lang=lang,
            state={"home": {"waiting": [WAITING]}},
            api={
                "set_anime_japanese_subtitles_required": {
                    "media_id": 42,
                    "japanese_subtitles_required": False,
                    "ui_state_version": "2",
                }
            },
        )
        out = result.value
        texts = out["texts"]
        assert out["label"] == texts["toggle"]
        assert result.calls_of("set_anime_japanese_subtitles_required") == [[42, False]]
        assert texts["waiting"] in out["before"]["text"]
        # Without waiting for a new state, the episode is ready to watch.
        assert texts["waiting"] not in out["after"]["text"] and out["after"]["section"] == texts["ready"]
        assert texts["optional"] in out["toasts"]


def test_planning_card_marks_titles_whose_japanese_subtitles_are_optional(page) -> None:
    planned = [
        {"media_id": 1, "title": "Optional", "japanese_subtitles_required": False},
        {"media_id": 2, "title": "Required", "japanese_subtitles_required": True},
    ]
    result = page.run(
        r"""
        setPage('planned'); await __tick(50);
        const tag = id => document.querySelector(`#plannedContent [data-media-id="${id}"] .jp-optional-tag`);
        return {optional: [tag(1)?.innerText, tag(1)?.getAttribute('title')], required: tag(2), tip: t('uiA.home.japaneseSubtitlesOptional')};
        """,
        state={"planned": planned},
    )
    out = result.value
    assert out["optional"] == ["JP×", out["tip"]] and out["required"] is None


def test_light_novel_menu_offers_voice_markup_unless_an_ordinary_audiobook_is_paired(page) -> None:
    code = r"""
        await __fire('#lightNovelsContent [data-ln-book="9"]', 'contextmenu', {clientX: 20, clientY: 20}, 'MouseEvent');
        return [...document.querySelectorAll('#contextMenu [data-ln-context-action]')].map(b => b.dataset.lnContextAction);
    """
    for kind, expected in (("none", True), ("generated", True), ("ordinary", False)):
        book = {
            "id": 9,
            "title": "Novel",
            "volume": 1,
            "character_count": 1000,
            "irodori_tts": {"paired_kind": kind},
        }
        actions = page.run(
            code, api={"light_novel_state": {"version": 1, "settings": {}, "books": [book]}}
        ).value
        assert ("export-speaker-markup" in actions and "import-speaker-markup" in actions) is expected, kind


def test_torrent_pack_plan_groups_exact_file_ids_by_volume() -> None:
    from pudge.audiobooks import audiobook_torrent_pack_plan

    result = audiobook_torrent_pack_plan(
        [
            {"index": 4, "name": "Spice and Wolf Vol 01.m4b", "size": 100},
            {"index": 7, "name": "Spice and Wolf Vol 02.m4b", "size": 200},
            {"index": 8, "name": "cover.jpg", "size": 5},
        ]
    )
    assert result["selective"] is True
    assert [row["volume"] for row in result["volumes"]] == [1, 2]
    assert [row["file_ids"] for row in result["volumes"]] == [[4], [7]]
    archive = audiobook_torrent_pack_plan([{"index": 3, "name": "all-volumes.7z", "size": 999}])
    assert archive["selective"] is False
    assert archive["reason"] == "single_archive"
    assert archive["archive_file_ids"] == [3]


def test_anilist_romaji_reading_is_canonical_for_ayanokouji() -> None:
    from pudge.light_novels import _romaji_to_hiragana

    assert _romaji_to_hiragana("Ayanokouji") == "あやのこうじ"
    assert _romaji_to_hiragana("Kiyotaka") == "きよたか"


def test_irodori_voice_seed_is_stable_per_character_and_caption() -> None:
    from pudge.web_app import WebAppApi

    a = WebAppApi._irodori_voice_seed("Ayanokouji Kiyotaka", "calm young male")
    b = WebAppApi._irodori_voice_seed("  AYANOKOUJI   KIYOTAKA ", "calm young male")
    c = WebAppApi._irodori_voice_seed("Ayanokouji Kiyotaka", "older male")
    assert a == b
    assert 1 <= a < 2_147_483_647
    assert c != a


def test_per_anime_subtitle_requirement_is_reversible_and_does_not_delete_subtitles(tmp_path: Path) -> None:
    from pudge.manager import AnimeManager

    video = tmp_path / "episode.mkv"
    video.write_bytes(b"video")
    subtitle = tmp_path / "episode.srt"
    subtitle.write_text("kept", encoding="utf-8")
    episode = SimpleNamespace(
        video_path=video, subtitle_path=subtitle, media_id=42, episode=1, state="waiting_subtitles"
    )

    class DB:
        def __init__(self):
            self.state: dict[str, str] = {}
            self.deleted_jobs: list[Path] = []
            self.deleted_states: list[str] = []
            self.queued: list[Path] = []

        def get_state(self, key, default=""):
            return self.state.get(key, default)

        def set_state(self, key, value):
            self.state[key] = str(value)

        def delete_state(self, key):
            self.deleted_states.append(str(key))
            self.state.pop(key, None)

        def episodes(self, media_id=None):
            return [episode] if media_id in (None, 42) else []

        def delete_subtitle_job(self, path):
            self.deleted_jobs.append(Path(path))

        def ensure_subtitle_job(self, path, media_id, ep):
            self.queued.append(Path(path))

    manager = object.__new__(AnimeManager)
    manager.db = DB()
    manager.logger = SimpleNamespace(info=lambda *args, **kwargs: None)
    assert manager.japanese_subtitles_required(42) is True
    assert manager.set_japanese_subtitles_required(42, False) is False
    assert manager.japanese_subtitles_required(42) is False
    assert manager.db.deleted_jobs == [video]
    assert subtitle.read_text(encoding="utf-8") == "kept"
    assert manager.set_japanese_subtitles_required(42, True) is True
    assert manager.japanese_subtitles_required(42) is True
    assert manager.db.queued == [video]


def test_web_payload_contains_top_level_japanese_subtitle_requirement(tmp_path: Path, monkeypatch) -> None:
    from pudge.manager_models import LibraryAnime
    from test_web_app import make_api

    api = make_api(tmp_path)
    api.manager.db.upsert_anime(LibraryAnime(42, "Show", status="CURRENT", episodes=12))
    assert api.get_state_fast()["current"][0]["japanese_subtitles_required"] is True
    # The toggle answers without rebuilding the state (that took seconds on large libraries).
    monkeypatch.setattr(
        api, "get_state_fast", lambda *a, **k: pytest.fail("no state rebuild on the click path")
    )
    monkeypatch.setattr(api, "get_state", lambda *a, **k: pytest.fail("no state rebuild on the click path"))
    answer = api.set_anime_japanese_subtitles_required(42, False)
    assert answer == {
        "media_id": 42,
        "japanese_subtitles_required": False,
        "ui_state_version": api.manager.db.get_state("ui_state_version", ""),
    }
    monkeypatch.undo()
    assert api.get_state_fast()["current"][0]["japanese_subtitles_required"] is False
    # The watch-order (relations) payload carries it too.
    anime = api.manager.db.get_anime(42)
    assert api._relation_payload(anime)["current"]["japanese_subtitles_required"] is False


def test_stt_audio_selection_prefers_japanese_and_refuses_ambiguous_dual_audio(
    monkeypatch, tmp_path: Path
) -> None:
    from pudge.subtitles import stt

    video = tmp_path / "dual.mkv"
    video.write_bytes(b"video")

    def run_japanese(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "index": 1,
                            "codec_name": "aac",
                            "tags": {"language": "eng"},
                            "disposition": {"default": 1},
                        },
                        {
                            "index": 2,
                            "codec_name": "flac",
                            "tags": {"language": "jpn", "title": "Japanese"},
                            "disposition": {"default": 0},
                        },
                    ]
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(stt.subprocess, "run", run_japanese)
    index, info = stt._select_japanese_audio_stream(video, "ffprobe")
    assert index == 2
    assert info["language"] == "jpn"
    assert info["selection_reason"] == "japanese_language_tag"

    def run_ambiguous(*args, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "streams": [
                        {"index": 1, "codec_name": "aac", "tags": {}, "disposition": {"default": 1}},
                        {"index": 2, "codec_name": "aac", "tags": {}, "disposition": {"default": 0}},
                    ]
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(stt.subprocess, "run", run_ambiguous)
    index, info = stt._select_japanese_audio_stream(video, "ffprobe")
    assert index is None
    assert info["selection_reason"] == "ambiguous_multiple_audio_streams"
    assert info["stream_count"] == 2


def test_stt_rejected_text_clock_is_preserved_with_explicit_gate_failures(
    monkeypatch, tmp_path: Path
) -> None:
    import pudge.syncing as syncing

    source = tmp_path / "source.srt"
    reference = tmp_path / "reference.srt"
    lines = []
    for i in range(1, 7):
        start = i * 2
        lines.append(
            f"{i}\n00:00:{start:02d},000 --> 00:00:{start + 1:02d},000\nこれは十分に長い日本語テキストです{i}\n"
        )
    source.write_text("\n".join(lines), encoding="utf-8")
    reference.write_text("\n".join(lines), encoding="utf-8")

    monkeypatch.setattr(
        syncing,
        "align_light_novel_to_transcript",
        lambda *a, **k: {
            "alignment_method": "test",
            "matched_anchor_count": 6,
            "anchor_count": 6,
            "confidence": 0.9,
            "frontier": None,
            "frontier_reason": "not_applicable",
        },
    )
    monkeypatch.setattr(
        syncing,
        "audio_position_for_light_novel_offset",
        lambda alignment, chapter, offset: float(offset) / 5.0,
    )
    monkeypatch.setattr(
        syncing,
        "_stt_alass_transition_safety",
        lambda *a, **k: {
            "accepted": False,
            "reason": "large_transition_without_real_gap",
            "unsupported_transition_count": 2,
        },
    )
    monkeypatch.setattr(
        syncing,
        "_subtitle_stt_text_score",
        lambda *a, **k: {
            "available": True,
            "score": 0.8,
            "coverage": 1.0,
        },
    )
    monkeypatch.setattr(
        syncing,
        "compare_timing_activity",
        lambda *a, **k: {
            "available": True,
            "weighted": 0.8,
        },
    )

    output, diagnostics = syncing._stt_text_clock_candidate(
        source, reference, tmp_path / "cache", model="tiny"
    )
    assert output is None
    assert diagnostics["accepted"] is False
    assert diagnostics["reject_reason"] == "transition_safety:large_transition_without_real_gap"
    assert diagnostics["gate_failures"] == ["transition_safety:large_transition_without_real_gap"]
    rejected = Path(str(diagnostics["rejected_output"]))
    assert rejected.is_file()
    assert diagnostics["segment_count"] == 6
    assert diagnostics["anchor_count"] == 6
    assert "shift_spread_seconds" in diagnostics


def test_stt_cache_key_is_path_independent_for_same_content_identity() -> None:
    from pudge.subtitles.stt import _cache_key

    assert _cache_key("same-fingerprint", "tiny", 2) == _cache_key("same-fingerprint", "tiny", 2)
    assert _cache_key("same-fingerprint", "tiny", 2) != _cache_key("same-fingerprint", "tiny", 1)


def test_ln_store_title_keeps_local_volume_but_matches_base_anilist_work() -> None:
    from pudge.light_novels import LightNovelService, _series_key, _series_title, _volume_from_text

    title = "ようこそ実力至上主義の教室へ２<ようこそ実力至上主義の教室へ> (MF文庫J)"
    assert _volume_from_text(title) == 2
    assert _series_title(title) == "ようこそ実力至上主義の教室へ"
    assert LightNovelService._anilist_search_text(title) == "ようこそ実力至上主義の教室へ"
    assert LightNovelService._match_title(title) == "ようこそ実力至上主義の教室へ"
    assert _series_key(title) == _series_key("ようこそ実力至上主義の教室へ 第1巻")


# Two series of one franchise.
FRANCHISE = [
    {
        "id": i,
        "title": f"{series} {volume}",
        "series_key": series.lower(),
        "series_title": series,
        "volume": volume,
        "character_count": 1000,
        "franchise_key": "spice",
        "franchise_title": "Spice and Wolf",
        "franchise_order": order,
    }
    for i, (series, volume, order) in enumerate(
        [("Spice", 1, 1), ("Spice", 2, 1), ("Spring Log", 1, 2)], start=1
    )
]


def test_ln_franchise_tile_selects_all_its_volumes(page) -> None:
    result = page.run(
        r"""
        setPage('lightnovels'); await __tick(50);
        const tile = '#lightNovelsContent [data-ln-franchise-ids]';
        const tiles = document.querySelectorAll(tile).length;
        await __click(tile, {metaKey: true});
        const selected = [...ui.lnSelection].sort();
        const marked = document.querySelector(tile).classList.contains('franchise-selected');
        await __click(tile, {metaKey: true});
        return {tiles, selected, marked, cleared: [...ui.lnSelection]};
        """,
        api={"light_novel_state": {"version": 1, "settings": {}, "books": FRANCHISE}},
    )
    assert result.value == {"tiles": 1, "selected": [1, 2, 3], "marked": True, "cleared": []}


def test_ln_franchise_tile_opens_all_its_series_in_one_panel(page) -> None:
    result = page.run(
        r"""
        setPage('lightnovels'); await __tick(50);
        await __click('#lightNovelsContent [data-ln-franchise-ids]'); await __tick(100);
        const panel = document.getElementById('lnVolumePanel');
        return {open: __visible('#lnVolumePanel'), volumes: [...panel.querySelectorAll('[data-ln-book]')].map(node => node.dataset.lnBook),
                text: panel.innerText};
        """,
        api={"light_novel_state": {"version": 1, "settings": {}, "books": FRANCHISE}},
    )
    out = result.value
    assert out["open"] is True and out["volumes"] == ["1", "2", "3"]
    assert out["text"].index("Spice") < out["text"].index("Spring Log")  # franchise order
    rules = css_rules("index.html")
    # Shelves span the whole grid row, and cards are never reordered to fill holes
    # (dense packing moved volumes around).
    assert rules.get(".ln-franchise-group", "grid-column") == "1/-1"
    assert not [rule for rule in rules.rules if "dense" in rule[2].get("grid-auto-flow", "")]


def test_manual_speaker_markup_accepts_character_owned_narration(tmp_path: Path) -> None:
    from pudge.web_app import WebAppApi

    units = [
        {"id": 0, "text": "地の文。", "dialogue": False},
        {"id": 1, "text": "「台詞」", "dialogue": True},
    ]
    path = tmp_path / "manual.json"
    path.write_text(
        json.dumps(
            {
                "assignments": [
                    {"id": 0, "speaker": "綾小路清隆", "caption": "低く落ち着いた声。"},
                    {"id": 1, "speaker": "堀北鈴音", "caption": "澄んだ声。"},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    loaded = WebAppApi._load_audiobook_speaker_assignments(path, units)
    assert loaded[0]["speaker"] == "綾小路清隆"
    assert loaded[1]["speaker"] == "堀北鈴音"


def test_speaker_markup_export_lists_narration_spans_and_asks_for_character_narration(
    tmp_path: Path, monkeypatch
) -> None:
    import zipfile

    from test_web_app import make_api

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    api = make_api(tmp_path)
    novel = tmp_path / "Novel.txt"
    novel.write_text("第一章\n俺は窓の外を見た。\n「おはよう」\n彼女は笑った。", encoding="utf-8")
    book = api.light_novels.import_file(novel)
    exported = api.export_light_novel_speaker_markup(int(book["id"]))
    assert exported["dialogue_count"] == 1
    with zipfile.ZipFile(exported["path"]) as archive:
        [chapter_name] = [name for name in archive.namelist() if name.startswith("books/")]
        chapter = json.loads(archive.read(chapter_name))
        instructions = archive.read("INSTRUCTIONS.md").decode("utf-8")
    kinds = {segment["id"]: segment["kind"] for segment in chapter["segments"]}
    assert chapter["dialogue_ids"] == [i for i, kind in kinds.items() if kind == "dialogue"] == [1]
    assert chapter["narration_ids"] == [i for i, kind in kinds.items() if kind == "narration"] == [0, 2]
    # The annotator is asked to voice first-person narration too, not only dialogue.
    assert (
        "Also annotate a `narration` span when it is clearly voiced by a specific character" in instructions
    )
