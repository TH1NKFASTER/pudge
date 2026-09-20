from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelError, LightNovelService
from pudge.web_app import WebAppApi


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "pudge" / "web"


def _service(tmp_path: Path) -> LightNovelService:
    cfg = AppConfig()
    cfg.library.root_dir = tmp_path / "library"
    cfg.library.database_path = tmp_path / "pudge.sqlite3"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    service = LightNovelService(cfg)
    service.save_settings({"jiten_api_key": "jiten-token", "study_backend": "jiten"})
    return service


def test_review_auto_adds_unassigned_word_and_enriches_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    mutations: list[tuple[str, dict]] = []
    sentences: list[tuple[int, int, str]] = []
    images: list[tuple[int, int, bytes, str]] = []

    monkeypatch.setattr(
        service,
        "jiten_live_states",
        lambda pairs, *, force=False: [{"studyDeckIds": [], "states": ["new"]}],
    )
    monkeypatch.setattr(
        service, "_jiten_mutation", lambda action, payload=None: mutations.append((action, payload or {})) or {}
    )
    monkeypatch.setattr(
        service,
        "_jiten_add_custom_sentence",
        lambda word_id, reading_index, sentence: sentences.append((word_id, reading_index, sentence)),
    )
    monkeypatch.setattr(
        service,
        "_jiten_upload_card_image",
        lambda word_id, reading_index, image, *, filename="": images.append(
            (word_id, reading_index, image, filename)
        ),
    )
    monkeypatch.setattr(
        "pudge.light_novels.JitenReviewProvider.submit_review",
        lambda *_args, **_kwargs: {"ok": True, "outcome": "confirmed", "provider": "jiten"},
    )

    sentence = "前一 前二 前三 前四 前五 対象 後一 後二 後三 後四 後五。次の文。"
    result = service.study_action(
        "jiten",
        "review",
        120,
        2,
        grade="good",
        sentence=sentence,
        deck_id=77,
        attempt_id="r17-review-1",
        id_namespace="jiten",
        media_image=b"jpeg-data",
        media_filename="page.jpg",
    )

    assert mutations[0][0] == "srs/study-decks/77/words"
    assert mutations[0][1]["wordId"] == 120
    assert mutations[0][1]["readingIndex"] == 2
    assert mutations[0][1]["sentence"] == sentence
    assert sentences == [(120, 2, sentence)]
    assert images == [(120, 2, b"jpeg-data", "page.jpg")]
    assert result["auto_added"] is True


def test_review_does_not_duplicate_word_already_in_any_jiten_deck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    mutations: list[tuple[str, dict]] = []
    enriched: list[str] = []
    monkeypatch.setattr(
        service,
        "jiten_live_states",
        lambda pairs, *, force=False: [{"studyDeckIds": [12], "states": ["new"]}],
    )
    monkeypatch.setattr(
        service, "_jiten_mutation", lambda action, payload=None: mutations.append((action, payload or {})) or {}
    )
    monkeypatch.setattr(service, "_jiten_add_custom_sentence", lambda *_args: enriched.append("sentence"))
    monkeypatch.setattr(service, "_jiten_upload_card_image", lambda *_args, **_kwargs: enriched.append("image"))
    monkeypatch.setattr(
        "pudge.light_novels.JitenReviewProvider.submit_review",
        lambda *_args, **_kwargs: {"ok": True, "outcome": "confirmed", "provider": "jiten"},
    )

    result = service.study_action(
        "jiten", "review", 120, 2, deck_id=77, sentence="context", id_namespace="jiten"
    )

    assert mutations == []
    assert enriched == []
    assert result["auto_added"] is False



def test_deck_add_metadata_does_not_exceed_jiten_150_char_sentence_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    mutations: list[tuple[str, dict]] = []
    custom_sentences: list[str] = []
    long_context = "文" * 350
    monkeypatch.setattr(
        service,
        "jiten_live_states",
        lambda pairs, *, force=False: [{"studyDeckIds": [], "states": ["new"]}],
    )
    monkeypatch.setattr(
        service, "_jiten_mutation", lambda action, payload=None: mutations.append((action, payload or {})) or {}
    )
    monkeypatch.setattr(
        service, "_jiten_add_custom_sentence", lambda _w, _r, sentence: custom_sentences.append(sentence)
    )
    monkeypatch.setattr(
        "pudge.light_novels.JitenReviewProvider.submit_review",
        lambda *_args, **_kwargs: {"ok": True, "outcome": "confirmed", "provider": "jiten"},
    )

    service.study_action(
        "jiten", "review", 120, 2, deck_id=77, sentence=long_context, id_namespace="jiten"
    )

    assert len(mutations[0][1]["sentence"]) <= 150
    assert custom_sentences == [long_context]


def test_web_api_only_compresses_manga_page_for_word_missing_from_all_decks() -> None:
    class Settings:
        study_backend = "jiten"

    class LightNovels:
        def __init__(self, deck_ids):
            self.deck_ids = deck_ids
            self.last_kwargs = None

        def settings(self):
            return Settings()

        def jiten_live_states(self, pairs, *, force=False):
            return [{"studyDeckIds": list(self.deck_ids), "states": ["new"]}]

        def study_action(self, *args, **kwargs):
            self.last_kwargs = kwargs
            return {"ok": True}

    payload = {
        "backend": "jiten", "action": "review", "word_id": 120, "reading_index": 2,
        "deck_id": 77, "media_context": {"kind": "manga", "book_id": 5, "page_index": 3},
    }

    existing = WebAppApi.__new__(WebAppApi)
    existing.light_novels = LightNovels([12])
    existing._study_manga_card_image = lambda _ctx: pytest.fail("existing deck word must not compress manga page")
    assert existing.study_action(payload)["ok"] is True
    assert existing.light_novels.last_kwargs["media_image"] is None

    missing = WebAppApi.__new__(WebAppApi)
    missing.light_novels = LightNovels([])
    missing._study_manga_card_image = lambda _ctx: (b"jpeg", "page.jpg")
    assert missing.study_action(payload)["ok"] is True
    assert missing.light_novels.last_kwargs["media_image"] == b"jpeg"
    assert missing.light_novels.last_kwargs["media_filename"] == "page.jpg"


def test_reader_blocks_new_unassigned_review_until_a_jiten_deck_is_selected() -> None:
    source = (WEB / "reading_tools.js").read_text(encoding="utf-8")
    assert "memberships && memberships.length === 0 && !deck" in source
    assert "Choose a Jiten deck for this new word" in source


def test_card_media_upload_uses_jiten_api_key_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(tmp_path)
    captured: dict = {}

    class Response:
        status_code = 200
        content = b"{}"

        def json(self):
            return {}

    def fake_post(url, *, headers, files, timeout):
        captured.update(url=url, headers=headers, files=files, timeout=timeout)
        return Response()

    monkeypatch.setattr("pudge.light_novels.httpx.post", fake_post)
    service._jiten_upload_card_image(120, 2, b"jpeg", filename="page.jpg")

    assert captured["headers"]["Authorization"] == "ApiKey jiten-token"
    assert "Content-Type" not in captured["headers"]
    assert captured["files"]["file"][0] == "page.jpg"


def test_context_mining_keeps_at_least_five_words_on_each_side_and_nplus1_is_strict() -> None:
    tools_path = str(WEB / "reading_tools.js")
    script = f"""
    global.window=global;
    global.document={{addEventListener(){{}},querySelectorAll(){{return []}}}};
    require({json.dumps(tools_path)});
    const study=PudgeReadingTools.study;
    const words=['前1','前2','前3','前4','前5','対象','後1','後2','後3','後4','後5'];
    const text=words.join(' ')+'。';
    const tokens=[];
    let cursor=0;
    for(let i=0;i<words.length;i++){{
      const p=text.indexOf(words[i],cursor);cursor=p+words[i].length;
      tokens.push({{wordId:i+1,readingIndex:0,surface:words[i],sentence:text,contextStart:p,contextEnd:cursor,card:{{states:i===5?['new']:['mature'],frequencyRank:i===5?900:100,studyDeckIds:[]}}}});
    }}
    const mined=study.contextForToken(text,tokens[5],tokens);
    const classes=tokens.map(()=>new Set());
    const entries=tokens.map((token,i)=>({{token,node:{{classList:{{add(x){{classes[i].add(x)}},remove(x){{classes[i].delete(x)}}}}}}}}));
    const highlighted=study.applyOptimalHighlights(entries,{{enabled:true,frequencyLimit:1000}});
    process.stdout.write(JSON.stringify({{mined,highlighted,classes:classes.map(x=>[...x])}}));
    """
    result = json.loads(subprocess.check_output(["node", "-e", script], text=True))
    assert "前1" in result["mined"] and "後5" in result["mined"]
    assert result["highlighted"] == 1
    assert "pudge-optimal-word" in result["classes"][5]
    assert all("pudge-optimal-word" not in row for i, row in enumerate(result["classes"]) if i != 5)


def test_manga_reader_supplies_page_context_media_without_optimal_toggle() -> None:
    manga = (WEB / "manga_reader_v2.js").read_text(encoding="utf-8")
    assert "highlightOptimalWords: true" not in manga
    assert 'data-manga-setting="highlightOptimalWords"' not in manga
    assert "mangaPageStudyContext" in manga
    assert "contextText:" in manga and "contextOffset:" in manga
    assert "mediaContext:" in manga
    assert "kind: 'manga'" in manga
    assert "highlightOptimalWords: settings.highlightOptimalWords !== false" not in manga


def test_nplus1_highlight_is_static_gold_without_animation() -> None:
    css = (ROOT / "pudge" / "web" / "reading_tools.css").read_text()
    assert "@keyframes pudge-optimal-word-shimmer" not in css
    optimal_rule = css.split(".pudge-optimal-word{", 1)[1].split("}", 1)[0]
    assert "animation:" not in optimal_rule
    assert "background:" in optimal_rule
    assert "rgba(255,210" in optimal_rule or "rgba(255,211" in optimal_rule or "#ffd" in optimal_rule.lower()
