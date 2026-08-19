from pathlib import Path


HTML = Path(__file__).parents[1] / "pudge" / "web" / "index.html"


def test_repaired_functions_have_no_detached_old_suffixes() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "\n,card={}){" not in source
    assert "\n,extra={}){" not in source
    assert "\n){const speechActive=options.speechActive!==false" not in source
    assert source.count("function lnAudioReadingWeights(") == 1
    assert source.count("function lnPairedTrace(") == 1
    assert source.count("function renderLnPairedPosition(") == 1


def test_repaired_sync_features_remain_present() -> None:
    source = HTML.read_text(encoding="utf-8")

    assert "function lnPairedResetWordProgress(reader)" in source
    assert "function lnPairedSmoothOffset(" in source
    assert "until:performance.now()+34" not in source
    assert "surface:lnPairedSurface(current)" in source
    assert 'class="ln-reader-actions"' in source
    assert "#lnFinishVolume::after{content:attr(data-short-label)}" in source
