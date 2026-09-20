from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"
PREVIEW = ROOT / "pudge" / "web" / "cover_preview.js"


def test_unparsed_reader_gaps_are_audiobook_words_not_plain_text() -> None:
    html = HTML.read_text(encoding="utf-8")

    assert "function lnAudioFallbackMoraWeights(surface)" in html
    assert "function renderLnFallbackGap(" in html
    assert 'class="ln-word ln-word-fallback"' in html
    assert 'data-ln-fallback="1"' in html
    assert "ui.lnTokenMap.set(key,token)" in html
    assert "appendGap(start)" in html
    assert "appendGap(text.length)" in html

    # A fallback word must participate in the same paired-audio coordinate
    # system as Jiten tokens, so Play from here and the running fill work.
    gap = html.split("function renderLnFallbackGap", 1)[1].split(
        "function renderLnParagraph", 1
    )[0]
    assert "data-ln-audio-start" in gap
    assert "data-ln-audio-end" in gap
    assert "data-ln-audio-weights" in gap
    assert "lnAudioFallbackMoraWeights(part)" in gap


def test_fallback_words_use_shared_study_card_with_audio_actions() -> None:
    html = HTML.read_text(encoding="utf-8")
    popup = html.split("async function showLnPopup", 1)[1].split(
        "async function showLnNyaa", 1
    )[0]

    assert "if(token.fallback)" in popup
    assert "No Jiten entry" not in popup
    assert "Нет записи Jiten" not in popup
    assert 'id="lnPlayFromWord"' in popup
    assert "data-ln-bookmark-here" in popup
    assert popup.index("PudgeReadingTools?.study?.open") < popup.index(
        "if(token.fallback)"
    )


def test_blurred_reader_uses_same_inline_img_and_reveal_only_removes_blur() -> None:
    html = HTML.read_text(encoding="utf-8")
    preview = PREVIEW.read_text(encoding="utf-8")

    arm = html.split("function lnArmInlineImages", 1)[1].split(
        "function lnReplaceReaderHtmlPreservingImages", 1
    )[0]
    assert "const deferHidden=root.classList.contains('blur-images')" not in arm
    assert "img.removeAttribute('src')" not in arm
    assert "lnLoadInlineImage(img,figure)" in arm

    assert "function lnBuildInlineRasterPreview" not in html
    assert "canvas.className='ln-inline-image-raster'" not in html

    click = html.split(
        "const figure=event.target.closest?.('#lnReader .ln-inline-image')", 1
    )[1].split("document.addEventListener('contextmenu'", 1)[0]
    assert "reader?.classList.contains('blur-images')&&!figure?.classList.contains('revealed')" in click
    assert "lnMarkInlineImageRevealed(figure);return;" in click
    assert "image.removeAttribute('src')" not in click
    assert "lnBuildInlineRasterPreview" not in click
    assert "window.PudgeCoverPreview?.open?.(image)" in click

    assert "function openPreviewSource(source,restoreFocusTo=null)" in preview
    assert "openSource:source=>openPreviewSource(source)" in preview


def test_blur_mode_is_ttsu_style_css_over_the_real_image() -> None:
    html = HTML.read_text(encoding="utf-8")
    assert ".ln-reader.blur-images .ln-inline-image:not(.revealed) .ln-inline-image-spoiler-label{display:inline-block}" in html
    assert ".ln-reader.blur-images .ln-inline-image:not(.revealed) img{filter:blur(44px);cursor:pointer}" in html
    assert ".ln-inline-image-raster" not in html
    assert ".ln-inline-image-placeholder" not in html
