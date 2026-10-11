"""A word card made from a VN line (Screen text panel, Visual novels page) gets the game
picture the line was read from, the way a manga add gets its page: the VN capture keeps
a downscaled JPEG of the frame each line was first read from (last few lines, memory
only), and study_action uploads it as the Jiten card's picture. JPDB cards cannot hold
a picture: the add says so."""

from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image

from pudge.light_novels import LightNovelService
from pudge.visual_novels import VisualNovelService
from tests.i18n_support import en
from tests.test_patch_e_ui import make_api


def game_frame(colour: str, size: tuple[int, int] = (3456, 2168)) -> Image.Image:
    return Image.new("RGBA", size, colour)


def colour_of(jpeg: bytes) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(jpeg)) as image:
        assert image.format == "JPEG"
        return image.convert("RGB").getpixel((5, 5))


def read(service: VisualNovelService, image: Image.Image, text: str, *, commit: bool = False) -> int:
    """What the capture loop does after OCR of one frame (and when a line has settled)."""
    frame_id = service._remember_frame(1, image, text)
    service._set_current_text(1, text)
    service._state["current_frame_id"] = frame_id
    if commit:
        service._commit_candidate(1, text, text)
    return frame_id


def fresh_service() -> VisualNovelService:
    service = VisualNovelService()
    service._generation = 1
    return service


def test_each_line_keeps_the_frame_it_was_first_read_from() -> None:
    service = fresh_service()
    partial = read(service, game_frame("red"), "それが")  # half-typed
    first = read(service, game_frame("blue"), "それが、二階堂ヒロだった。")
    assert first != partial
    # The same line read again later (another frame) keeps its first frame.
    assert read(service, game_frame("green"), "それが、二階堂ヒロだった。", commit=True) == first
    [row] = service.state()["transcript"]
    assert row["frame_id"] == first
    jpeg = service.frame_jpeg(first)
    assert colour_of(jpeg)[2] > 200  # blue: the frame the line was read from, not the newer one
    with Image.open(io.BytesIO(jpeg)) as image:
        assert max(image.size) <= 1600  # downscaled
    # A half-typed text's frame is not kept once the line has settled.
    assert service.frame_jpeg(partial) is None


def test_only_the_last_few_lines_keep_a_frame() -> None:
    service = fresh_service()
    ids = [read(service, game_frame("white", (320, 200)), f"台詞{index}", commit=True) for index in range(12)]
    kept = [frame_id for frame_id in ids if service.frame_jpeg(frame_id)]
    assert kept == ids[-VisualNovelService._FRAMES_KEPT :]
    # A new capture starts empty.
    service._frames.clear()
    assert service.frame_jpeg(ids[-1]) is None


def make(tmp_path: Path):
    api = make_api(tmp_path)
    service = api.visual_novels
    service._generation = 1
    frame_id = read(service, game_frame("blue"), "二階堂ヒロ", commit=True)
    calls: list[dict] = []

    def action(backend, action, word_id, reading_index, **kwargs):
        calls.append({"backend": backend, "action": action, **kwargs})
        return {"ok": True, "outcome": "confirmed", "provider": backend}

    api.light_novels.study_action = action  # the Jiten/JPDB request stand-in
    return api, frame_id, calls


def test_adding_a_word_from_a_vn_line_attaches_its_game_picture(tmp_path) -> None:
    api, frame_id, calls = make(tmp_path)
    try:
        result = api.study_action(
            {
                "backend": "jiten",
                "action": "add",
                "word_id": 7,
                "reading_index": 0,
                "deck_id": "12",
                "sentence": "それが、二階堂ヒロだった。",
                "media_context": {"kind": "vn", "frame_id": frame_id},
            }
        )
        [call] = calls
        assert call["media_image"] == api.visual_novels.frame_jpeg(frame_id)
        assert call["media_filename"] == f"pudge-vn-frame-{frame_id}.jpg"
        assert call["media_source"] == "vn"
        assert "enrichment_warnings" not in result
        # A line whose frame is no longer kept: the word is added and the toast says why there is no picture.
        result = api.study_action(
            {
                "backend": "jiten",
                "action": "add",
                "word_id": 7,
                "reading_index": 0,
                "deck_id": "12",
                "media_context": {"kind": "vn", "frame_id": 999},
            }
        )
        assert calls[-1]["media_image"] is None and result["enrichment_warnings"] == [en("vn.cardImageGone")]
    finally:
        api.close()


def test_jpdb_cards_cannot_hold_the_picture_and_say_so(tmp_path) -> None:
    api, frame_id, calls = make(tmp_path)
    try:
        result = api.study_action(
            {
                "backend": "jpdb",
                "action": "add",
                "word_id": 7,
                "reading_index": 0,
                "deck_id": "forq",
                "id_namespace": "jpdb",
                "media_context": {"kind": "vn", "frame_id": frame_id},
            }
        )
        assert calls[0]["media_image"] is None
        assert result["ok"] is True and result["enrichment_warnings"] == [en("vn.jpdbNoCardImage")]
    finally:
        api.close()


def test_jiten_add_uploads_the_game_picture_as_card_media(tmp_path) -> None:
    api = make_api(tmp_path)
    try:
        service: LightNovelService = api.light_novels
        requests: list[tuple] = []
        service._jiten_mutation = lambda path, body: requests.append(("deck", path, body)) or {}
        service._jiten_add_custom_sentence = lambda *args: requests.append(("sentence", *args))
        service._jiten_upload_card_image = lambda w, r, data, filename="": requests.append(
            ("media", w, r, data, filename)
        )
        result = service.study_action(
            "jiten",
            "add",
            7,
            0,
            deck_id="12",
            sentence="それが、二階堂ヒロだった。" * 20,
            surface="二階堂ヒロ",
            media_image=b"JPEG",
            media_filename="pudge-vn-frame-3.jpg",
            media_source="vn",
        )
        assert result["ok"] is True
        kinds = [entry[0] for entry in requests]
        assert kinds == ["deck", "sentence", "media"]
        assert len(requests[0][2]["sentence"]) == 150  # mined like a manga page: full sentence on the card
        assert requests[2][3:] == (b"JPEG", "pudge-vn-frame-3.jpg")
    finally:
        api.close()


PARSED = {
    "paragraphs": ["二階堂ヒロ"],
    "tokens": [[{"wordId": 7, "readingIndex": 0, "start": 0, "end": 3}]],
    "vocabulary": [
        {
            "wordId": 7,
            "readingIndex": 0,
            "spelling": "二階堂",
            "states": ["new"],
            "studyDeckIds": [],
            "meanings": ["Nikaidou"],
        }
    ],
}
PAGE_API = (
    f"window.PARSED = {json.dumps(PARSED, ensure_ascii=False)};"
    "__api.visual_novel_parse = async () => window.PARSED; __api.study_parse_text = async () => window.PARSED;"
    "__api.study_decks = async () => [{id: '12', name: 'Mining'}];"
    "__api.study_action = async payload => ({ok: true, outcome: 'confirmed'});"
)
ADD = r"""
await __click('#ROOT [data-pudge-study-token]'); await __tick(50);
await __input('#pudgeStudyDeck', '12'); await __tick(10);
await __click('#pudgeStudyCard [data-pudge-study-add]'); await __tick(50);
return true;
"""


def test_panel_and_vn_page_send_the_lines_frame_with_the_add(page) -> None:
    panel = page.run(
        "await __tick(50);" + ADD.replace("#ROOT", "#screenTextBody"),
        name="screen_text.html",
        api={"screen_text_settings": {"language": "en", "study_backend": "jiten"}},
        before=PAGE_API
        + "__api.screen_text_state = async () => ({running: true, status: 'reading', text: '二階堂ヒロ', text_id: 1, frame_id: 5, ocr_count: 1});",
    )
    [[added]] = panel.calls_of("study_action")
    assert added["action"] == "add" and added["media_context"] == {"kind": "vn", "frame_id": 5}
    vn_page = page.run(
        "setPage('visualnovels'); await __tick(100);" + ADD.replace("#ROOT", "#vnCurrent"),
        before=PAGE_API
        + "__api.visual_novel_state = async () => ({running: true, status: 'reading', capture_count: 1, ocr_count: 1,"
        " transcript: [{id: 1, text: '二階堂ヒロ', frame_id: 3}], current_text: '二階堂ヒロ', current_frame_id: 3});",
    )
    [[added]] = vn_page.calls_of("study_action")
    assert added["media_context"] == {"kind": "vn", "frame_id": 3}
