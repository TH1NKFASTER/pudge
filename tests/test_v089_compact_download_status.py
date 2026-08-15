from pathlib import Path


def _compact_download_status_source() -> str:
    source = Path("pudge/web/index.html").read_text(encoding="utf-8")
    start = source.index("function compactDownloadStatus(download){")
    end = source.index("\nfunction animeCard(", start)
    return source[start:end]


def test_compact_download_status_shows_only_progress_and_eta() -> None:
    source = _compact_download_status_source()
    assert "Downloading ${progress}%" in source
    assert "ETA ${torrentEta(eta)}" in source
    assert "torrentRate(" not in source
    assert "download_speed" not in source
    assert "eta>0" in source


def test_download_center_keeps_detailed_speed_stats() -> None:
    source = Path("pudge/web/index.html").read_text(encoding="utf-8")
    assert "torrentRate(download.download_speed)" in source
