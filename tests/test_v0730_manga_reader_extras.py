"""Series word fixes, unreliable-text flags, background OCR, volume words and manga card adds.

Invented words, procedural pages and fake providers only (see tests/OCR_TESTS.md).
"""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from css_support import css_rules

from pudge import manga_lexicon as lexicon
from pudge import web_app as web_app_module
from pudge.config import load_config, write_config
from pudge.database import Database
from pudge.manga import MangaService
from pudge.manga_mokuro import mokuro_page_regions
from pudge.web_app import WebAppApi
from tests.t71g06_support import reader
from tests.test_web_app import make_api


# ---------------------------------------------------------------- series word fixes


def test_rare_lookalike_spelling_of_a_frequent_series_word_is_fixed() -> None:
    counts = {"ブランコロ": 40, "フランコロ": 1, "パンッ": 1, "パンツ": 90, "ハラ": 1, "バラ": 300}
    fixes = lexicon.build_fixes(counts)
    assert fixes == {"フランコロ": "ブランコロ"}
    # a final small ッ is a sound effect; two-letter words are too ambiguous


def test_shape_lookalikes_need_a_very_frequent_word() -> None:
    assert lexicon.build_fixes({"モルタ": 60, "モレタ": 1}) == {}
    assert lexicon.build_fixes({"モルタ": 150, "モレタ": 1}) == {"モレタ": "モルタ"}


def test_a_spelling_seen_often_is_not_a_misread() -> None:
    assert lexicon.build_fixes({"ブランコロ": 40, "フランコロ": 5}) == {}


def test_lookalike_kanji_inside_a_katakana_word_is_fixed() -> None:
    counts = lexicon.count_terms(["ガロンだ"] * 35 + ["ガ口ンだ"])
    assert lexicon.build_fixes(counts) == {"ガ口ン": "ガロン"}


def test_region_fix_keeps_character_boxes_in_step_and_never_touches_hand_edits() -> None:
    fixes = {"フランコロ": "ブランコロ"}
    region = {
        "text": "フランコロだ",
        "segments": [
            {"text": ch, "x": 0.1, "y": 0.1, "width": 0.01, "height": 0.01} for ch in "フランコロだ"
        ],
    }
    fixed = lexicon.fix_region(region, fixes)
    assert fixed["text"] == "ブランコロだ" and fixed["raw_text"] == "フランコロだ"
    assert "".join(segment["text"] for segment in fixed["segments"]) == "ブランコロだ"
    assert fixed["lexicon_fixes"] == [{"from": "フランコロ", "to": "ブランコロ"}]
    mismatched = {"text": "フランコロだ", "segments": [{"text": "フランコロ"}, {"text": "だ"}]}
    assert lexicon.fix_region(mismatched, fixes) is mismatched


def _series_book(tmp_path: Path, service: MangaService, name: str, lines: list[str]) -> int:
    archive = tmp_path / f"{name}.cbz"
    image = tmp_path / "page.png"
    Image.new("RGB", (400, 600), "white").save(image)
    with zipfile.ZipFile(archive, "w") as zf:
        zf.write(image, "01.png")
    book_id = int(service.import_file(archive)["id"])
    blocks = [
        {"box": [300, 10 + 14 * index, 340, 20 + 14 * index], "vertical": True, "lines": [text]}
        for index, text in enumerate(lines)
    ]
    sidecar = tmp_path / f"{name}.mokuro"
    sidecar.write_text(
        json.dumps(
            {"pages": [{"img_path": "01.png", "img_width": 400, "img_height": 600, "blocks": blocks}]}
        ),
        encoding="utf-8",
    )
    service.import_mokuro(book_id, sidecar)
    return book_id


def test_series_vocabulary_fixes_a_misread_in_the_reader_and_study_text(tmp_path: Path) -> None:
    service = MangaService(Database(tmp_path / "pudge.db"), cache_dir=tmp_path / "cache")
    first = _series_book(tmp_path, service, "Kumo Saga 1", ["ブランコロ"] * 20)
    second = _series_book(tmp_path, service, "Kumo Saga 2", ["ブランコロ"] * 19 + ["フランコロ"])
    other = _series_book(tmp_path, service, "Other Tale 1", ["フランコロ"])
    regions = service.text_regions(second, 0, cached_only=True)["regions"]
    fixed = [region for region in regions if region.get("lexicon_fixes")]
    assert len(fixed) == 1 and fixed[0]["text"] == "ブランコロ" and fixed[0]["raw_text"] == "フランコロ"
    assert "フランコロ" not in [text for _page, text in service.cached_region_texts(second)]
    # Another series has its own vocabulary.
    assert service.text_regions(other, 0, cached_only=True)["regions"][0]["text"] == "フランコロ"
    assert first != second


# ---------------------------------------------------------------- unreliable text and duplicates


def _line(left: float, top: float, right: float, bottom: float) -> list[list[float]]:
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def test_paragraphs_of_small_print_and_tile_patterns_are_marked_unreliable() -> None:
    paragraph = {
        "box": [20, 20, 380, 120],
        "vertical": False,
        "font_size": 12,
        "lines": ["あいうえおかきくけこさしすせそたちつてと"] * 3,
        "lines_coords": [_line(20, 20 + 30 * i, 380, 44 + 30 * i) for i in range(3)],
    }
    bubble = {
        "box": [300, 200, 340, 330],
        "vertical": True,
        "font_size": 30,
        "lines": ["そうかな", "たぶんね"],
        "lines_coords": [_line(310, 200, 340, 330), _line(300, 200, 309, 330)],
    }
    tiles = {
        "box": [20, 500, 300, 540],
        "vertical": False,
        "font_size": 30,
        "lines": ["月日月日日月"],
        "lines_coords": [_line(20, 500, 300, 540)],
    }
    calendar = {
        "box": [20, 400, 120, 440],
        "vertical": False,
        "font_size": 30,
        "lines": ["三日月"],
        "lines_coords": [_line(20, 400, 120, 440)],
    }
    page = {"img_width": 400, "img_height": 600, "blocks": [paragraph, bubble, tiles, calendar]}
    first, second, third, fourth = mokuro_page_regions(page, 400, 600, "p.png")
    assert first["ocr_quality"] == "dense_text"
    assert "ocr_quality" not in second
    assert third["ocr_quality"] == "pattern"
    assert "ocr_quality" not in fourth  # a real word made of box-shaped kanji


def test_nested_duplicate_block_is_kept_once() -> None:
    outer = {
        "box": [100, 100, 160, 300],
        "vertical": True,
        "lines": ["またあした"],
        "lines_coords": [_line(110, 100, 150, 300)],
    }
    inner = {
        "box": [105, 110, 155, 290],
        "vertical": True,
        "lines": ["またあした"],
        "lines_coords": [_line(110, 110, 150, 290)],
    }
    elsewhere = {
        "box": [200, 100, 260, 300],
        "vertical": True,
        "lines": ["またあした"],
        "lines_coords": [_line(210, 100, 250, 300)],
    }
    regions = mokuro_page_regions({"blocks": [inner, outer, elsewhere]}, 400, 600, "p.png")
    assert [region["text"] for region in regions] == ["またあした", "またあした"]
    assert regions[0]["width"] == round(60 / 400, 6)  # the larger box wins


# Page 1: small print (dense_text), a tile pattern, an ordinary bubble, and small print the
# user corrected by hand.
FLAGGED = [
    {
        "text": "ちいさなもじ",
        "x": 0.05,
        "y": 0.7,
        "width": 0.3,
        "height": 0.1,
        "orientation": "horizontal",
        "ocr_quality": "dense_text",
    },
    {
        "text": "月日月日",
        "x": 0.05,
        "y": 0.5,
        "width": 0.3,
        "height": 0.1,
        "orientation": "horizontal",
        "ocr_quality": "pattern",
    },
    {"text": "そうかな", "x": 0.7, "y": 0.5, "width": 0.1, "height": 0.3, "orientation": "vertical"},
    {
        "text": "なおした",
        "x": 0.05,
        "y": 0.2,
        "width": 0.3,
        "height": 0.1,
        "orientation": "horizontal",
        "ocr_quality": "dense_text",
    },
]
LLM = {
    "settings": {
        "llm_enabled": True,
        "llm_model": "local",
        "llm_url": "http://localhost:11434",
        "llm_provider": "ollama",
    }
}


def test_reader_marks_unreliable_regions_and_offers_llm_explanation(page) -> None:
    result = reader(
        page,
        r"""
        const node = text => [...document.querySelectorAll('#mangaV2Pages .manga-v2-text-region')].find(n => n.getAttribute('aria-label') === text);
        const flagged = Object.fromEntries(regions().map(r => [r.text, {low: node(r.text).classList.contains('low-confidence'),
          title: node(r.text).querySelector('.manga-v2-selection-content').title}]));
        const titles = {dense: t('manga.denseSmallPrintOcrIs'), pattern: t('manga.looksLikeAPictureTiles'), plain: t('manga.selectOcrTextTripleClick')};
        const actions = await rightClick(regions().find(r => r.text === 'ちいさなもじ').index);
        await choose('explain'); await __tick(50);
        return {flagged, titles, menu: actions};
        """,
        regions={0: FLAGGED},
        state=LLM,
        before="__api.analyze_grammar = async () => ({status: 'ok', explanation: 'small print'});",
    )
    out = result.value
    titles = out["titles"]
    assert out["flagged"] == {
        "ちいさなもじ": {"low": True, "title": titles["dense"]},
        "月日月日": {"low": True, "title": titles["pattern"]},
        "そうかな": {"low": False, "title": titles["plain"]},
        "なおした": {"low": True, "title": titles["dense"]},
    }
    # Flagged bubbles get a dashed outline.
    assert "dashed" in css_rules("manga_reader_v2.css").get(".manga-v2-text-region.low-confidence", "outline")
    # The bubble's context menu offers an LLM explanation first; it explains the bubble in the
    # context of the page's text (in reading order).
    assert out["menu"] == ["explain"]
    [[sentence, context, _request]] = result.calls_of("analyze_grammar")
    assert sentence == "ちいさなもじ"
    assert context == "そうかな\nちいさなもじ\n月日月日\nなおした"


def test_no_llm_explanation_without_a_configured_llm(page) -> None:
    result = reader(page, "return await rightClick(0);", regions={0: FLAGGED[:1]})
    # Without an LLM a text region has no menu of its own (no OCR edits are offered).
    assert result.value == []


# ---------------------------------------------------------------- background OCR


class _Scheduler:
    def __init__(self, *, on_battery: bool | None = False, reason: str | None = None) -> None:
        self.on_battery = on_battery
        self.reason = reason

    def power_snapshot(self) -> dict:
        return {"on_battery": self.on_battery}

    def background_wait_reason(self):
        return self.reason


def _library_api(
    tmp_path: Path, *, enabled: bool = True, scheduler: _Scheduler | None = None
) -> tuple[WebAppApi, list[int]]:
    service = MangaService(Database(tmp_path / "pudge.db"), cache_dir=tmp_path / "cache")
    ids = []
    for name in ("Older 1", "Newer 1"):
        archive = tmp_path / f"{name}.cbz"
        Image.new("RGB", (40, 60), "white").save(tmp_path / "p.png")
        with zipfile.ZipFile(archive, "w") as zf:
            zf.write(tmp_path / "p.png", "01.png")
        ids.append(int(service.import_file(archive)["id"]))
        time.sleep(0.01)
    with service.db.connect() as conn:
        conn.execute("UPDATE manga_books SET updated_at=? WHERE id=?", ("2026-01-01T00:00:00", ids[0]))
        conn.execute("UPDATE manga_books SET updated_at=? WHERE id=?", ("2026-02-01T00:00:00", ids[1]))
    api = WebAppApi.__new__(WebAppApi)
    api.config = SimpleNamespace(tools=SimpleNamespace(manga_background_ocr=enabled, manga_mokuro_python=""))
    api.manager = SimpleNamespace(work_scheduler=scheduler or _Scheduler())
    api.manga = service
    import threading

    api._manga_book_ocr_lock = threading.Lock()
    api._manga_book_ocr_threads = {}
    api._manga_ocr_backend_available = lambda **_k: True
    return api, ids


def test_background_ocr_picks_the_most_recent_unfinished_volume_when_away_on_mains(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(web_app_module, "app_session_window_active", lambda: False)
    api, ids = _library_api(tmp_path)
    assert api._manga_background_ocr_candidate() == ids[1]
    # tried volumes wait half a day; the next one is offered meanwhile
    assert api._manga_background_ocr_candidate() == ids[0]
    assert api._manga_background_ocr_candidate() is None


def test_background_ocr_waits_for_mains_power_absence_and_setting(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(web_app_module, "app_session_window_active", lambda: False)
    assert _library_api(tmp_path / "a", enabled=False)[0]._manga_background_ocr_candidate() is None
    assert (
        _library_api(tmp_path / "b", scheduler=_Scheduler(on_battery=True))[
            0
        ]._manga_background_ocr_candidate()
        is None
    )
    assert (
        _library_api(tmp_path / "c", scheduler=_Scheduler(on_battery=None))[
            0
        ]._manga_background_ocr_candidate()
        is None
    )
    assert (
        _library_api(tmp_path / "d", scheduler=_Scheduler(reason="foreground"))[
            0
        ]._manga_background_ocr_candidate()
        is None
    )
    monkeypatch.setattr(web_app_module, "app_session_window_active", lambda: True)
    assert _library_api(tmp_path / "e")[0]._manga_background_ocr_candidate() is None


def test_background_ocr_setting_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    cfg = load_config(path)
    assert cfg.tools.manga_background_ocr is False
    cfg.tools.manga_background_ocr = True
    write_config(cfg, path)
    assert load_config(path).tools.manga_background_ocr is True
    # The window reads and saves it through the settings API.
    api = make_api(tmp_path / "app")
    assert api.get_state()["settings"]["manga_background_ocr"] is False
    api.save_settings({"manga_background_ocr": True})
    assert api.get_state()["settings"]["manga_background_ocr"] is True
    assert load_config(api.config.config_path).tools.manga_background_ocr is True
    # The app runs the library OCR loop as a supervised background task.
    assert {"name": "manga-background-ocr", "running": True} in [
        {"name": task["name"], "running": task["running"]} for task in api.task_supervisor.status()
    ]


def test_background_ocr_setting_in_the_settings_page(page) -> None:
    result = page.run(
        r"""
        const shown = $('s_manga_background_ocr').checked;
        const label = $('s_manga_background_ocr').closest('label').textContent.trim();
        await __input('#s_manga_background_ocr', false);
        await __click('#saveSettings'); await __tick(10);
        return {shown, label, expected: t('ui.ocrTheWholeLibraryIn')};
        """,
        state={"settings": {"manga_background_ocr": True}},
        before="__api.save_settings = async values => ({ok: true, settings: {...values, light_novels: {}}});",
    )
    assert result.value["shown"] is True and result.value["label"] == result.value["expected"]
    [[values]] = result.calls_of("save_settings")
    assert values["manga_background_ocr"] is False


# ---------------------------------------------------------------- volume words


class _Study:
    def __init__(self) -> None:
        self.parsed: list[str] = []

    def settings(self):
        return SimpleNamespace(study_backend="jiten")

    def parse_study_text(self, text: str) -> dict:
        self.parsed.append(text)
        words = {"かぜ": (1, 0, 50), "そら": (2, 0, 10), "うみ": (3, 0, 900), "やま": (4, 0, 5)}
        tokens, vocabulary = [], []
        for paragraph in text.split("\n"):
            row = []
            for word, (word_id, reading, rank) in words.items():
                for _ in range(paragraph.count(word)):
                    row.append({"wordId": word_id, "readingIndex": reading})
            tokens.append(row)
        for word, (word_id, reading, rank) in words.items():
            vocabulary.append(
                {
                    "wordId": word_id,
                    "readingIndex": reading,
                    "spelling": word,
                    "reading": f"{word}",
                    "meanings": [f"meaning {word_id}"],
                    "frequencyRank": rank,
                }
            )
        return {"tokens": tokens, "vocabulary": vocabulary}

    def jiten_live_states(self, pairs, force=False):
        states = {1: "new", 2: "known", 3: "new", 4: "learning"}
        return [{"wordId": w, "readingIndex": r, "normalizedState": states[w]} for w, r in pairs]


def test_volume_words_report_coverage_and_most_frequent_new_words(tmp_path: Path) -> None:
    api = WebAppApi.__new__(WebAppApi)
    study = _Study()
    api.light_novels = study
    seen: dict = {}

    def texts(book_id, *, include_unreliable=True):
        seen["include_unreliable"] = include_unreliable
        return [(0, "かぜ そら そら"), (1, "うみ かぜ やま"), (2, "そら")]

    api.manga = SimpleNamespace(cached_region_texts=texts)
    result = api.manga_volume_words(7, limit=5)
    assert seen["include_unreliable"] is False
    assert result["ok"] is True and result["tokens"] == 7 and result["unique_words"] == 4
    assert result["known_share"] == round(3 / 7, 4)
    assert result["known_or_learning_share"] == round(4 / 7, 4)
    assert [word["spelling"] for word in result["top_new"]] == ["かぜ", "うみ"]
    assert result["top_new"][0]["count"] == 2


def test_volume_words_without_ocr_explain_why(tmp_path: Path) -> None:
    api = WebAppApi.__new__(WebAppApi)
    api.light_novels = _Study()
    api.manga = SimpleNamespace(cached_region_texts=lambda book_id, include_unreliable=True: [])
    assert api.manga_volume_words(1) == {"ok": False, "reason": "no_ocr"}


VOLUME_WORDS = {
    "ok": True,
    "backend": "jiten",
    "tokens": 7,
    "unique_words": 4,
    "new_unique": 2,
    "known_share": 0.4286,
    "known_or_learning_share": 0.5714,
    "top_new": [
        {
            "spelling": "かぜ",
            "reading": "かぜ",
            "meaning": "wind",
            "count": 2,
            "word_id": 1,
            "reading_index": 0,
        },
        {
            "spelling": "うみ",
            "reading": "うみ",
            "meaning": "sea",
            "count": 1,
            "word_id": 3,
            "reading_index": 0,
        },
    ],
}


def test_volume_words_window_shows_coverage_and_adds_new_words(page) -> None:
    result = reader(
        page,
        r"""
        const open = async () => { await __click('#mangaV2Settings [data-manga-v2-action="volume-words"]'); await __tick(50); return modal(); };
        window.__words = {ok: false, reason: 'no_ocr'};
        const out = {noOcr: await open(), noOcrText: t('manga.thisVolumeHasNoOcr')};
        window.__words = WORDS;
        out.words = await open();
        out.rows = [...document.querySelectorAll('#modalBody [data-volume-word]')].map(box => [box.closest('label').querySelector('.title').textContent, box.checked]);
        out.summary = t('manga.youKnowOfTheRunning', {value: '43%', value2: '57%', unique_words: 4, new_unique: 2});
        await __input('#modalBody [data-volume-word="1"]', false);            // keep only かぜ
        await __click('#mangaVolumeWordsAdd'); await __tick(50);
        out.progress = __text('#mangaVolumeWordsProgress'); out.added = t('manga.wordsAdded', {added: 1});
        return out;
        """.replace("WORDS", json.dumps(VOLUME_WORDS)),
        regions={0: []},
        before="__api.manga_volume_words = async () => window.__words;"
        "__api.study_decks = async () => [{id: '12', name: 'Mining'}];"
        "__api.study_deck_preference = async () => ({deck_id: '12'});"
        "__api.study_action = async () => ({ok: true});",
    )
    out = result.value
    assert out["noOcr"]["open"] is True and out["noOcr"]["text"] == out["noOcrText"]
    assert result.calls_of("manga_volume_words") == [[3, 40], [3, 40]]
    assert out["summary"] in out["words"]["text"]
    assert out["rows"] == [["かぜ", True], ["うみ", True]]
    assert result.calls_of("study_action") == [
        [
            {
                "backend": "jiten",
                "action": "add",
                "word_id": 1,
                "reading_index": 0,
                "deck_id": "12",
                "id_namespace": "jiten",
            }
        ]
    ]
    assert result.calls_of("set_study_deck_preference") == [["jiten", "12"]]
    assert out["progress"] == out["added"]


# ---------------------------------------------------------------- manga card adds


def test_adding_a_manga_word_attaches_the_page_and_sentence(tmp_path: Path) -> None:
    from pudge.light_novels import LightNovelService

    service = LightNovelService.__new__(LightNovelService)
    calls: list[tuple] = []
    service._jiten_mutation = lambda path, body: calls.append(("add", path, body))
    service._jiten_add_custom_sentence = lambda w, r, sentence: calls.append(("sentence", sentence))
    service._jiten_upload_card_image = lambda w, r, data, filename: calls.append(("image", filename, data))
    service.invalidate_jiten_live_state = lambda w, r: None
    long_sentence = "あ" * 150 + "猫" + "あ" * 149
    result = service.study_action(
        "jiten",
        "add",
        5,
        0,
        deck_id="12",
        sentence=long_sentence,
        surface="猫",
        media_image=b"jpeg",
        media_filename="pudge-manga-1-page-3.jpg",
    )
    assert result["ok"] is True
    assert calls[0][0] == "add" and calls[0][2]["sentence"] == "あ" * 150
    assert ("sentence", "あ" * 72 + "**猫**" + "あ" * 73) in calls and (
        "image",
        "pudge-manga-1-page-3.jpg",
        b"jpeg",
    ) in calls
    calls.clear()
    service.study_action("jiten", "add", 5, 0, deck_id="12", sentence="短い")
    assert [call[0] for call in calls] == ["add"]  # outside the manga reader nothing extra is sent

    api = WebAppApi.__new__(WebAppApi)
    prepared: list[dict] = []
    api._study_manga_card_image = lambda context: prepared.append(context) or (b"jpeg", "page.jpg")
    forwarded: dict = {}
    api.light_novels = SimpleNamespace(
        settings=lambda: SimpleNamespace(study_backend="jiten"),
        study_action=lambda *args, **kwargs: forwarded.update(kwargs) or {"ok": True},
    )
    api.study_action(
        {
            "backend": "jiten",
            "action": "add",
            "word_id": 5,
            "reading_index": 0,
            "deck_id": "12",
            "media_context": {"kind": "manga", "book_id": 1, "page_index": 2},
        }
    )
    assert prepared and forwarded["media_image"] == b"jpeg"
    # The word card in the manga reader sends that media context with Add:
    # tests/test_manga_reading_tools_v3_local.py (study_action media_context {"kind": "manga", ...}).
