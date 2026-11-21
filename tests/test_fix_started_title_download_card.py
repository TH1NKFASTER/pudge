"""Home's "Available to download" card of a title being watched does not look never started.

Maintainer (Ansatsu Kyoushitsu, AniList 20755, CURRENT, progress 7 of 22): episodes 1-7 were
watched and cleaned up, 8 and 9 were searched every pass ("no suitable release found
automatically yet"), so nothing of the title was local. Home put it under "Available to
download" with "Download anime", as for a title never started. The section stays (the next
episode is not on disk and nothing downloads it: the person has to act), the card names the
next episode and offers the next episodes.
"""

from __future__ import annotations

from pathlib import Path

from pudge.manager_models import LibraryAnime
from test_web_app import make_api


def _card(page, data: dict, lang: str) -> dict:
    return page.run(
        f"""
        const box = document.createElement('div');
        box.innerHTML = downloadAvailableHomeCard({data!r});
        return {{
            state: box.querySelector('.airing-state')?.innerText || '',
            button: box.querySelector('.download-card-button')?.innerText || '',
            batch: box.querySelector('.download-card-button')?.dataset.batch || '',
        }};
        """,
        lang=lang,
    ).value


def test_started_title_names_its_next_episode(page) -> None:
    started = {"media_id": 20755, "title": "Ansatsu Kyoushitsu", "progress": 7, "next_episode": 8}
    assert _card(page, started, "en") == {
        "state": "Next: episode 8",
        "button": "Download next episodes",
        "batch": "1",
    }
    assert _card(page, started, "ru") == {
        "state": "Дальше — серия 8",
        "button": "Скачать следующие серии",
        "batch": "1",
    }
    fresh = {"media_id": 42, "title": "Never started", "progress": 0, "next_episode": 1}
    assert _card(page, fresh, "en") == {"state": "", "button": "Download anime", "batch": "1"}


def test_watched_and_cleaned_title_is_offered_with_its_progress(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=20755,
            title="Ansatsu Kyoushitsu",
            status="CURRENT",
            progress=7,
            episodes=22,
            media_status="FINISHED",
            format="TV",
        )
    )
    home = api.get_state()["home"]
    card = next(item for item in home["download_available"] if item["media_id"] == 20755)
    assert card["progress"] == 7 and card["next_episode"] == 8
