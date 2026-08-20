from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database import Database
from .media import TEXT_CODECS, find_embedded_japanese_subtitles, probe_media

_IMAGE_SUBTITLE_SUFFIXES = {".sup", ".pgs"}


@dataclass(frozen=True, slots=True)
class ResolvedSubtitle:
    external_path: Path | None = None
    embedded_subtitle_id: int | None = None
    embedded_stream_index: int | None = None
    codec: str = ""
    source: str = ""
    reason: str = ""
    recovered: bool = False
    is_text: bool = False

    @property
    def found(self) -> bool:
        return self.external_path is not None or self.embedded_subtitle_id is not None


def _usable_path(value: object, *, allow_bitmap: bool) -> Path | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    try:
        if not path.is_file() or path.stat().st_size <= 0:
            return None
    except OSError:
        return None
    if not allow_bitmap and path.suffix.casefold() in _IMAGE_SUBTITLE_SUFFIXES:
        return None
    return path.resolve()


def _history_candidates(history: dict[str, Any] | None) -> list[object]:
    if not isinstance(history, dict):
        return []
    details = history.get("details") if isinstance(history.get("details"), dict) else {}
    values: list[object] = []
    for key in ("final_path", "prepared_path", "subtitle_path", "candidate_path"):
        if details.get(key):
            values.append(details[key])
    if history.get("candidate_path"):
        values.append(history["candidate_path"])
    return values


def _probe_embedded(
    video_path: Path,
    *,
    stored_subtitle_id: int | None,
    ffprobe: str,
    ffmpeg: str,
    allow_bitmap: bool = False,
) -> tuple[int | None, int | None, str]:
    # A stored mpv sid is authoritative even when language tags are weak. Map it
    # to ffmpeg's absolute stream index directly from ffprobe first.
    if stored_subtitle_id is not None:
        sid = int(stored_subtitle_id)
        try:
            info = probe_media(video_path, ffprobe)
            streams = [row for row in info.get("streams", []) if row.get("codec_type") == "subtitle"]
            if 1 <= sid <= len(streams):
                row = streams[sid - 1]
                codec = str(row.get("codec_name") or "").casefold()
                if codec in TEXT_CODECS or allow_bitmap:
                    return sid, int(row["index"]), codec
        except Exception:
            pass

        # Library playback historically treats the DB mpv sid as authoritative
        # when the user explicitly allows image subtitles. Do not make that path
        # depend on ffprobe being able to inspect the container first.
        if allow_bitmap and sid > 0:
            return sid, None, ""

    # No usable stored sid: discover the best Japanese text stream just like the
    # desktop subtitle pipeline does.
    try:
        candidates = find_embedded_japanese_subtitles(
            video_path,
            ffprobe,
            ffmpeg,
            verbose=False,
        )
    except Exception:
        candidates = []
    candidate = next((item for item in candidates if item.codec in TEXT_CODECS), None)
    if candidate is None:
        return None, None, ""
    return int(candidate.subtitle_id), int(candidate.stream_index), str(candidate.codec or "")


def resolve_episode_subtitle(
    database: Database,
    *,
    video_path: Path,
    media_id: int | None,
    episode: int | None,
    stored_path: Path | str | None = None,
    stored_embedded_id: int | None = None,
    stored_origin: str = "",
    ffprobe: str = "ffprobe",
    ffmpeg: str = "ffmpeg",
    allow_bitmap: bool = False,
) -> ResolvedSubtitle:
    video = Path(video_path).expanduser().resolve()

    direct = _usable_path(stored_path, allow_bitmap=allow_bitmap)
    if direct is not None:
        is_text = direct.suffix.casefold() not in _IMAGE_SUBTITLE_SUFFIXES
        return ResolvedSubtitle(
            external_path=direct,
            source=str(stored_origin or "selected"),
            reason="database",
            recovered=False,
            is_text=is_text,
        )

    histories: list[dict[str, Any]] = []
    try:
        exact = database.latest_selected_subtitle(video)
        if isinstance(exact, dict):
            histories.append(exact)
    except Exception:
        pass
    try:
        fallback = database.latest_selected_subtitle_for_media_or_filename(
            video_path=video,
            media_id=media_id,
            episode=episode,
        )
        if isinstance(fallback, dict) and fallback not in histories:
            histories.append(fallback)
    except Exception:
        pass

    for history in histories:
        for value in _history_candidates(history):
            candidate = _usable_path(value, allow_bitmap=allow_bitmap)
            if candidate is None:
                continue
            is_text = candidate.suffix.casefold() not in _IMAGE_SUBTITLE_SUFFIXES
            return ResolvedSubtitle(
                external_path=candidate,
                source=str(history.get("source") or stored_origin or "history"),
                reason="subtitle_history",
                recovered=True,
                is_text=is_text,
            )

    sid, stream_index, codec = _probe_embedded(
        video,
        stored_subtitle_id=stored_embedded_id,
        ffprobe=ffprobe,
        ffmpeg=ffmpeg,
        allow_bitmap=allow_bitmap,
    )
    if sid is not None:
        return ResolvedSubtitle(
            embedded_subtitle_id=sid,
            embedded_stream_index=stream_index,
            codec=codec,
            source="embedded",
            reason="stored_embedded" if stored_embedded_id is not None else "embedded_probe",
            recovered=stored_embedded_id is None or int(stored_embedded_id) != sid,
            is_text=codec in TEXT_CODECS,
        )

    return ResolvedSubtitle(reason="no_japanese_text_subtitle")


def repair_episode_subtitle(
    database: Database,
    *,
    video_path: Path,
    selection: ResolvedSubtitle,
) -> bool:
    if not selection.found:
        return False
    path_value = str(selection.external_path) if selection.external_path is not None else None
    embedded_id = selection.embedded_subtitle_id if selection.external_path is None else None
    now = time.time()
    with database.connect() as conn:
        row = conn.execute(
            "SELECT state,subtitle_path,embedded_subtitle_id,subtitle_origin FROM episodes WHERE video_path=?",
            (str(Path(video_path).expanduser().resolve()),),
        ).fetchone()
        if row is None:
            return False
        state = str(row["state"] or "local")
        next_state = state
        if selection.is_text and state in {"local", "waiting_subtitles", "waiting_text_subtitles"}:
            next_state = "ready"
        changed = (
            str(row["subtitle_path"] or "") != str(path_value or "")
            or row["embedded_subtitle_id"] != embedded_id
            or (selection.source and str(row["subtitle_origin"] or "") != selection.source)
            or state != next_state
        )
        if not changed:
            return False
        conn.execute(
            """
            UPDATE episodes
            SET subtitle_path=?,embedded_subtitle_id=?,subtitle_origin=?,state=?,updated_at=?
            WHERE video_path=?
            """,
            (
                path_value,
                embedded_id,
                str(selection.source or row["subtitle_origin"] or ""),
                next_state,
                now,
                str(Path(video_path).expanduser().resolve()),
            ),
        )
    return True
