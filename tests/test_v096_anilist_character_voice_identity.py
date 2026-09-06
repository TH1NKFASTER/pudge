from __future__ import annotations

import json
from pathlib import Path

import pytest

import pudge.web_app as web_app_module
from pudge.config import AppConfig, write_config
from pudge.web_app import WebAppApi


def _api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WebAppApi:
    monkeypatch.setattr(web_app_module, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("HOME", str(tmp_path))
    config = AppConfig()
    config.config_path = tmp_path / "config.toml"
    config.library.database_path = tmp_path / "library.sqlite3"
    config.library.root_dir = tmp_path / "library"
    config.library.cover_cache_dir = tmp_path / "cache" / "covers"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True, exist_ok=True)
    config.paths.cache_dir.mkdir(parents=True, exist_ok=True)
    write_config(config, config.config_path)
    return WebAppApi(config.config_path)


def _insert_book(
    api: WebAppApi,
    tmp_path: Path,
    *,
    title: str,
    media_id: int,
    text: str,
) -> int:
    source_file = tmp_path / f"{media_id}.txt"
    source_file.write_text(text, encoding="utf-8")
    with api.light_novels._connect() as conn:
        cur = conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,volume,anilist_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (title, str(source_file), "txt", 1, media_id, 1.0, 1.0),
        )
        book_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 0, "Chapter 1", text, f"hash-{media_id}"),
        )
    return book_id


def _annotation(api: WebAppApi, book_id: int, text: str, caption: str) -> dict:
    series_key = api.light_novels.book(book_id)["series_key"]
    dialogue_id = next(
        int(row["id"]) for row in api._audiobook_voice_units(text) if row["dialogue"]
    )
    return {
        "schema": 1,
        "kind": "pudge-speaker-annotations",
        "series_key": series_key,
        "profiles": {},
        "character_profiles": {
            "999": {"name": "Ayanokouji Kiyotaka", "caption": caption}
        },
        "books": [
            {
                "book_id": book_id,
                "chapters": [
                    {
                        "chapter_index": 0,
                        "source_sha256": api._speaker_markup_chapter_digest(text),
                        "assignments": [
                            {
                                "id": dialogue_id,
                                "speaker": "Ayanokouji Kiyotaka",
                                "anilist_character_id": 999,
                                "caption": caption,
                            }
                        ],
                    }
                ],
            }
        ],
    }


def test_same_anilist_character_keeps_voice_across_different_ln_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    y1_text = "地の文。「一年目だ」続き。"
    y2_text = "地の文。「二年目でも同じ声だ」続き。"
    y1 = _insert_book(api, tmp_path, title="Classroom Year 1 Vol. 1", media_id=111, text=y1_text)
    y2 = _insert_book(api, tmp_path, title="Classroom Year 2 Vol. 1", media_id=222, text=y2_text)
    assert api.light_novels.book(y1)["series_key"] != api.light_novels.book(y2)["series_key"]

    glossary = [
        {
            "source": "綾小路清隆",
            "preferred": "Ayanokouji Kiyotaka",
            "character_id": 999,
        }
    ]
    monkeypatch.setattr(api.light_novels, "character_glossary", lambda _media_id: glossary)

    first = tmp_path / "first.json"
    first.write_text(json.dumps(_annotation(api, y1, y1_text, "最初に決めた低く静かな声"), ensure_ascii=False), encoding="utf-8")
    api.import_light_novel_speaker_markup(y1, str(first))

    second = tmp_path / "second.json"
    second.write_text(json.dumps(_annotation(api, y2, y2_text, "二年目で勝手に変えた声"), ensure_ascii=False), encoding="utf-8")
    api.import_light_novel_speaker_markup(y2, str(second))

    global_profiles = api._read_audiobook_character_profiles(api._audiobook_character_profiles_path())
    assert global_profiles[999]["caption"] == "最初に決めた低く静かな声"

    for book_id, text in ((y1, y1_text), (y2, y2_text)):
        parts = api._audiobook_speaker_parts(
            book_id=book_id,
            book_title="Classroom",
            chapter_index=0,
            chapter_text=text,
            base_caption="narrator",
            known_characters=glossary,
        )
        character = [part for part in parts if part[3] == "Ayanokouji Kiyotaka"]
        assert character
        assert all(part[2] == "最初に決めた低く静かな声" for part in character)


def test_v95_series_profile_is_lazily_promoted_to_anilist_character(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    text = "地の文。「昔からの声だ」続き。"
    book_id = _insert_book(api, tmp_path, title="Legacy Series Vol. 1", media_id=333, text=text)
    context = api._audiobook_speaker_cache_context(book_id)
    api._write_audiobook_speaker_profiles(
        Path(context["profiles_path"]),
        {"Ayanokouji Kiyotaka": "v95で既に使っていた声"},
        context["series_key"],
        context["series_title"],
    )
    units = api._audiobook_voice_units(text)
    dialogue_id = next(int(row["id"]) for row in units if row["dialogue"])
    manual = api._audiobook_manual_chapter_cache(Path(context["book_dir"]), 0, text)
    manual.write_text(
        json.dumps(
            {
                "schema": 1,
                "assignments": [
                    {"id": dialogue_id, "speaker": "Ayanokouji Kiyotaka", "caption": "новый не нужен"}
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    glossary = [
        {
            "source": "綾小路清隆",
            "preferred": "Ayanokouji Kiyotaka",
            "character_id": 999,
        }
    ]
    parts = api._audiobook_speaker_parts(
        book_id=book_id,
        book_title="Legacy",
        chapter_index=0,
        chapter_text=text,
        base_caption="narrator",
        known_characters=glossary,
    )
    assert any(part[2] == "v95で既に使っていた声" for part in parts)
    global_profiles = api._read_audiobook_character_profiles(api._audiobook_character_profiles_path())
    assert global_profiles[999]["caption"] == "v95で既に使っていた声"


def test_export_includes_anilist_character_ids_and_global_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    text = "地の文。「台詞だ」続き。"
    book_id = _insert_book(api, tmp_path, title="Export Series Vol. 1", media_id=444, text=text)
    glossary = [
        {"source": "綾小路清隆", "preferred": "Ayanokouji Kiyotaka", "character_id": 999}
    ]
    monkeypatch.setattr(api.light_novels, "character_glossary", lambda _media_id: glossary)
    api._write_audiobook_character_profiles(
        api._audiobook_character_profiles_path(),
        {999: {"name": "Ayanokouji Kiyotaka", "caption": "global voice"}},
    )
    result = api.export_light_novel_speaker_markup(book_id)
    import zipfile

    with zipfile.ZipFile(result["path"]) as zf:
        assert "character_profiles.json" in zf.namelist()
        known = json.loads(zf.read("known_characters.json"))
        assert known[0]["character_id"] == 999
        character_profiles = json.loads(zf.read("character_profiles.json"))
        assert character_profiles["characters"]["999"]["caption"] == "global voice"
        instructions = zf.read("INSTRUCTIONS.md").decode("utf-8")
        assert "anilist_character_id" in instructions
        assert "PRIMARY voice identity" in instructions
