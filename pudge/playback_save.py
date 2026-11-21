"""Small mpv heartbeat entry point without subtitle/OCR/UI imports."""

from __future__ import annotations
import argparse
import sys
import hashlib
import json
from pathlib import Path
from .config import DEFAULT_CONFIG_PATH, load_config
from .database import Database
from .consumption import ConsumptionLedger


def save_playback(
    db: Database,
    video: Path,
    *,
    position: float,
    duration: float,
    active_seconds: float = 0,
    session: str = "",
    sequence: int = 0,
    active_total: float = 0,
    ledger: ConsumptionLedger | None = None,
    sub_delay: float | None = None,
    subtitle: str | None = None,
    subtitle_sid: int | None = None,
    cache_dir: Path | None = None,
) -> int:
    import math

    if not all(math.isfinite(float(value)) for value in (position, duration, active_seconds, active_total)):
        raise ValueError("Playback measurements must be finite")
    video = video.expanduser().resolve()
    # Progress, ledger and retry marker commit together across GUI and fallback.
    with db.connection_scope() as conn:
        conn.execute("BEGIN IMMEDIATE")
        episode = db.episode_by_path(video)
        if episode is None:
            return 0
        active = active_seconds
        marker = None
        if session:
            marker = "playback_save:v1:" + hashlib.sha256((str(video) + session).encode()).hexdigest()
            previous = json.loads(db.get_state(marker, '{"sequence":-1,"total":0}'))
            if sequence <= previous["sequence"]:
                return 0
            active = max(0.0, active_total - float(previous["total"]))
        (ledger or ConsumptionLedger(db)).record_anime_playback(
            video, position=position, duration=duration, active_seconds=active
        )
        try:
            # The part just played, for watched credit summed across devices (iPad + mpv).
            from .watch_coverage import record_mpv_heartbeat

            record_mpv_heartbeat(db, episode, position=position, duration=duration, active_seconds=active)
        except Exception as exc:  # noqa: BLE001 - never blocks saving the place
            print(f"Watch coverage warning: {exc}", file=sys.stderr)
        db.record_playback(video, position, duration, active)
        if sub_delay is not None:
            # mpv's z/x delay, kept for the next launch of this same subtitle
            # (after the sequence check: a late, older save must not undo it).
            from .subtitle_delay import remember_subtitle_delay

            if not subtitle and subtitle_sid is None:
                subtitle_sid = getattr(episode, "embedded_subtitle_id", None)
            remember_subtitle_delay(
                db,
                video,
                sub_delay,
                subtitle=subtitle or None,
                embedded_sid=None if subtitle else subtitle_sid,
                source=_library_subtitle_source(episode, subtitle, subtitle_sid, cache_dir),
            )
        if marker:
            db.set_state(
                marker,
                json.dumps({"sequence": sequence, "total": max(float(previous["total"]), active_total)}),
            )
    return 0


def _library_subtitle_source(
    episode: object, subtitle: str | None, subtitle_sid: int | None, cache_dir: Path | None
) -> str:
    """The row's subtitle identity when mpv shows that subtitle (or its cleaned copy), else ""."""
    from .subtitle_delay import subtitle_identity

    source = getattr(episode, "subtitle_path", None)
    embedded = getattr(episode, "embedded_subtitle_id", None)
    if subtitle:
        if source is None or cache_dir is None:
            return ""
        from .subtitle_formats import is_playback_copy_of

        return subtitle_identity(source) if is_playback_copy_of(subtitle, source, cache_dir) else ""
    if (
        source is None
        and embedded is not None
        and subtitle_sid is not None
        and int(subtitle_sid) == int(embedded)
    ):
        return subtitle_identity(None, embedded)
    return ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    p.add_argument("--playback-save", action="store_true")
    p.add_argument("--playback-video", type=Path, required=True)
    p.add_argument("--playback-position", type=float, default=0)
    p.add_argument("--playback-duration", type=float, default=0)
    p.add_argument("--playback-active-seconds", type=float, default=0)
    p.add_argument("--playback-session", default="")
    p.add_argument("--playback-sequence", type=int, default=0)
    p.add_argument("--playback-active-total", type=float, default=0)
    p.add_argument("--playback-sub-delay", type=float, default=None)
    p.add_argument("--playback-subtitle", default="")
    p.add_argument("--playback-subtitle-sid", type=int, default=None)
    a = p.parse_args(argv)
    try:
        from .restore_guard import database_use

        cfg = load_config(a.config)
        # Waits while the app restores a backup over this database.
        with database_use(cfg.library.database_path):
            db = Database(cfg.library.database_path, initialize=False)
            video = a.playback_video.expanduser().resolve()
            return save_playback(
                db,
                video,
                position=a.playback_position,
                duration=a.playback_duration,
                active_seconds=a.playback_active_seconds,
                session=a.playback_session,
                sequence=a.playback_sequence,
                active_total=a.playback_active_total,
                sub_delay=a.playback_sub_delay,
                subtitle=a.playback_subtitle,
                subtitle_sid=a.playback_subtitle_sid,
                cache_dir=cfg.paths.cache_dir,
            )
    except Exception as exc:
        print(f"Playback save warning: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
