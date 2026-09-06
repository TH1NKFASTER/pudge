from __future__ import annotations

import json
import zipfile
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


def _insert_volume(api: WebAppApi, tmp_path: Path, *, title: str, volume: int, text: str) -> int:
    source_file = tmp_path / f"volume-{volume}.txt"
    source_file.write_text(text, encoding="utf-8")
    with api.light_novels._connect() as conn:
        cur = conn.execute(
            "INSERT INTO ln_books(title,file_path,file_type,volume,anilist_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (title, str(source_file), "txt", volume, 55555, 1.0, 1.0),
        )
        book_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO ln_chapters(book_id,chapter_index,title,text,text_hash) VALUES(?,?,?,?,?)",
            (book_id, 0, "Chapter 1", text, f"hash-{volume}"),
        )
    return book_id


def test_export_is_one_series_archive_with_all_local_volumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    first = _insert_volume(api, tmp_path, title="Series Vol. 1", volume=1, text="地の文。「一巻の台詞」続き。")
    second = _insert_volume(api, tmp_path, title="Series Vol. 2", volume=2, text="地の文。「二巻の台詞」続き。")

    result = api.export_light_novel_speaker_markup(first)
    archive = Path(result["path"])
    assert archive.is_file()
    assert result["book_count"] == 2
    assert result["chapter_count"] == 2

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "INSTRUCTIONS.md" in names
        assert "manifest.json" in names
        assert "profiles.json" in names
        assert "known_characters.json" in names
        assert "OUTPUT_TEMPLATE.json" in names
        manifest = json.loads(zf.read("manifest.json"))
        template = json.loads(zf.read("OUTPUT_TEMPLATE.json"))
        assert manifest["kind"] == "pudge-speaker-markup-source"
        assert template["kind"] == "pudge-speaker-annotations"
        assert {row["book_id"] for row in manifest["books"]} == {first, second}
        chapter_names = [name for name in names if name.startswith("books/") and name.endswith(".json")]
        assert len(chapter_names) == 2
        chapter = json.loads(zf.read(chapter_names[0]))
        assert any(row["kind"] == "dialogue" for row in chapter["segments"])
        assert "text" in chapter["segments"][0]
        assert "NEVER return or rewrite" in zf.read("INSTRUCTIONS.md").decode("utf-8")


def test_imported_markup_works_without_llm_and_reuses_series_voice_across_volumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = _api(tmp_path, monkeypatch)
    first_text = "彼女は言った。「一巻よ」そして去った。"
    second_text = "彼女は戻った。「二巻でも同じ声よ」静かに笑った。"
    first = _insert_volume(api, tmp_path, title="Series Vol. 1", volume=1, text=first_text)
    second = _insert_volume(api, tmp_path, title="Series Vol. 2", volume=2, text=second_text)
    api.config.llm.enabled = False
    api.config.llm.model = ""

    series_key = api.light_novels.book(first)["series_key"]
    books = []
    for book_id, text in [(first, first_text), (second, second_text)]:
        units = api._audiobook_voice_units(text)
        dialogue_id = next(int(row["id"]) for row in units if row["dialogue"])
        books.append(
            {
                "book_id": book_id,
                "chapters": [
                    {
                        "chapter_index": 0,
                        "source_sha256": api._speaker_markup_chapter_digest(text),
                        "assignments": [
                            {
                                "id": dialogue_id,
                                "speaker": "綾乃",
                                # Deliberately disagree in vol.2; top-level series profile must win.
                                "caption": "二巻だけの違う声" if book_id == second else "一巻だけの違う声",
                            }
                        ],
                    }
                ],
            }
        )
    annotations = {
        "schema": 1,
        "kind": "pudge-speaker-annotations",
        "series_key": series_key,
        "series_title": "Series",
        "profiles": {"綾乃": "若い女性。落ち着いた低めの声。柔らかく明瞭な話し方。"},
        "books": books,
    }
    output = tmp_path / "pudge-speaker-annotations.json"
    output.write_text(json.dumps(annotations, ensure_ascii=False), encoding="utf-8")

    result = api.import_light_novel_speaker_markup(first, str(output))
    assert result["chapters"] == 2
    assert result["assignments"] == 2
    assert api.light_novels.settings().audiobook_character_voices is True

    expected = "若い女性。落ち着いた低めの声。柔らかく明瞭な話し方。"
    for book_id, text in [(first, first_text), (second, second_text)]:
        parts = api._audiobook_speaker_parts(
            book_id=book_id,
            book_title="Series",
            chapter_index=0,
            chapter_text=text,
            base_caption="narrator",
        )
        character_parts = [row for row in parts if row[3] == "綾乃"]
        assert character_parts
        assert all(row[2] == expected for row in character_parts)

    first_context = api._audiobook_speaker_cache_context(first)
    second_context = api._audiobook_speaker_cache_context(second)
    assert first_context["profiles_path"] == second_context["profiles_path"]
    profiles = api._read_audiobook_speaker_profiles(Path(first_context["profiles_path"]))
    assert profiles["綾乃"] == expected


def test_import_rejects_changed_source_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    api = _api(tmp_path, monkeypatch)
    book_id = _insert_volume(api, tmp_path, title="Series Vol. 1", volume=1, text="地の文。「台詞」")
    series_key = api.light_novels.book(book_id)["series_key"]
    payload = {
        "schema": 1,
        "kind": "pudge-speaker-annotations",
        "series_key": series_key,
        "profiles": {},
        "books": [
            {
                "book_id": book_id,
                "chapters": [
                    {"chapter_index": 0, "source_sha256": "wrong", "assignments": []}
                ],
            }
        ],
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(Exception, match="text changed"):
        api.import_light_novel_speaker_markup(book_id, str(path))


def test_v95_archive_backend_and_manual_character_voices_remain_available_without_llm_gate() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(encoding="utf-8")
    assert hasattr(WebAppApi, "export_light_novel_speaker_markup")
    assert hasattr(WebAppApi, "import_light_novel_speaker_markup")
    assert "Different character voices" in html
    availability = html.split("function syncConditionalSettings", 1)[1].split("function collectSettings", 1)[0]
    assert "s_ln_character_voices'),audiobookProvider==='irodori'" in availability
    assert "s_ln_character_voices'),audiobookProvider==='irodori'&&enabled('s_llm_enabled')" not in availability
