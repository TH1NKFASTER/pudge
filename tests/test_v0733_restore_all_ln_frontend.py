from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).parents[1]
HTML = ROOT / "pudge/web/index.html"


def test_all_ln_frontend_features_are_restored() -> None:
    source = HTML.read_text(encoding="utf-8")

    required = [
        "function toggleLnPairedPlayback()",
        "function lnAudioReadingWeights(",
        "function lnPairedSpeechActive(",
        "function lnPairedResetWordProgress(",
        "function lnPairedSmoothOffset(",
        "function exportLnPairedTrace()",
        "light_novel_export_audio_sync_trace",
        "event.code!=='KeyL'",
        "{reason:'poll',speechActive:true,previewOffset}",
        "const previousWord=previous;",
        "surface:lnPairedSurface(current)",
        'class="ln-reader-actions"',
        'data-wide-label="Light Novels"',
        'data-short-label="Finish"',
        "#lnReaderClose::after",
        "#lnFinishVolume::after{content:attr(data-short-label)}",
    ]
    missing = [value for value in required if value not in source]
    assert not missing, missing

    assert "\nasync\nasync function applyLnPairedPosition" not in source
    assert not re.search(r"(?m)^\s*async\s*$", source)


def test_inline_javascript_parses() -> None:
    source = HTML.read_text(encoding="utf-8")
    scripts = [
        match.group(2)
        for match in re.finditer(
            r"<script([^>]*)>(.*?)</script>",
            source,
            flags=re.I | re.S,
        )
        if not re.search(r"\bsrc\s*=", match.group(1), flags=re.I)
    ]
    assert scripts

    for index, script in enumerate(scripts):
        path = ROOT / f".pudge-restore-inline-{index}.js"
        path.write_text(script, encoding="utf-8")
        try:
            subprocess.run(["node", "--check", str(path)], cwd=ROOT, check=True)
        finally:
            path.unlink(missing_ok=True)
