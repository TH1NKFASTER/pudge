from pathlib import Path


ROOT = Path(__file__).parents[1]
OLD_MAP = "{ArrowLeft:-5,ArrowRight:5,ArrowUp:15,ArrowDown:-15}"
NEW_MAP = "{ArrowLeft:-5,ArrowRight:5,ArrowUp:-15,ArrowDown:15}"


def test_runtime_shortcuts_use_natural_direction() -> None:
    html = (ROOT / "pudge/web/index.html").read_text(encoding="utf-8")
    media = (ROOT / "pudge/web/media.js").read_text(encoding="utf-8")

    assert f"const lnAudioShortcuts={NEW_MAP};" in html
    assert f"const audioShortcuts={NEW_MAP};" in media
    assert OLD_MAP not in html
    assert OLD_MAP not in media


def test_arrow_contract_tests_keep_distinct_old_and_new_maps() -> None:
    source = (
        ROOT / "tests/test_v0738_ln_no_skip_natural_arrows.py"
    ).read_text(encoding="utf-8")

    assert f'OLD_MAP = "{OLD_MAP}"' in source
    assert f'NEW_MAP = "{NEW_MAP}"' in source
