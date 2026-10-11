"""Card covers fall back like the cover preview does.

User report: after reinstalling, some anime / light-novel cards stayed on the grey
"Cover" placeholder while the expanded cover (the preview) of the same item showed.
The card loads the list-size copy (``<cover>.list.jpg``) or the stored LN cover, the
preview the full cached cover; a card whose file failed had no other source.
"""

from __future__ import annotations

import os
from pathlib import Path

from pudge.config import AppConfig, write_config
from pudge.light_novels import LightNovelService
from pudge.manager_models import LibraryAnime
from pudge.web_app import WebAppApi

REMOTE = "https://s4.anilist.co/file/anilistcdn/media/anime/cover/large/bx9-x.jpg"
LOCAL_LIST = "http://127.0.0.1:1234/covers/9.jpg.list.jpg"
LOCAL_FULL = "http://127.0.0.1:1234/covers/9.jpg"

# Runs the img's inline handlers as WebKit does.
FIRE = r"""
window.__coverEvent = (img, type) => new Function('event', img.getAttribute('on' + type)).call(img, new Event(type));
"""


def _anime_card(**extra):
    return {
        "media_id": 9,
        "title": "Tensei Shitara Ken Deshita 2nd Season",
        "site_url": "https://anilist.co/anime/9",
        **extra,
    }


def test_anime_card_falls_back_to_full_cover_then_anilist(page) -> None:
    result = page.run(
        FIRE
        + r"""
        await __tick(100);
        const img = () => document.querySelector('.caught-up-card .cover-shell img[data-pudge-cover-image]');
        const look = () => ({src: img().getAttribute('src'), placeholder: __visible('.caught-up-card [data-pudge-cover-placeholder]'),
                             ready: img().dataset.pudgeCoverReady ?? null});
        const out = {start: look()};
        __coverEvent(img(), 'error');            // list-size copy is broken
        out.full = look();
        __coverEvent(img(), 'error');            // full cached cover is broken too
        out.remote = look();
        img().__naturalWidth = 230; img().__naturalHeight = 345;
        __coverEvent(img(), 'load');
        out.loaded = look();
        // A home refresh with the same data keeps the working source.
        renderCurrent();
        out.rerender = look();
        return out;
        """,
        state={"home": {"caught_up": [_anime_card(cover=LOCAL_LIST, cover_remote=REMOTE)]}},
    )
    out = result.value
    assert out["start"] == {"src": LOCAL_LIST, "placeholder": True, "ready": None}
    assert out["full"] == {"src": LOCAL_FULL, "placeholder": True, "ready": None}
    assert out["remote"] == {"src": REMOTE, "placeholder": True, "ready": None}
    assert out["loaded"] == {"src": REMOTE, "placeholder": False, "ready": "1"}
    assert out["rerender"] == out["loaded"]


def test_anime_card_without_cached_cover_uses_anilist_and_ends_on_placeholder(page) -> None:
    result = page.run(
        FIRE
        + r"""
        await __tick(100);
        const img = document.querySelector('.caught-up-card img[data-pudge-cover-image]');
        const src = img.getAttribute('src');
        __coverEvent(img, 'error');
        return {src, after: img.getAttribute('src'), placeholder: __visible('.caught-up-card [data-pudge-cover-placeholder]')};
        """,
        state={"home": {"caught_up": [_anime_card(cover="", cover_remote=REMOTE)]}},
    )
    # No cached file (e.g. a fresh cache after reinstall): the card shows AniList's
    # cover; when nothing is left to try it keeps the placeholder (no endless retry).
    assert result.value == {"src": REMOTE, "after": REMOTE, "placeholder": True}


LN_PAGE = (
    FIRE
    + r"""
setPage('lightnovels', true);
await __tick(50);
const out = {};
for (const id of [4, 6]) {
  const steps = [];
  for (let i = 0; i < 4; i++) {
    const node = [...document.querySelectorAll(`#lightNovelsContent [data-ln-book="${id}"] .ln-card-cover`)][0];
    if (!node) break;
    if (node.tagName !== 'IMG') { steps.push({placeholder: node.textContent, link: node.getAttribute('data-ln-anilist-url')}); break; }
    steps.push(node.getAttribute('src'));
    __coverEvent(node, 'error');
  }
  out[id] = steps;
}
return out;
"""
)


def test_light_novel_card_falls_back_to_full_cover_then_anilist_then_placeholder(page) -> None:
    book = {
        "id": 4,
        "title": "Novel",
        "volume": 1,
        "anilist_id": 55,
        "cover_url": "covers/ln-abc.jpg.list.jpg",
        "remote_cover_url": "https://img/55.jpg",
        "character_count": 1000,
    }
    stored_only = {
        **book,
        "id": 6,
        "title": "Other",
        "anilist_id": None,
        "cover_url": "covers/ln-def.jpg.list.jpg",
        "remote_cover_url": "",
    }
    result = page.run(
        LN_PAGE,
        api={"light_novel_state": {"version": 1, "settings": {}, "books": [book, stored_only]}},
    )
    assert result.value["4"] == [
        "covers/ln-abc.jpg.list.jpg",
        "covers/ln-abc.jpg",
        "https://img/55.jpg",
        {"placeholder": "Cover", "link": "https://anilist.co/manga/55"},
    ]
    assert result.value["6"] == [
        "covers/ln-def.jpg.list.jpg",
        "covers/ln-def.jpg",
        {"placeholder": "Cover", "link": None},
    ]


def test_light_novel_franchise_tile_cover_falls_back(page) -> None:
    volumes = [
        {
            "id": book_id,
            "title": f"Tower {series}",
            "volume": 1,
            "series_key": f"tower-{series}",
            "series_title": f"Tower {series}",
            "franchise_key": "tower",
            "franchise_title": "Tower",
            "franchise_order": order,
            "anilist_id": 77 + order,
            "cover_url": f"covers/ln-{series}.jpg.list.jpg",
            "remote_cover_url": f"https://img/{77 + order}.jpg",
        }
        for order, (book_id, series) in enumerate([(11, "a"), (12, "b")])
    ]
    result = page.run(
        FIRE
        + r"""
        setPage('lightnovels', true);
        await __tick(50);
        const node = () => document.querySelector('#lightNovelsContent [data-ln-group-open="franchise"] .ln-card-cover');
        const steps = [];
        for (let i = 0; i < 4 && node(); i++) {
          if (node().tagName !== 'IMG') { steps.push('placeholder:' + node().textContent); break; }
          steps.push(node().getAttribute('src'));
          __coverEvent(node(), 'error');
        }
        return steps;
        """,
        api={"light_novel_state": {"version": 1, "settings": {}, "books": volumes}},
    )
    steps = result.value
    assert len(steps) == 4, steps
    first = steps[0]
    remote = "https://img/77.jpg" if "ln-a" in first else "https://img/78.jpg"
    assert steps[1:] == [first.removesuffix(".list.jpg"), remote, "placeholder:Cover"]


def test_home_payload_carries_anilist_cover_next_to_cached_one(tmp_path: Path) -> None:
    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=9,
            title="Ken Deshita",
            status="CURRENT",
            episodes=12,
            progress=3,
            media_status="RELEASING",
            cover_url=REMOTE,
        )
    )
    state = api.get_state()
    cards = [card for cards in state["home"].values() for card in cards if card.get("media_id") == 9]
    assert cards, state["home"]
    # Nothing cached yet: no local cover, but the card still has AniList's.
    assert all(card["cover"] == "" and card["cover_remote"] == REMOTE for card in cards)


def test_light_novel_state_carries_anilist_cover_for_card_fallback() -> None:
    service = LightNovelService.__new__(LightNovelService)
    service._anilist_cache = (
        0.0,
        [
            {
                "media_id": 55,
                "title": "Novel",
                "format": "NOVEL",
                "cover": "https://img/55.jpg",
                "relations": [],
            }
        ],
    )
    service._state_refreshing = False
    service._state_version = 1
    service.books = lambda: [
        {"id": 4, "anilist_id": 55, "title": "Novel", "cover_url": "covers/ln-a.jpg.list.jpg"}
    ]
    service.settings_payload = lambda: {}
    service._ensure_literature_account = lambda: None
    book = service._state_payload_fast()["books"][0]
    assert book["cover_url"] == "covers/ln-a.jpg.list.jpg"
    assert book["remote_cover_url"] == "https://img/55.jpg"


def test_cover_refresh_never_leaves_a_truncated_cover(tmp_path: Path, monkeypatch) -> None:
    """A refresh that dies mid-write keeps the previous cover (the window may be reading it)."""
    from pudge import manager as manager_module

    cfg = AppConfig()
    cfg.config_path = tmp_path / "config.toml"
    cfg.library.database_path = tmp_path / "library.sqlite3"
    cfg.library.cover_cache_dir = tmp_path / "cache" / "covers"
    cfg.paths.cache_dir = tmp_path / "cache"
    cfg.library.root_dir = tmp_path / "library"
    write_config(cfg, cfg.config_path)
    api = WebAppApi(cfg.config_path)
    anime = LibraryAnime(media_id=9, title="Ken Deshita", status="CURRENT", cover_url=REMOTE)
    cached = cfg.library.cover_cache_dir / "9.jpg"
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"old-cover-bytes")
    os.utime(cached, (0, 0))  # due for a refresh

    class Response:
        content = b"new-cover-bytes-that-are-longer"

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(manager_module.httpx, "get", lambda *a, **k: Response())
    real_write = Path.write_bytes

    def dying_write(self: Path, data: bytes) -> int:
        real_write(self, data[:5])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", dying_write)
    assert api.manager.cover_path(anime) == cached
    monkeypatch.setattr(Path, "write_bytes", real_write)
    assert cached.read_bytes() == b"old-cover-bytes"
