from pathlib import Path

from pudge.providers.anilist import _overlay_relation_list_entries


def test_completed_collection_entry_marks_nested_relation_watched() -> None:
    relations = [
        {
            "media_id": 20,
            "episodes": 12,
            "list_status": "",
            "progress": 0,
            "watched": False,
            "relations": [
                {
                    "media_id": 10,
                    "episodes": 24,
                    "list_status": "",
                    "progress": 0,
                    "watched": False,
                }
            ],
        }
    ]

    _overlay_relation_list_entries(
        relations,
        {
            20: {"status": "COMPLETED", "progress": 12, "episodes": 12},
            10: {"status": "COMPLETED", "progress": 24, "episodes": 24},
        },
    )

    assert relations[0]["watched"] is True
    assert relations[0]["list_status"] == "COMPLETED"
    assert relations[0]["relations"][0]["watched"] is True


def test_planning_left_click_is_cover_only_and_hover_uses_large_preview() -> None:
    html = (Path(__file__).parents[1] / "pudge" / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert 'id="relationPreview"' in html
    assert "showRelationPreview" in html
    assert 'data-cover-url="${escapeHtml(coverUrl)}"' in html
    assert 'class="relation-cover-action" data-action="url"' in html
    assert 'class="cover-action" role="button"' in html
    assert 'data-context-click="1"' not in html
    assert '<div class="relation-node ' in html
    assert '<button class="relation-node ' not in html
