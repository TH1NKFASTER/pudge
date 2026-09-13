from __future__ import annotations

from pathlib import Path


def test_home_download_status_is_compact_and_hides_eta_when_torrents_are_off() -> None:
    root = Path(__file__).resolve().parents[1]
    js = (root / "pudge/web/home_status.js").read_text(encoding="utf-8")
    html = (root / "pudge/web/index.html").read_text(encoding="utf-8")
    start = js.index("function compactDownloadStatus(download)")
    end = js.index("function episodePresentationStatus", start)
    block = js[start:end]

    assert "Downloading ${progress}%" not in block
    assert "Загрузка ${progress}%" not in block
    assert "const parts=[`${progress}%`]" in block
    assert "ui.torrentToggleDesired===false?false" in block
    assert "ui.state?.settings?.torrents_enabled" in block
    assert "ui.torrentTraffic?.enabled!==false" in block
    assert "torrentTrafficActive&&Number.isFinite(eta)&&eta>0" in block

    handler = html.split("async function torrentToggleHttpCapture(){", 1)[1].split(
        "document.addEventListener('click',event=>{", 1
    )[0]
    assert "if(desired===false)renderSafely('current',renderCurrent)" in handler
    assert "renderDownloads();renderSafely('current',renderCurrent);" in handler
