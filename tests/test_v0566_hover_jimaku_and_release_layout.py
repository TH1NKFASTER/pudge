"""Watch-order hover list, the Nyaa release list and Jimaku's .ja[cc] marker (behaviour;
audit 71Б rewrite). Window checks run the real index.html (tests/page_harness.py)."""

from __future__ import annotations

from pathlib import Path

from css_support import css_rules
from pudge.language import has_japanese_marker, is_japanese_subtitle

SEQUENCE = {
    "kind": "watch_sequence",
    "title": "Series",
    "items": [
        {"media_id": 2, "title": "Part A", "local": {"video_path": "/v/a.mkv"}, "ready_episodes": [1]},
        {"media_id": 3, "title": "Part B", "local": {"video_path": "/v/b.mkv"}, "ready_episodes": [1, 2]},
        {"media_id": 1, "title": "Part C", "local": {"video_path": "/v/c.mkv"}, "ready_episodes": []},
    ],
}


def test_watch_order_hover_list_stays_inside_card_cover(page) -> None:
    result = page.run(
        r"""
        const card = document.querySelector('#currentContent .ready-sequence-card');
        const list = card.querySelector('.ready-sequence-list');
        return {
          insideCover: Boolean(list.closest('.ready-sequence-cover')),
          rows: [...list.querySelectorAll('.ready-sequence-row')].map(row => row.innerText.replace('\n', ' ')),
          switches: [...card.querySelectorAll('.ready-sequence-cover > [data-action]')].map(button => button.dataset.action),
        };
        """,
        state={"home": {"completed_ready": [SEQUENCE]}},
    )
    # The list of entries opens over the cover (not beside the card, where it was cut off),
    # with previous/next switches on the cover.
    assert result.value == {
        "insideCover": True,
        "rows": ["1 Part A", "2 Part B", "3 Part C"],
        "switches": ["ready-sequence-previous", "ready-sequence-next"],
    }
    css = css_rules("index.html")
    assert css.get(".ready-sequence-cover", "position") == "relative"
    assert css.get(".ready-sequence-cover", "overflow") == "hidden"
    assert (css.get(".ready-sequence-list", "position"), css.get(".ready-sequence-list", "inset")) == (
        "absolute",
        "6px",
    )
    assert css.get(".ready-sequence-list", "opacity") == "0"
    # Shown while the counter (or the list itself) is hovered.
    for shown in (".ready-sequence-count-wrap:hover + .ready-sequence-list", ".ready-sequence-list:hover"):
        assert css.declarations(shown) == {"opacity": "1", "pointer-events": "auto"}


RELEASES = [
    {
        "title": "[Group] Very Long Release Title " + "x" * 120 + " [1080p]",
        "seeders": 31,
        "downloads": 900,
        "size_text": "1.4 GiB",
        "group": "Group",
    },
    {
        "title": "[Other] Show - 03 [720p]",
        "seeders": 4,
        "downloads": 12,
        "size_text": "350 MiB",
        "group": "Other",
    },
]


def test_nyaa_release_download_button_never_requires_horizontal_scroll(page) -> None:
    result = page.run(
        r"""
        await __settle(openRelease(7, 3, false));
        const rows = [...document.querySelectorAll('#modalBody .release-list > .release-row')];
        const view = {
          open: __visible('#modalBackdrop'),
          table: document.querySelectorAll('#modalBody table').length,
          rows: rows.map(row => ({title: row.querySelector('.release-main strong').innerText,
                                  button: row.querySelector(':scope > .release-download-button')?.innerText})),
          download: t('action.download'),
        };
        await __click('#modalBody .release-row:nth-child(2) .release-download-button');
        return view;
        """,
        api={"search_releases": RELEASES, "add_release": {"already_downloading": False}},
    )
    view = result.value
    assert view["open"] and view["table"] == 0
    assert view["rows"] == [{"title": release["title"], "button": view["download"]} for release in RELEASES]
    assert result.calls_of("search_releases") == [[7, 3, False]]
    assert result.calls_of("add_release") == [[7, 3, False, RELEASES[1]]]
    # The title column may shrink below its text width; the button keeps its own column.
    css = css_rules("index.html")
    assert css.get(".release-row", "display") == "grid"
    assert css.get(".release-row", "grid-template-columns") == "minmax(0,1fr) auto"
    assert css.get(".release-main", "min-width") == "0"
    assert css.get(".release-main strong", "text-overflow") == "ellipsis"


def test_jimaku_dot_ja_bracket_marker_is_recognized(tmp_path: Path) -> None:
    name = "僕のヒーローアカデミア.SP.I.am.a.hero.too.WEBRip.DMMTV.ja[cc].srt"
    subtitle = tmp_path / name
    # Deliberately too little Japanese text for content-based detection. The
    # explicit Jimaku language marker must still make this a valid JP subtitle.
    subtitle.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nあ\n",
        encoding="utf-8",
    )

    assert has_japanese_marker(name)
    assert is_japanese_subtitle(subtitle)
