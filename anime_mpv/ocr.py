from __future__ import annotations

import hashlib
import logging
import platform
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .branding import APP_SLUG
from typing import Iterable

from PIL import Image

from .pgs import END_SEGMENT, ODS_SEGMENT, PCS_SEGMENT, PDS_SEGMENT, iter_pgs_segments

logger = logging.getLogger(__name__)


class OCRUnavailableError(RuntimeError):
    pass


class OCRConversionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OCRCue:
    start: float
    end: float
    text: str




@dataclass(frozen=True, slots=True)
class _VisionTextRow:
    y: float
    x: float
    width: float
    height: float
    text: str

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def top(self) -> float:
        return self.y + self.height


def _kana_ratio(value: str) -> float:
    meaningful = [ch for ch in value if ch.isalnum() or "\u3040" <= ch <= "\u30ff" or "\u4e00" <= ch <= "\u9fff"]
    if not meaningful:
        return 0.0
    kana = sum(1 for ch in meaningful if "\u3040" <= ch <= "\u30ff")
    return kana / len(meaningful)


def _filter_probable_furigana_rows(rows: list[_VisionTextRow]) -> list[_VisionTextRow]:
    """Drop small kana readings positioned immediately above larger base text.

    Apple Vision often exposes furigana/ruby as an independent text observation.
    Joining those observations verbatim produces subtitles such as three short
    lines of readings followed by the actual dialogue.  Geometry is much safer
    than text-only deletion: a row is removed only when it is substantially
    smaller, mostly kana, horizontally overlaps a larger row, and sits directly
    above it.
    """
    if len(rows) < 2:
        return rows
    drop: set[int] = set()
    for index, row in enumerate(rows):
        text = row.text.strip()
        if len(text) > 24 or _kana_ratio(text) < 0.58:
            continue
        for base_index, base in enumerate(rows):
            if base_index == index or base.height <= 0 or row.height <= 0:
                continue
            # Ruby is normally around half to two-thirds of the base glyph size.
            if row.height > base.height * 0.74:
                continue
            overlap = max(0.0, min(row.right, base.right) - max(row.x, base.x))
            if overlap < min(row.width, base.width) * 0.42:
                continue
            # VN coordinates are bottom-origin.  Candidate must be above the
            # base row, but allow a small amount of box overlap from OCR.
            vertical_gap = row.y - base.top
            if vertical_gap < -base.height * 0.22:
                continue
            if vertical_gap > base.height * 1.35:
                continue
            if row.width > base.width * 1.25:
                continue
            drop.add(index)
            break
    return [row for index, row in enumerate(rows) if index not in drop]


@dataclass(slots=True)
class _ObjectBitmap:
    width: int
    height: int
    data: bytes


@dataclass(slots=True)
class _CompositionObject:
    object_id: int
    x: int
    y: int


@dataclass(slots=True)
class _Composition:
    time_seconds: float
    width: int
    height: int
    palette_id: int
    objects: list[_CompositionObject]


def _parse_palette(payload: bytes) -> tuple[int, dict[int, tuple[int, int, int, int]]]:
    if len(payload) < 2:
        return 0, {}
    palette_id = payload[0]
    colors: dict[int, tuple[int, int, int, int]] = {}
    for offset in range(2, len(payload) - 4, 5):
        index, y, cr, cb, alpha = payload[offset : offset + 5]
        # ITU-R BT.601 conversion used by Blu-ray subtitle palettes.
        yf = 1.164 * (y - 16)
        r = round(yf + 1.596 * (cr - 128))
        g = round(yf - 0.392 * (cb - 128) - 0.813 * (cr - 128))
        b = round(yf + 2.017 * (cb - 128))
        colors[index] = (
            max(0, min(255, r)),
            max(0, min(255, g)),
            max(0, min(255, b)),
            int(alpha),
        )
    return palette_id, colors


def _parse_composition(payload: bytes, time_seconds: float) -> _Composition | None:
    if len(payload) < 11:
        return None
    width = int.from_bytes(payload[0:2], "big")
    height = int.from_bytes(payload[2:4], "big")
    palette_id = payload[9]
    count = payload[10]
    offset = 11
    objects: list[_CompositionObject] = []
    for _ in range(count):
        if offset + 8 > len(payload):
            break
        object_id = int.from_bytes(payload[offset : offset + 2], "big")
        cropped = bool(payload[offset + 3] & 0x80)
        x = int.from_bytes(payload[offset + 4 : offset + 6], "big")
        y = int.from_bytes(payload[offset + 6 : offset + 8], "big")
        offset += 8
        if cropped:
            offset += 8
        objects.append(_CompositionObject(object_id=object_id, x=x, y=y))
    return _Composition(
        time_seconds=time_seconds,
        width=width,
        height=height,
        palette_id=palette_id,
        objects=objects,
    )


def _consume_object_segment(
    payload: bytes,
    chunks: dict[int, bytearray],
    bitmaps: dict[int, _ObjectBitmap],
) -> None:
    if len(payload) < 4:
        return
    object_id = int.from_bytes(payload[0:2], "big")
    sequence = payload[3]
    offset = 4
    first = bool(sequence & 0x80)
    last = bool(sequence & 0x40)
    if first:
        if len(payload) < 11:
            return
        _data_length = int.from_bytes(payload[4:7], "big")
        width = int.from_bytes(payload[7:9], "big")
        height = int.from_bytes(payload[9:11], "big")
        offset = 11
        chunks[object_id] = bytearray()
        bitmaps[object_id] = _ObjectBitmap(width=width, height=height, data=b"")
    if object_id not in chunks:
        chunks[object_id] = bytearray()
    chunks[object_id].extend(payload[offset:])
    if last and object_id in bitmaps:
        bitmap = bitmaps[object_id]
        bitmaps[object_id] = _ObjectBitmap(
            width=bitmap.width,
            height=bitmap.height,
            data=bytes(chunks.pop(object_id, b"")),
        )


def _decode_rle(bitmap: _ObjectBitmap, palette: dict[int, tuple[int, int, int, int]]) -> Image.Image:
    pixels = [(0, 0, 0, 0)] * max(1, bitmap.width * bitmap.height)
    data = bitmap.data
    x = y = offset = 0

    def draw(index: int, length: int) -> None:
        nonlocal x, y
        color = palette.get(index, (255, 255, 255, 255 if index else 0))
        for _ in range(max(0, length)):
            if y >= bitmap.height:
                return
            if x >= bitmap.width:
                x = 0
                y += 1
                if y >= bitmap.height:
                    return
            pixels[y * bitmap.width + x] = color
            x += 1

    while offset < len(data) and y < bitmap.height:
        value = data[offset]
        offset += 1
        if value:
            draw(value, 1)
            continue
        if offset >= len(data):
            break
        control = data[offset]
        offset += 1
        if control == 0:
            x = 0
            y += 1
            continue
        if control & 0x40:
            if offset >= len(data):
                break
            length = ((control & 0x3F) << 8) | data[offset]
            offset += 1
        else:
            length = control & 0x3F
        if control & 0x80:
            if offset >= len(data):
                break
            index = data[offset]
            offset += 1
        else:
            index = 0
        draw(index, length)
    image = Image.new("RGBA", (bitmap.width, bitmap.height))
    image.putdata(pixels)
    return image


def decode_pgs_compositions(path: Path) -> list[tuple[float, Image.Image | None]]:
    palettes: dict[int, dict[int, tuple[int, int, int, int]]] = {}
    objects: dict[int, _ObjectBitmap] = {}
    chunks: dict[int, bytearray] = {}
    current: _Composition | None = None
    displays: list[tuple[float, Image.Image | None]] = []

    for segment in iter_pgs_segments(path.read_bytes()):
        if segment.segment_type == PCS_SEGMENT:
            current = _parse_composition(segment.payload, segment.time_seconds)
        elif segment.segment_type == PDS_SEGMENT:
            palette_id, colors = _parse_palette(segment.payload)
            if colors:
                palettes[palette_id] = colors
        elif segment.segment_type == ODS_SEGMENT:
            _consume_object_segment(segment.payload, chunks, objects)
        elif segment.segment_type == END_SEGMENT and current is not None:
            if not current.objects:
                displays.append((current.time_seconds, None))
                current = None
                continue
            canvas = Image.new("RGBA", (max(1, current.width), max(1, current.height)))
            palette = palettes.get(current.palette_id, {})
            rendered = False
            for item in current.objects:
                bitmap = objects.get(item.object_id)
                if bitmap is None or not bitmap.data:
                    continue
                layer = _decode_rle(bitmap, palette)
                canvas.alpha_composite(layer, (item.x, item.y))
                rendered = True
            displays.append((current.time_seconds, canvas if rendered else None))
            current = None
    return displays


def _vision_recognize(image: Image.Image) -> str:
    if platform.system() != "Darwin":
        raise OCRUnavailableError("Apple Vision OCR is available only on macOS")
    try:
        import Vision  # type: ignore
        from Foundation import NSURL  # type: ignore
    except ImportError as exc:
        raise OCRUnavailableError("pyobjc-framework-Vision is not installed") from exc

    bbox = image.getbbox()
    if bbox is None:
        return ""
    cropped = image.crop(bbox)
    # A dark opaque background and moderate upscale improve recognition of
    # anti-aliased PGS glyphs while retaining their original line arrangement.
    scale = 2 if max(cropped.size) >= 900 else 3
    cropped = cropped.resize((cropped.width * scale, cropped.height * scale), Image.Resampling.LANCZOS)
    background = Image.new("RGB", cropped.size, "black")
    background.paste(cropped, mask=cropped.getchannel("A"))

    with tempfile.TemporaryDirectory(prefix=f"{APP_SLUG}-ocr-") as temp_dir:
        image_path = Path(temp_dir) / "subtitle.png"
        background.save(image_path)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setRecognitionLanguages_(["ja-JP"])
        request.setUsesLanguageCorrection_(True)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(
            NSURL.fileURLWithPath_(str(image_path)), None
        )
        success, error = handler.performRequests_error_([request], None)
        if not success:
            raise OCRConversionError(str(error or "Vision request failed"))
        rows: list[_VisionTextRow] = []
        for observation in request.results() or []:
            candidates = observation.topCandidates_(1)
            if not candidates:
                continue
            text = str(candidates[0].string()).strip()
            if not text:
                continue
            box = observation.boundingBox()
            rows.append(
                _VisionTextRow(
                    y=float(box.origin.y),
                    x=float(box.origin.x),
                    width=float(box.size.width),
                    height=float(box.size.height),
                    text=text,
                )
            )
        rows = _filter_probable_furigana_rows(rows)
        rows.sort(key=lambda item: (-item.y, item.x))
        return "\n".join(item.text for item in rows).strip()


def _normalize_ocr_text(value: str) -> str:
    lines = []
    for line in value.replace("\r", "\n").splitlines():
        line = re.sub(r"[ \t\u3000]+", " ", line).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def _srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, int(round(float(seconds) * 1000)))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def _write_srt(cues: Iterable[OCRCue], destination: Path) -> None:
    blocks = []
    for index, cue in enumerate(cues, start=1):
        blocks.append(
            f"{index}\n{_srt_timestamp(cue.start)} --> {_srt_timestamp(cue.end)}\n{cue.text}\n"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("\n".join(blocks), encoding="utf-8")


def extract_embedded_pgs(
    video: Path,
    stream_index: int,
    destination: Path,
    *,
    ffmpeg_path: str,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg_path,
        "-v",
        "error",
        "-y",
        "-i",
        str(video),
        "-map",
        f"0:{int(stream_index)}",
        "-c:s",
        "copy",
        str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        stderr = getattr(exc, "stderr", b"")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        raise OCRConversionError(f"Could not extract embedded bitmap subtitles: {stderr or exc}") from exc
    return destination


def evaluate_ocr_quality(cues: list[OCRCue], display_count: int) -> dict[str, object]:
    texts = [cue.text.strip() for cue in cues if cue.text.strip()]
    joined = "".join(texts)
    meaningful = [ch for ch in joined if not ch.isspace() and not re.match(r"[\W_]", ch)]
    japanese = [ch for ch in joined if ("\u3040" <= ch <= "\u30ff") or ("\u3400" <= ch <= "\u9fff")]
    japanese_ratio = len(japanese) / max(1, len(meaningful))
    duplicate_ratio = 1.0 - len(set(texts)) / max(1, len(texts))
    recognized_ratio = len(cues) / max(1, int(display_count))
    average_length = sum(len(text.replace("\n", "")) for text in texts) / max(1, len(texts))
    warnings: list[str] = []
    if len(cues) < 8:
        warnings.append("too_few_cues")
    if japanese_ratio < 0.35:
        warnings.append("low_japanese_ratio")
    if duplicate_ratio > 0.45:
        warnings.append("too_many_duplicates")
    if recognized_ratio < 0.12:
        warnings.append("low_recognition_coverage")
    if average_length > 90:
        warnings.append("implausibly_long_lines")
    return {
        "status": "review" if warnings else "accepted",
        "accepted": not warnings,
        "warnings": warnings,
        "japanese_ratio": round(japanese_ratio, 3),
        "duplicate_ratio": round(duplicate_ratio, 3),
        "recognized_ratio": round(recognized_ratio, 3),
        "average_length": round(average_length, 1),
    }


def image_subtitle_to_srt(
    video: Path,
    cache_dir: Path,
    *,
    subtitle_path: Path | None = None,
    embedded_stream_index: int | None = None,
    embedded_codec: str | None = None,
    ffmpeg_path: str = "ffmpeg",
    force: bool = False,
) -> tuple[Path | None, dict[str, object]]:
    """Convert Japanese PGS/SUP subtitles to SRT with Apple Vision OCR.

    This function is intentionally called during subtitle preparation, not
    during playback. Text subtitles continue to win before this fallback runs.
    """
    if subtitle_path is None and embedded_stream_index is None:
        return None, {"reason": "no_image_subtitle"}
    source_identity = (
        f"file:{subtitle_path.resolve()}:{subtitle_path.stat().st_mtime_ns}:{subtitle_path.stat().st_size}"
        if subtitle_path is not None
        else f"embedded:{video.resolve()}:{video.stat().st_mtime_ns}:{video.stat().st_size}:{embedded_stream_index}"
    )
    # Include OCR post-processing generation so improvements such as ruby/
    # furigana filtering rebuild old cached OCR text instead of silently
    # reusing the previous malformed SRT.
    digest = hashlib.sha256(f"ocr-ruby-v2:{source_identity}".encode("utf-8")).hexdigest()
    output = cache_dir / "ocr" / f"{digest}.srt"
    if output.is_file() and output.stat().st_size > 0 and not force:
        return output, {"reason": "cached", "cue_count": output.read_text(encoding="utf-8", errors="ignore").count(" --> "), "quality": {"status": "accepted", "accepted": True, "warnings": []}}

    if subtitle_path is not None:
        source = subtitle_path.resolve()
        if source.suffix.casefold() not in {".sup", ".pgs"}:
            return None, {"reason": "unsupported_bitmap_format", "format": source.suffix.casefold()}
    else:
        normalized_codec = str(embedded_codec or "").casefold()
        if normalized_codec and normalized_codec not in {
            "hdmv_pgs_subtitle",
            "pgs",
            "pgssub",
        }:
            return None, {
                "reason": "unsupported_bitmap_codec",
                "codec": normalized_codec,
            }
        source = cache_dir / "ocr" / f"{digest}.sup"
        extract_embedded_pgs(
            video,
            int(embedded_stream_index),
            source,
            ffmpeg_path=ffmpeg_path,
        )

    displays = decode_pgs_compositions(source)
    cues: list[OCRCue] = []
    recognized = 0
    recognition_cache: dict[str, str] = {}
    for index, (start, image) in enumerate(displays):
        if image is None:
            continue
        next_time = displays[index + 1][0] if index + 1 < len(displays) else start + 4.0
        end = max(start + 0.08, next_time)
        image_key = hashlib.sha256(
            f"{image.width}x{image.height}:".encode("ascii") + image.tobytes()
        ).hexdigest()
        if image_key not in recognition_cache:
            recognition_cache[image_key] = _normalize_ocr_text(_vision_recognize(image))
        text = recognition_cache[image_key]
        if not text:
            continue
        recognized += 1
        if cues and cues[-1].text == text and start - cues[-1].end <= 0.25:
            previous = cues[-1]
            cues[-1] = OCRCue(start=previous.start, end=end, text=previous.text)
        else:
            cues.append(OCRCue(start=start, end=end, text=text))
    if not cues:
        return None, {
            "reason": "ocr_no_text",
            "display_count": len(displays),
            "recognized_count": recognized,
        }
    quality = evaluate_ocr_quality(cues, len(displays))
    _write_srt(cues, output)
    logger.info(
        "RESULT step=subtitle.ocr video=%s source=%s cues=%s displays=%s output=%s",
        video.name,
        source,
        len(cues),
        len(displays),
        output,
    )
    return output, {
        "reason": "ocr_ready",
        "cue_count": len(cues),
        "display_count": len(displays),
        "recognized_count": recognized,
        "quality": quality,
    }
