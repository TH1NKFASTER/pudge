from PIL import Image

from pudge import manga_ocr_worker as worker


def _fragment(
    x: float,
    y: float,
    width: float,
    height: float,
    *,
    component_count: int,
    component_coverage: float,
) -> dict[str, object]:
    return {
        "text": "",
        "raw_text": "",
        "orientation": "vertical",
        "orientation_reason": "observed-undilated-ink-components",
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "source": "manga-layout-line-v1",
        "detector": "manga-ink-components-raw-v1",
        "geometry_source": "manga-ink-components-raw-v1",
        "geometry_status": "observed",
        "provenance": {
            "component_count": component_count,
            "component_coverage": component_coverage,
        },
    }


class _GapModel:
    def __init__(self, merged: str = "うわああ～～っ！！") -> None:
        self.merged = merged

    def __call__(self, crop: Image.Image) -> str:
        # p030-like physical sizes at 760x1200: lower ~67 px, upper ~154 px,
        # merged ~314 px (plus a few pixels of recovery padding).
        if crop.height < 100:
            return "っ！！"
        if crop.height < 230:
            return "うわああ"
        return self.merged


def test_gapped_vertical_sfx_recovers_wave_bridge_only_with_prefix_suffix_consensus(monkeypatch):
    upper = _fragment(
        0.852632,
        0.8075,
        0.051316,
        0.128333,
        component_count=5,
        component_coverage=0.9671,
    )
    lower = _fragment(
        0.860526,
        0.674167,
        0.040789,
        0.055833,
        component_count=3,
        component_coverage=0.7692,
    )
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda _image: [upper, lower])

    image = Image.new("RGB", (760, 1200), "white")
    recover = getattr(worker, "_recover_gapped_vertical_sfx_fragments", lambda _model, _image, regions: regions)
    result = recover(_GapModel(), image, [])

    donors = [
        region
        for region in result
        if region.get("recognition_selection") == "gapped-vertical-sfx-consensus-v1"
    ]
    assert [region.get("text") for region in donors] == ["うわああ～～っ！！"]
    donor = donors[0]
    provenance = donor.get("provenance") or {}
    assert provenance.get("gapped_vertical_sfx_bridge") is True
    assert provenance.get("upper_direct_text") == "うわああ"
    assert provenance.get("lower_direct_text") == "っ！！"


def test_gapped_vertical_sfx_rejects_semantic_content_inside_the_gap(monkeypatch):
    upper = _fragment(
        0.852632,
        0.8075,
        0.051316,
        0.128333,
        component_count=5,
        component_coverage=0.9671,
    )
    lower = _fragment(
        0.860526,
        0.674167,
        0.040789,
        0.055833,
        component_count=3,
        component_coverage=0.7692,
    )
    monkeypatch.setattr(worker, "_raw_vertical_lines", lambda _image: [upper, lower])

    image = Image.new("RGB", (760, 1200), "white")
    recover = getattr(worker, "_recover_gapped_vertical_sfx_fragments", lambda _model, _image, regions: regions)
    result = recover(_GapModel("うわああ何っ！！"), image, [])

    assert all(
        region.get("recognition_selection") != "gapped-vertical-sfx-consensus-v1"
        for region in result
    )
