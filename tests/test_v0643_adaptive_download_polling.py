from pathlib import Path


def test_download_polling_is_eta_adaptive_and_uses_requested_thresholds() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert "downloadPollHistory:new Map()" in html
    assert "function adaptiveDownloadPollDelay" in html
    assert "if(progress>=.95)return 2000" in html
    assert "if(progress>=.80)return 5000" in html
    assert "return 20000" in html
    assert "return 60000" in html
    assert "etaSeconds*.5" in html
    assert "etaToEighty" in html
    assert "Math.min(60000,delay)" in html


def test_old_90_and_98_percent_poll_thresholds_are_gone() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(encoding="utf-8")
    assert ">=.98" not in html
    assert ">=.90" not in html
