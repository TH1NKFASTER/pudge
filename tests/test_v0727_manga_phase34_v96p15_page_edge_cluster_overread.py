from pudge import manga_ocr_worker as worker


def test_v96p15_page_edge_cluster_member_overread_is_suppressed() -> None:
    suppress = worker._suppress_page_edge_narrow_vertical_overreads

    p37_false_cluster = {
        "text": "そして、",
        "raw_text": "そして、",
        "orientation": "vertical",
        "x": 0.970868,
        "y": 0.3265,
        "width": 0.029132,
        "height": 0.162833,
        "confidence": 0.68,
        "detector": "manga-layout-cluster-v1",
        "source": worker._LAYOUT_LINE_SOURCE,
        "selected_hypothesis_id": "manga-ocr-layout-cluster-member-v3",
        "provenance": {
            "proposal_kind": "layout_context_cluster_v2",
            "member_count": 4,
            "cluster_context_donor": True,
            "cluster_member_consensus": True,
            "member_boxes": [
                {
                    "x": 0.859026,
                    "y": 0.382333,
                    "width": 0.035895,
                    "height": 0.067833,
                    "component_count": 4,
                    "component_coverage": 0.962,
                },
                {
                    "x": 0.893421,
                    "y": 0.37,
                    "width": 0.021053,
                    "height": 0.078333,
                    "component_count": 7,
                    "component_coverage": 0.9457,
                },
                {
                    "x": 0.915605,
                    "y": 0.395667,
                    "width": 0.026684,
                    "height": 0.053667,
                    "component_count": 2,
                    "component_coverage": 0.9839,
                },
                {
                    "x": 0.970868,
                    "y": 0.3265,
                    "width": 0.029132,
                    "height": 0.162833,
                    "component_count": 2,
                    "component_coverage": 0.975,
                },
            ],
        },
    }
    p21_legit_edge = {
        "text": "しかし、",
        "raw_text": "しかし、",
        "orientation": "vertical",
        "x": 0.9735,
        "y": 0.680667,
        "width": 0.0265,
        "height": 0.080333,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "selected_hypothesis_id": "manga-ocr-xy-context-retry",
        "provenance": {
            "component_count": 3,
            "component_coverage": 0.6596,
        },
    }
    p39_legit_edge = {
        "text": "えー",
        "raw_text": "えー",
        "orientation": "vertical",
        "x": 0.9735,
        "y": 0.086,
        "width": 0.0265,
        "height": 0.062,
        "source": worker._LAYOUT_LINE_SOURCE,
        "detector": worker._LAYOUT_DETECTOR,
        "selected_hypothesis_id": "manga-ocr",
        "provenance": {
            "component_count": 2,
            "component_coverage": 1.292,
        },
    }

    repaired = suppress([p37_false_cluster, p21_legit_edge, p39_legit_edge])

    assert [piece["text"] for piece in repaired] == ["しかし、", "えー"]
