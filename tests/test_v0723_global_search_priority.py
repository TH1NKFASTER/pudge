from __future__ import annotations

from pathlib import Path


def test_global_search_priority_contract() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "pudge" / "web_app.py"
    ).read_text(encoding="utf-8")

    assert 'int(item.get("priority_tier", 3))' in source
    assert '"priority_tier": 0' in source
    assert 'str(anime.status or "").upper() != "PLANNING"' in source

    rows = [
        {"title": "Exact Planning", "priority_tier": 2, "score": 100},
        {"title": "Remote AniList", "priority_tier": 3, "score": 100},
        {"title": "Ready fuzzy", "priority_tier": 0, "score": 82},
        {"title": "Current fuzzy", "priority_tier": 1, "score": 90},
    ]
    rows.sort(
        key=lambda item: (
            int(item.get("priority_tier", 3)),
            -float(item.get("score") or 0),
        )
    )

    assert [row["title"] for row in rows] == [
        "Ready fuzzy",
        "Current fuzzy",
        "Exact Planning",
        "Remote AniList",
    ]
