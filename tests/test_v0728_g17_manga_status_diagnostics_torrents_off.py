"""Torrents switched off and confirmed read "Torrents off"; the manga OCR debug export says
why words were not painted (behaviour, page harness; audit 71Б rewrite)."""

from tests.i18n_support import en, has_text, ru
from tests.t71g06_support import STUDY, column, reader

# The sidebar switch's status endpoint, with the transition the backend reports.
TRAFFIC = """
window.fetch = async url => new Response(JSON.stringify(String(url).endsWith('/api/torrents/status')
  ? {ok: true, enabled: false, desired_enabled: false, stale: false, updated_at: Date.now() / 1000, transition: window.__transition,
     download_speed: 0, upload_speed: 0, waiting: 0, active: 0, paused: 0}
  : {ok: true}), {status: 200});
__api.torrent_downloads = async () => ({downloads: [], enabled: false, observed_at: Date.now() / 1000, storage: {}, network_guard: {}});
"""


def test_confirmed_off_is_presented_as_torrents_off() -> None:
    assert (en("ui.torrentsOff"), ru("ui.torrentsOff")) == ("Torrents off", "Торренты выключены")
    assert (en("ui.shutdownNotConfirmed"), ru("ui.shutdownNotConfirmed")) == (
        "Shutdown not confirmed",
        "Остановка не подтверждена",
    )
    # The long "shutdown confirmed" wording is gone from the catalog.
    for text in ("Shutdown confirmed", "Traffic shutdown confirmed"):
        assert not has_text(text)
    for text in ("Остановка подтверждена", "Остановка трафика подтверждена"):
        assert not has_text(text, "ru")


def test_switch_and_download_center_show_off_or_an_unconfirmed_shutdown(page) -> None:
    for lang in ("en", "ru"):
        shown = {}
        for transition in ("off_confirmed", "off_requested"):
            result = page.run(
                r"""
                await pollTorrentTraffic(); await __tick(10);
                const button = $('torrentToggleButton');
                const out = {title: button.querySelector('span')?.textContent, detail: button.querySelector('small')?.textContent ?? null};
                await openDownloadCenter(); await __tick(10);
                out.callout = __text('#modalBody .setup-callout.warning');
                out.labels = {off: t('ui.torrentsOff2'), confirmed: t('ui.torrentsOff'), unconfirmed: t('ui.shutdownNotConfirmed'),
                              requested: t('ui.torrentShutdownRequestedButTraffic')};
                return out;
                """,
                lang=lang,
                before=TRAFFIC + f"window.__transition = '{transition}';",
            )
            shown[transition] = result.value
        confirmed, requested = shown["off_confirmed"], shown["off_requested"]
        labels = confirmed["labels"]
        # Confirmed: only the compact "off" state, no warning detail.
        assert (confirmed["title"], confirmed["detail"], confirmed["callout"]) == (
            labels["off"],
            None,
            labels["confirmed"],
        )
        # Not confirmed yet: still a warning.
        assert (requested["title"], requested["detail"], requested["callout"]) == (
            labels["off"],
            labels["unconfirmed"],
            labels["requested"],
        )


# Words of three bubbles; each kind of word that stays unpainted is counted in the export.
DIAGNOSTICS_PAGE = {
    0: [
        column("猫が好き", 0.80, 0.90),  # 猫 new (painted), が no state, 好き blacklisted
        column("犬と鳥", 0.70, 0.90),  # 犬 known: category switched off, 鳥 stale state
        column("花だ", 0.60, 0.90, mapped=False),  # characters not mapped to words
    ]
}
DIAGNOSTICS_WORDS = {
    "猫が好き": [["猫", 1, 0], ["が", 2, 0], ["好き", 3, 0]],
    "犬と鳥": [["犬", 4, 0], ["と", 5, 0], ["鳥", 6, 0]],
    "花だ": [["花", 7, 0], ["だ", 8, 0]],
}
DIAGNOSTICS_LIVE = """
window.__live = {'1:0': {states: ['new'], normalizedState: 'new'}, '3:0': {states: ['blacklisted'], normalizedState: 'blacklisted'},
  '4:0': {states: ['known'], normalizedState: 'known'}, '6:0': {states: ['known'], normalizedState: 'known', stale: true},
  '7:0': {states: ['new'], normalizedState: 'new'}};
"""


def test_ocr_debug_export_explains_why_words_are_not_painted(page) -> None:
    result = reader(
        page,
        "const payload = await exportDebug(); return {marks: marks().map(m => m.word), layer: payload.events.filter(e => e.event === 'status_layer').pop()};",
        regions=DIAGNOSTICS_PAGE,
        parse=DIAGNOSTICS_WORDS,
        prefs={"configured": True, "statusHighlight": True, "statusKnown": False},
        before=STUDY + DIAGNOSTICS_LIVE,
    )
    out = result.value
    assert out["marks"] == ["猫"]
    counts = {key: value for key, value in out["layer"].items() if key.endswith("_count")}
    assert counts == {
        "region_count": 3,
        "box_count": 1,
        "parsed_token_count": 8,  # every word of the page
        "mapped_token_count": 6,  # on character geometry (not 花だ)
        "colored_token_count": 1,  # 猫
        "ignored_state_count": 1,  # 好き blacklisted
        "unverified_state_count": 3,  # が, と without a state; 鳥 stale
        "unknown_state_count": 0,
        "category_disabled_count": 1,  # 犬: known words are switched off
        "geometry_unmapped_count": 2,  # 花, だ
    }
