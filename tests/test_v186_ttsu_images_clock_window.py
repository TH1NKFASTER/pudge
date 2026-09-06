from pathlib import Path
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "pudge" / "web" / "index.html"


def test_spoiler_image_matches_ttsu_architecture_not_canvas_pipeline() -> None:
    html = HTML.read_text(encoding="utf-8")
    frame = html.split(".ln-inline-image-frame{", 1)[1].split("}", 1)[0]
    assert "overflow:hidden" in frame
    assert "contain:" not in frame
    assert "translateZ" not in frame
    assert "backface" not in frame
    assert "filter:blur(44px)" in html
    assert 'src="${src}" data-ln-src="${src}"' in html
    assert "ln-inline-image-spoiler-label" in html
    assert "lnBuildInlineRasterPreview" not in html
    assert "lnBuildInlineBlurPreview(img)" not in html
    assert "ln-inline-image-raster" not in html


def test_first_click_unblurs_same_img_second_click_opens_preview() -> None:
    html = HTML.read_text(encoding="utf-8")
    click = html.split(
        "const figure=event.target.closest?.('#lnReader .ln-inline-image')", 1
    )[1].split("document.addEventListener('contextmenu'", 1)[0]
    assert "lnMarkInlineImageRevealed(figure);return;" in click
    assert "image.removeAttribute('src')" not in click
    assert "window.PudgeCoverPreview?.open?.(image);" in click


def test_activity_path_carries_vad_through_lookahead_anchors() -> None:
    html = HTML.read_text(encoding="utf-8")
    fn = html.split("function lnPairedOffsetAtTime", 1)[1].split(
        "function lnPairedResetWordProgress", 1
    )[0]
    assert "lnPairedSpeechRatioRange(anchor,left.time,right.time,value)" in fn
    assert "value>=windowRight" not in fn


def test_backend_poll_correction_keeps_monotonic_clock_on_stale_backward_sample() -> None:
    html = HTML.read_text(encoding="utf-8")
    reconcile = html.split("function lnPairedTransportClockReconcile", 1)[1].split(
        "function lnPairedTransportClockReset", 1
    )[0]
    assert "staleBackward=true" in reconcile
    assert "Math.abs(drift)<=1.5" in reconcile
    assert "maxSettledLead" not in reconcile


def test_lookahead_path_advances_smoothly_and_holds_only_inside_real_vad_gap() -> None:
    html = HTML.read_text(encoding="utf-8")
    start = html.index("function lnPairedSpeechRatioRange")
    end = html.index("function lnPairedResetWordProgress", start)
    source = html[start:end]
    script = """
const lnPairedWeightedPosition=x=>x;
const lnPairedSourcePosition=x=>x;
""" + source + """
const state={chapter_char_offset_exact:125,anchor_window:{
  left_time:14084.195,left_offset:125,right_time:14084.415,right_offset:126,
  activity_clock:true,
  activity:[{start:14084.195,end:14085.935}],
  path:[
    {time:14084.195,offset:125},{time:14084.415,offset:126},
    {time:14084.635,offset:127},{time:14084.895,offset:128},
    {time:14085.455,offset:130},{time:14085.635,offset:131},
    {time:14085.935,offset:133}
  ]
}};
console.log(JSON.stringify({
  first:lnPairedOffsetAtTime(state,14084.30),
  betweenA:lnPairedOffsetAtTime(state,14084.50),
  betweenB:lnPairedOffsetAtTime(state,14084.60),
  second:lnPairedOffsetAtTime(state,14084.75),
  late:lnPairedOffsetAtTime(state,14085.80)
}));
"""
    result = subprocess.run(
        ["node", "-e", script], cwd=ROOT, check=True, text=True, capture_output=True
    )
    values = json.loads(result.stdout)
    assert 125 < values["first"] < 126
    assert 126 < values["betweenA"] < values["betweenB"] < 127
    assert 127 < values["second"] < 128
    assert 131 < values["late"] < 133
