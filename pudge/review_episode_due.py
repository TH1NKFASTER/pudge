from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .media import TEXT_CODECS, find_embedded_japanese_subtitles, probe_media
from .models import EmbeddedSubtitle
from .subtitle_formats import convert_to_plain_srt, parse_srt



def resolve_episode_review_subtitle(
    *,
    video_path: Path,
    subtitle_path: Path | None,
    embedded_subtitle_id: int | None,
    cache_dir: Path,
    ffmpeg_path: str = "ffmpeg",
    ffprobe_path: str = "ffprobe",
) -> tuple[Path, dict[str, Any]]:
    """Return a text subtitle file suitable for episode-scoped Jiten parsing.

    Prefer an already prepared external subtitle. If the library row points at
    an embedded Japanese text track (or the row is stale but the MKV contains
    one), extract that exact track to a content-addressed SRT cache. Bitmap
    tracks are intentionally not OCR'd here: the review gate only consumes
    trustworthy text subtitles.
    """
    external = Path(subtitle_path).expanduser() if subtitle_path is not None else None
    if external is not None and external.is_file():
        return external, {"source": "external", "path": str(external)}

    video = Path(video_path).expanduser()
    if not video.is_file():
        raise FileNotFoundError(f"Episode video is unavailable: {video}")

    selected: EmbeddedSubtitle | None = None
    # The library's stored mpv sid is already the track Pudge selected for
    # playback. Resolve that ordinal directly first: Netflix/multi-sub releases
    # often contain many tracks, and rescanning every unlabelled subtitle just to
    # rediscover the stored one can make the review gate unnecessarily slow.
    if embedded_subtitle_id is not None:
        try:
            info = probe_media(video, str(ffprobe_path or "ffprobe"))
            streams = [
                row for row in (info.get("streams") or [])
                if isinstance(row, dict) and row.get("codec_type") == "subtitle"
            ]
            sid = int(embedded_subtitle_id)
            if 1 <= sid <= len(streams):
                row = streams[sid - 1]
                codec = str(row.get("codec_name") or "").casefold()
                if codec in TEXT_CODECS:
                    tags = row.get("tags") if isinstance(row.get("tags"), dict) else {}
                    selected = EmbeddedSubtitle(
                        stream_index=int(row["index"]),
                        subtitle_id=sid,
                        codec=codec,
                        language=str(tags.get("language") or ""),
                        title=str(tags.get("title") or ""),
                        score=1000.0,
                    )
        except Exception:
            selected = None

    if selected is None:
        candidates = [
            candidate
            for candidate in find_embedded_japanese_subtitles(
                video, str(ffprobe_path or "ffprobe"), str(ffmpeg_path or "ffmpeg")
            )
            if candidate.codec in TEXT_CODECS
        ]
        if not candidates:
            if embedded_subtitle_id is not None:
                raise ValueError(
                    "The episode has embedded subtitles, but no Japanese text subtitle track can be extracted for review."
                )
            raise FileNotFoundError(
                "This episode has no prepared external or embedded Japanese text subtitles, so Pudge cannot determine its due words."
            )
        selected = next(
            (item for item in candidates if item.subtitle_id == embedded_subtitle_id),
            candidates[0],
        )
    stat = video.stat()
    digest = hashlib.sha256(
        (
            f"review-gate-embedded-v1:{video.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:"
            f"{selected.stream_index}:{selected.subtitle_id}:{selected.codec}"
        ).encode("utf-8")
    ).hexdigest()[:24]
    output_dir = Path(cache_dir).expanduser() / "review_gate" / "embedded"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{digest}.srt"
    if output.is_file() and output.stat().st_size > 0:
        return output, {
            "source": "embedded",
            "cached": True,
            "path": str(output),
            "stream_index": selected.stream_index,
            "subtitle_id": selected.subtitle_id,
            "codec": selected.codec,
        }

    handle = tempfile.NamedTemporaryFile(
        prefix=f".{digest}.", suffix=".srt", dir=output_dir, delete=False
    )
    temporary = Path(handle.name)
    handle.close()
    try:
        completed = subprocess.run(
            [
                str(ffmpeg_path or "ffmpeg"),
                "-y",
                "-v",
                "error",
                "-nostdin",
                "-i",
                str(video),
                "-map",
                f"0:{selected.stream_index}",
                "-c:s",
                "srt",
                str(temporary),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if completed.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
            detail = str(completed.stderr or completed.stdout or "").strip()[-800:]
            raise ValueError(
                "Could not extract the embedded Japanese text subtitles"
                + (f": {detail}" if detail else "")
            )
        temporary.replace(output)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"Could not extract the embedded Japanese text subtitles: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)

    return output, {
        "source": "embedded",
        "cached": False,
        "path": str(output),
        "stream_index": selected.stream_index,
        "subtitle_id": selected.subtitle_id,
        "codec": selected.codec,
    }

def _pair(item: dict[str, Any]) -> tuple[int, int] | None:
    try:
        word_id = int(item.get("wordId"))
        reading_index = int(item.get("readingIndex"))
    except (TypeError, ValueError):
        return None
    if word_id <= 0 or reading_index < 0:
        return None
    return word_id, reading_index


def _is_due(state: dict[str, Any]) -> bool:
    normalized = str(state.get("normalizedState") or "").strip().casefold()
    if normalized == "due":
        return True
    states = state.get("states") or state.get("knownState") or []
    return any(str(value or "").strip().casefold() == "due" for value in states if value is not None)


def _definitions(item: dict[str, Any]) -> list[dict[str, Any]]:
    chunks = item.get("meaningsChunks") or item.get("meanings") or []
    if not isinstance(chunks, list):
        return []
    pos = item.get("partsOfSpeech") or []
    if not isinstance(pos, list):
        pos = []
    rows: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        meanings = chunk if isinstance(chunk, list) else [chunk]
        cleaned = [str(value).strip() for value in meanings if str(value or "").strip()]
        if cleaned:
            rows.append({"index": index, "meanings": cleaned, "partsOfSpeech": list(pos)})
    return rows


def _media_image_url(media: dict[str, Any] | None) -> str:
    if not isinstance(media, dict):
        return ""
    image = media.get("image")
    if not isinstance(image, dict):
        return ""
    return str(image.get("url") or "").strip()


def build_episode_due_review_cards(
    service: Any,
    subtitle_path: Path,
    cache_dir: Path,
) -> dict[str, Any]:
    """Return currently-due Jiten cards occurring in exactly one subtitle file.

    The subtitle corpus defines membership; Jiten live state defines whether each
    word is currently due. The first subtitle cue containing a card becomes its
    local context sentence. No new Jiten cards are created.
    """
    subtitle = Path(subtitle_path).expanduser()
    if not subtitle.is_file():
        raise FileNotFoundError(f"Subtitle file is unavailable: {subtitle}")

    plain_path, conversion = convert_to_plain_srt(
        subtitle,
        Path(cache_dir).expanduser() / "review_gate",
    )
    if plain_path.suffix.casefold() != ".srt":
        raise ValueError(
            "Episode subtitles could not be converted to plain SRT for Jiten parsing"
        )
    cues = parse_srt(plain_path)
    cue_rows = [
        (float(start), float(end), " ".join(str(text or "").split()).strip())
        for start, end, text in cues
        if " ".join(str(text or "").split()).strip()
    ]
    if not cue_rows:
        return {"cards": [], "episode_pairs": 0, "due_pairs": 0, "conversion": conversion}

    stat = plain_path.stat()
    digest = hashlib.sha256(
        (
            f"review-gate-episode-v1:{plain_path.resolve()}:"
            f"{stat.st_size}:{stat.st_mtime_ns}"
        ).encode("utf-8")
    ).hexdigest()
    parsed = service.jiten_preparse("\n".join(row[2] for row in cue_rows), digest=digest)
    token_rows = parsed.get("tokens") if isinstance(parsed, dict) else None
    vocabulary = parsed.get("vocabulary") if isinstance(parsed, dict) else None
    if not isinstance(token_rows, list):
        token_rows = []
    if not isinstance(vocabulary, list):
        vocabulary = []

    context: dict[tuple[int, int], dict[str, Any]] = {}
    order: list[tuple[int, int]] = []
    occurrences: dict[tuple[int, int], int] = {}
    for index, tokens in enumerate(token_rows):
        if index >= len(cue_rows) or not isinstance(tokens, list):
            continue
        start, end, sentence = cue_rows[index]
        for token in tokens:
            if not isinstance(token, dict):
                continue
            pair = _pair(token)
            if pair is None:
                continue
            occurrences[pair] = occurrences.get(pair, 0) + 1
            if pair not in context:
                context[pair] = {
                    "sentence": sentence,
                    "start": start,
                    "end": end,
                }
                order.append(pair)

    if not order:
        return {"cards": [], "episode_pairs": 0, "due_pairs": 0, "conversion": conversion}

    vocab_by_pair: dict[tuple[int, int], dict[str, Any]] = {}
    for item in vocabulary:
        if not isinstance(item, dict):
            continue
        pair = _pair(item)
        if pair is not None:
            vocab_by_pair[pair] = item

    live_by_pair: dict[tuple[int, int], dict[str, Any]] = {}
    for offset in range(0, len(order), 400):
        chunk = order[offset : offset + 400]
        for state in service.jiten_live_states(chunk, force=True):
            if not isinstance(state, dict):
                continue
            pair = _pair(state)
            if pair is not None:
                live_by_pair[pair] = state

    due_pairs = [pair for pair in order if _is_due(live_by_pair.get(pair, {}))]
    media_by_pair: dict[tuple[int, int], dict[str, Any]] = {}
    if due_pairs and hasattr(service, "jiten_card_media"):
        try:
            media_by_pair = service.jiten_card_media(due_pairs)
        except Exception:
            # Card media is cosmetic. Never make the review gate fail because a
            # signed image URL could not be fetched.
            media_by_pair = {}

    cards: list[dict[str, Any]] = []
    for word_id, reading_index in due_pairs:
        pair = (word_id, reading_index)
        item = vocab_by_pair.get(pair, {})
        ctx = context[pair]
        spelling = str(item.get("spelling") or "").strip()
        reading = str(item.get("reading") or "").strip()
        word_text = spelling or reading or f"#{word_id}"
        card: dict[str, Any] = {
            "wordId": word_id,
            "readingIndex": reading_index,
            "wordText": word_text,
            "wordTextPlain": word_text,
            "reading": reading,
            "readings": ([{
                "text": reading,
                "rubyText": reading,
                "readingIndex": reading_index,
                "formType": 0,
            }] if reading else []),
            "definitions": _definitions(item),
            "partsOfSpeech": list(item.get("partsOfSpeech") or []),
            "pitchAccents": list(item.get("pitchAccents") or []),
            "frequencyRank": int(item.get("frequencyRank") or 0),
            "pudgeCardKey": f"{word_id}:{reading_index}",
            "pudgePreviouslyReviewed": True,
            "pudgeProvider": "jiten",
            "pudgeContextSentence": str(ctx["sentence"]),
            "pudgeContextStart": float(ctx["start"]),
            "pudgeContextEnd": float(ctx["end"]),
            "pudgeEpisodeOccurrences": int(occurrences.get(pair, 1)),
        }
        image_url = _media_image_url(media_by_pair.get(pair))
        if image_url:
            card["pudgeContextImage"] = image_url
        cards.append(card)

    return {
        "cards": cards,
        "episode_pairs": len(order),
        "due_pairs": len(due_pairs),
        "conversion": conversion,
    }
