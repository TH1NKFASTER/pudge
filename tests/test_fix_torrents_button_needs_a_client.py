"""The Torrents button at the bottom left shows only when a torrent client (aria2 or
qBittorrent) is chosen in Settings: with neither there is nothing to switch on or off."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    ("aria2", "qbt", "shown"),
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
def test_torrents_button_shows_only_with_a_torrent_client(page, aria2: bool, qbt: bool, shown: bool) -> None:
    result = page.run(
        "renderAll(); return __visible('#torrentToggleButton');",
        state={"settings": {"aria2_enabled": aria2, "qbt_enabled": qbt}},
    )
    assert result.value is shown


def test_choosing_a_client_in_settings_brings_the_button_back(page) -> None:
    result = page.run(
        r"""
        renderAll();
        const before = __visible('#torrentToggleButton');
        ui.state.settings.aria2_enabled = true;
        renderAll();
        return {before, after: __visible('#torrentToggleButton')};
        """,
        state={"settings": {"aria2_enabled": False, "qbt_enabled": False}},
    )
    assert result.value == {"before": False, "after": True}
