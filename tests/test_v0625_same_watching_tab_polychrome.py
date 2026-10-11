"""The foil (polychrome) shine on Watching covers plays on real returns only (behaviour, page
harness; audit 71Б rewrite)."""

from __future__ import annotations

SHOW = {
    "media_id": 7,
    "title": "Show",
    "episode": 3,
    "video_path": "/library/Show - 03.mkv",
    "cover": "https://img.example/7.jpg",
    "resume_start": 300,
    "progress_percent": 20,
}


def test_clicking_active_watching_tab_does_not_restart_polychrome(page) -> None:
    result = page.run(
        """
        const cover = () => document.querySelector('#currentContent .cover-shell.polychrome');
        const shines = () => Number(cover().dataset.polychromeToken || 0);   // one more per started shine
        const out = {};
        await __tick(3000);                                              // the shine after start-up is over
        out.start = shines();
        await __click('.nav button[data-page="current"]');              // the tab that is already open
        await __tick(100);
        out.sameTab = shines();
        await __click('.nav button[data-page="planned"]');
        await __tick(100);
        await __click('.nav button[data-page="current"]');              // a real return to Watching
        await __tick(100);
        out.back = shines();
        out.shining = cover().classList.contains('polychrome-wake');
        await __tick(3000);
        await __setFocused(false);
        await __setFocused(true);                                        // the window comes back
        await __tick(100);
        out.refocused = shines();
        return out;
        """,
        state={"home": {"continue_watching": [SHOW]}},
    )
    out = result.value
    assert out["sameTab"] == out["start"]
    assert out["back"] == out["start"] + 1 and out["shining"] is True
    assert out["refocused"] == out["back"] + 1
