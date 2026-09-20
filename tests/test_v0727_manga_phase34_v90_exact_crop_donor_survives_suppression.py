from pudge.manga_ocr_worker import _suppress_redundant_implausible_wide_vertical_donors


def _peer():
    return {
        "text": "それは",
        "orientation": "vertical",
        "x": 0.546053,
        "y": 0.8625,
        "width": 0.019737,
        "height": 0.035,
        "source": "manga-layout-line-v1",
        "detector": "manga-ink-components-v1",
        "provenance": {},
    }


def _donor(*, proven: bool):
    segments = [
        {
            "text": ch,
            "orientation": "vertical",
            "x": 0.565605,
            "y": 0.8265 + i * 0.005,
            "width": 0.031947,
            "height": 0.010,
            "source": "layout-line-ink-v2+tight-v1",
        }
        for i, ch in enumerate("私がやります")
    ]
    provenance = {
        "wide_vertical_text_donor": True,
        "wide_vertical_exact_crop_reread": proven,
        "wide_vertical_exact_crop_original_text": "私がやり",
        "wide_vertical_exact_crop_text": "私がやります",
        "wide_vertical_exact_crop_added_japanese": 2,
    }
    return {
        "text": "私がやります",
        "raw_text": "私がやります",
        "orientation": "vertical",
        "x": 0.565605,
        "y": 0.8265,
        "width": 0.031947,
        "height": 0.072,
        "source": "manga-layout-line-v1",
        "detector": "wide-vertical-text-donor-v1",
        "segments": segments,
        "recognition_selection": "wide-vertical-layout-donor-exact-crop-v1" if proven else "wide-vertical-layout-donor-v1",
        "provenance": provenance,
    }


def test_v90_ink_proven_exact_crop_donor_survives_redundant_donor_suppression(monkeypatch):
    # Force the legacy plausibility branch so this test exercises only the new
    # evidence exemption rather than depending on tuning inside the scorer.
    monkeypatch.setattr("pudge.manga_ocr_worker._layout_retry_acceptable", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("pudge.manga_ocr_worker._layout_text_geometry_plausible", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("pudge.manga_ocr_worker._layout_vertical_overlap", lambda *_args, **_kwargs: 0.9)

    result = _suppress_redundant_implausible_wide_vertical_donors([_donor(proven=True), _peer()])

    assert [item["text"] for item in result] == ["私がやります", "それは"]


def test_v90_unproven_implausible_wide_donor_is_still_suppressed(monkeypatch):
    monkeypatch.setattr("pudge.manga_ocr_worker._layout_retry_acceptable", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("pudge.manga_ocr_worker._layout_text_geometry_plausible", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("pudge.manga_ocr_worker._layout_vertical_overlap", lambda *_args, **_kwargs: 0.9)

    result = _suppress_redundant_implausible_wide_vertical_donors([_donor(proven=False), _peer()])

    assert [item["text"] for item in result] == ["それは"]
