from pathlib import Path


def test_pyinstaller_uses_webrtcvad_wheels_compatibility_hook() -> None:
    script = Path(__file__).resolve().parents[1] / "install.sh"
    text = script.read_text(encoding="utf-8")

    assert 'hook-webrtcvad.py' in text
    assert 'copy_metadata("webrtcvad-wheels")' in text
    assert '--additional-hooks-dir "$HOOK_DIR"' in text
