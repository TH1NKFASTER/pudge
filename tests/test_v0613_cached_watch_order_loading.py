from pathlib import Path


def test_cached_watch_order_does_not_flash_loading_copy_immediately() -> None:
    html = (Path(__file__).parents[1] / "anime_mpv" / "web" / "index.html").read_text(
        encoding="utf-8"
    )

    assert "showLoading:false" in html
    assert "context.showLoading?" in html
    assert "},200);" in html
    assert "clearRelationLoadingTimer(context);" in html
    assert "ui.relationContext!==context||!context.loading" in html
