from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pudge.library_groups import literature_franchise_metadata
from pudge.light_novels import LightNovelService

ROOT = Path(__file__).resolve().parents[1]
SHELVES = ROOT / "pudge" / "web" / "library_shelves.js"


def _node(payload: dict) -> dict:
    source = json.dumps(SHELVES.read_text(encoding="utf-8"))
    data = json.dumps(payload)
    script = f"""
const source = {source};
const seed = {data};
const storage = new Map();
global.window = global;
global.localStorage = {{
  getItem: key => storage.has(key) ? storage.get(key) : null,
  setItem: (key, value) => storage.set(key, String(value)),
}};
eval(source);
const shelves = PudgeLibraryShelves.build(seed.books, {{
  seriesKey: b => b.series_key,
  seriesTitle: b => b.series_title,
  bookOrder: (a,b) => a.volume-b.volume || a.id-b.id,
}});
const before = shelves.map(s => ({{
  key:s.key, series:s.series.map(g=>g.key), books:s.series.map(g=>g.books.map(b=>b.id)),
  visible:s.visibleSeries.map(g=>g.key), hidden:s.hiddenSeriesCount, expanded:s.expanded,
}}));
PudgeLibraryShelves.toggle(seed.toggleKey || '');
const after = PudgeLibraryShelves.build(seed.books, {{
  seriesKey: b => b.series_key,
  seriesTitle: b => b.series_title,
  bookOrder: (a,b) => a.volume-b.volume || a.id-b.id,
}}).map(s => ({{key:s.key, visible:s.visibleSeries.map(g=>g.key), hidden:s.hiddenSeriesCount, expanded:s.expanded}}));
console.log(JSON.stringify({{before,after}}));
"""
    completed = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(completed.stdout)


def test_franchise_chain_uses_relation_order_not_title_order() -> None:
    rows = [
        {"media_id": 30, "title": "AAA sequel", "year": 2022, "format": "NOVEL", "relations": [{"media_id": 20, "relation_type": "PREQUEL", "format": "NOVEL"}]},
        {"media_id": 10, "title": "ZZZ prequel", "year": 2020, "format": "NOVEL", "relations": [{"media_id": 20, "relation_type": "SEQUEL", "format": "NOVEL"}]},
        {"media_id": 20, "title": "Middle", "year": 2021, "format": "NOVEL", "relations": [{"media_id": 30, "relation_type": "SEQUEL", "format": "NOVEL"}]},
    ]
    metadata = literature_franchise_metadata(rows)
    assert [media_id for media_id, _ in sorted(metadata.items(), key=lambda item: item[1]["franchise_order"])] == [10, 20, 30]
    assert len({item["franchise_key"] for item in metadata.values()}) == 1
    assert metadata[10]["relation_kind"] == "root"
    assert all(not item["franchise_ambiguous"] for item in metadata.values())


def test_franchise_cycle_keeps_every_series_in_stable_order() -> None:
    rows = [
        {"media_id": 7, "title": "Same", "year": 2020, "format": "NOVEL", "relations": [{"media_id": 9, "relation_type": "SEQUEL", "format": "NOVEL"}]},
        {"media_id": 9, "title": "Same", "year": 2021, "format": "NOVEL", "relations": [{"media_id": 7, "relation_type": "SEQUEL", "format": "NOVEL"}]},
    ]
    metadata = literature_franchise_metadata(rows)
    assert set(metadata) == {7, 9}
    assert [media_id for media_id, _ in sorted(metadata.items(), key=lambda item: item[1]["franchise_order"])] == [7, 9]
    assert all(item["franchise_ambiguous"] for item in metadata.values())


def test_unrelated_same_title_and_non_novel_relations_do_not_merge() -> None:
    rows = [
        {"media_id": 1, "title": "Same", "format": "NOVEL", "relations": [{"media_id": 2, "relation_type": "SEQUEL", "format": "MANGA"}]},
        {"media_id": 2, "title": "Same", "format": "NOVEL", "relations": []},
    ]
    assert literature_franchise_metadata(rows) == {}


def test_light_novel_state_exposes_stable_shelf_metadata_without_db_mutation() -> None:
    service = LightNovelService.__new__(LightNovelService)
    service._anilist_cache = (
        0.0,
        [
            {"media_id": 101, "title": "First", "format": "NOVEL", "year": 2020, "relations": [{"media_id": 102, "relation_type": "SEQUEL", "format": "NOVEL"}]},
            {"media_id": 102, "title": "Second", "format": "NOVEL", "year": 2021, "relations": [{"media_id": 101, "relation_type": "PREQUEL", "format": "NOVEL"}]},
        ],
    )
    service._state_refreshing = False
    service._state_version = 3
    service.books = lambda: [
        {"id": 1, "anilist_id": 101, "title": "First v1", "volume": 1},
        {"id": 2, "anilist_id": 102, "title": "Second v1", "volume": 1},
    ]
    service.settings_payload = lambda: {}
    state = service._state_payload_fast()
    assert [book["franchise_order"] for book in state["books"]] == [0, 1]
    assert state["books"][0]["franchise_key"] == state["books"][1]["franchise_key"]
    assert state["books"][0]["franchise_series_count"] == 2


def test_shelf_model_collapses_large_franchise_and_persists_expansion() -> None:
    books = [
        {"id": index + 1, "series_key": f"series-{index}", "series_title": f"Series {index}", "franchise_key": "franchise:1", "franchise_order": index, "volume": 1}
        for index in range(5)
    ]
    result = _node({"books": books, "toggleKey": "franchise:1"})
    assert result["before"][0]["visible"] == ["series-0", "series-1", "series-2"]
    assert result["before"][0]["hidden"] == 2
    assert result["after"][0]["visible"] == [f"series-{index}" for index in range(5)]
    assert result["after"][0]["expanded"] is True


def test_shelf_model_preserves_duplicate_titles_distinct_ids_and_many_volumes() -> None:
    books = [
        {"id": 100 + volume, "series_key": "edition-a", "series_title": "Same title", "volume": volume}
        for volume in range(1, 22)
    ] + [
        {"id": 500, "series_key": "edition-b", "series_title": "Same title", "volume": 1},
    ]
    result = _node({"books": books, "toggleKey": "unused"})
    shelves = result["before"]
    assert len(shelves) == 2
    by_series = {shelf["series"][0]: shelf for shelf in shelves}
    assert len(by_series["edition-a"]["books"][0]) == 21
    assert by_series["edition-b"]["books"][0] == [500]
