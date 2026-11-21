"""The downloads window as a table: sort, select many, act on the selection.

User: "add more convenient torrent management: they are huge now and cannot be selected or
sorted". Each torrent was a large card with its own buttons. Now one compact row per torrent
with columns that sort on a click, checkboxes (Shift+click for a range, Cmd/Ctrl+click on a
row), Pause / Resume / Remove / Remove + files for the selection, and a row click opens the
old card with every detail.
"""

from __future__ import annotations

GIB = 1024**3


def _row(
    hash_: str, title: str, *, added: int, size: int, progress: float, state: str = "downloading"
) -> dict:
    return {
        "hash": hash_,
        "name": f"[Grp] {title} (Batch)",
        "anime_title": title,
        "state": state,
        "progress": progress,
        "total_bytes": size,
        "downloaded_bytes": int(size * progress),
        "added_on": added,
        "backend": "aria2",
        "backend_id": "aria2",
        "batch": True,
        "media_id": 1,
    }


DOWNLOADS = [
    _row("aaa", "Baccano!", added=1_790_000_100, size=1 * GIB, progress=0.2),
    _row("bbb", "Shouwa Genroku Rakugo Shinjuu", added=1_790_000_300, size=3 * GIB, progress=0.9),
    _row("ccc", "Given", added=1_790_000_200, size=2 * GIB, progress=0.5, state="paused"),
]

SCRIPT = r"""
const order = () => [...document.querySelectorAll('#modalBody [data-torrent-row]')].map(n => n.dataset.torrentRow);
const sort = async key => { await __click(`#modalBody [data-torrent-sort="${key}"]`); await __tick(5); return order(); };
const check = async (hash, shiftKey = false) => {
  const box = document.querySelector(`#modalBody [data-torrent-select="${hash}"]`);
  box.dispatchEvent(new MouseEvent('click', {bubbles: true, shiftKey})); await __tick(5);
};
void openDownloadCenter(null); await __tick(100);
const out = {initial: order(), rows: document.querySelectorAll('#modalBody .torrent-row').length};
out.sizeDesc = await sort('size'); out.sizeAsc = await sort('size'); out.byName = await sort('name');
await sort('added');                                          // back to newest first: bbb, ccc, aaa
await check('bbb'); await check('aaa', true);                 // Shift: the whole range
out.selected = [...document.querySelectorAll('#modalBody .torrent-row.selected')].map(n => n.dataset.torrentRow);
out.bulk = __text('#modalBody .torrent-bulk strong');
await __click('#modalBody [data-torrent-bulk="pause"]'); await __tick(20);
await check('ccc');                                           // a plain click takes one out
await __click('#modalBody [data-torrent-bulk="remove-files"]'); await __tick(20);
out.question = __text('#pudgeConfirmMessage');
await __click('#pudgeConfirmBackdrop [data-pudge-confirm=ok]'); await __tick(30);
out.afterRemove = document.querySelectorAll('#modalBody .torrent-row.selected').length;
// A row click opens its card with the details and buttons; again closes it.
const card = () => document.querySelector('#modalBody [data-torrent-group="ccc"] .download-card');
out.closed = card().hidden;
await __click('#modalBody [data-torrent-row="ccc"] .torrent-name'); await __tick(5);
out.opened = card().hidden === false;
await __click('#modalBody [data-torrent-row="ccc"] .torrent-name'); await __tick(5);
out.closedAgain = card().hidden;
out.expected = {
  selected: PudgeI18n.t('uiB.downloads.selected', {count: 3}),
  question: PudgeI18n.t('uiB.downloads.removeSelectedFilesConfirm', {count: 2}),
};
return out;
"""


def test_downloads_table_sorts_selects_and_acts_on_many(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            SCRIPT,
            lang=lang,
            state={"settings": {"torrents_enabled": True}},
            api={
                "startup_maintenance": {"running": False},
                "ui_state_versions": {"ready": "1", "ui": "1"},
                "torrent_downloads": {"downloads": DOWNLOADS, "backend": "aria2"},
                "torrent_download_action": {"downloads": DOWNLOADS},
            },
        )
        out = result.value
        assert out["rows"] == 3
        assert out["initial"] == ["bbb", "ccc", "aaa"]  # newest first
        assert out["sizeDesc"] == ["bbb", "ccc", "aaa"] and out["sizeAsc"] == ["aaa", "ccc", "bbb"]
        assert out["byName"] == ["aaa", "ccc", "bbb"]  # Baccano!, Given, Shouwa…
        assert out["selected"] == ["bbb", "ccc", "aaa"] and out["bulk"] == out["expected"]["selected"]
        assert out["question"] == out["expected"]["question"]
        assert result.calls_of("torrent_download_action") == [
            ["bbb", "pause", False, "aria2"],
            ["ccc", "pause", False, "aria2"],
            ["aaa", "pause", False, "aria2"],
            ["bbb", "remove", True, "aria2"],
            ["aaa", "remove", True, "aria2"],
        ]
        assert out["afterRemove"] == 0
        assert out["closed"] is True and out["opened"] is True and out["closedAgain"] is True
