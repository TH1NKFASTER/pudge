"""Scrolling: the polychrome cover effect still answers the pointer while the anime list
scrolls; light-novel words are not hit-tested while the reader actively scrolls
(behaviour, page harness; audit 71Б rewrite).

Library shelves without a dense/masonry layout (light novels and manga):
tests/test_v148_franchise_jiten_benchmark.py."""

from __future__ import annotations

from css_support import css_rules

READY = {
    "home": {
        "new_ready": [
            {
                "media_id": 5,
                "title": "Show",
                "cover": "c.jpg",
                "episode": 1,
                "ready_episodes": [1],
                "local": {
                    "video_path": "/v/1.mkv",
                    "episode": 1,
                    "state": "ready",
                    "subtitle_source": "external",
                },
            }
        ]
    }
}


def test_polychrome_cover_reacts_to_the_pointer_while_the_anime_list_scrolls(page) -> None:
    result = page.run(
        r"""
        await __tick(2200);                                   // the wake-up shimmer after start-up is over
        const cover = document.querySelector('#current .cover-shell.polychrome');
        const shimmer = () => ['polychrome-wake', 'polychrome-hover-wake'].filter(name => cover.classList.contains(name));
        const out = {idle: shimmer()};
        document.getElementById('scroll').dispatchEvent(new Event('scroll'));
        out.scrolling = document.documentElement.classList.contains('ui-anime-scrolling');
        await __fire('#current .cover-shell.polychrome img', 'pointerover', {}, 'PointerEvent');
        out.hover = shimmer();
        await __tick(200);
        out.scrolled = document.documentElement.classList.contains('ui-anime-scrolling');
        return out;
        """,
        state=READY,
    )
    assert result.value == {
        "idle": [],
        "scrolling": True,
        "hover": ["polychrome-hover-wake"],
        "scrolled": False,
    }
    # No stylesheet switches the effect off during scrolling either.
    assert not [
        selector
        for _media, selector, _decls in css_rules("index.html").rules
        if "ui-anime-scrolling" in selector and "polychrome" in selector
    ]


def test_light_novel_words_ignore_the_pointer_only_while_the_reader_scrolls(page) -> None:
    result = page.run(
        r"""
        const scroller = document.getElementById('lnReaderScroll');
        const scrolling = () => scroller.classList.contains('ln-reader-scrolling');
        const scroll = () => scroller.dispatchEvent(new Event('scroll'));
        const out = {before: scrolling()};
        scroll(); out.started = scrolling();
        await __tick(100); scroll();
        await __tick(100); out.stillScrolling = scrolling();      // 100 ms after the last scroll event
        await __tick(100); out.settled = scrolling();             // 200 ms after it
        return out;
        """
    )
    assert result.value == {"before": False, "started": True, "stillScrolling": True, "settled": False}
    words = css_rules("index.html").declarations(".ln-reader-scroll.ln-reader-scrolling .ln-word")
    assert (words["pointer-events"], words["cursor"]) == ("none", "default")
    assert css_rules("index.html").get(".ln-word", "cursor") == "pointer"
