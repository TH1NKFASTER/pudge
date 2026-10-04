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


def save_playback(db: Database, video: Path, *, position: float, duration: float,
                  active_seconds: float = 0, session: str = "", sequence: int = 0,
                  active_total: float = 0, ledger: ConsumptionLedger | None = None) -> int:
    import math
    if not all(math.isfinite(float(value)) for value in (position,duration,active_seconds,active_total)):
        raise ValueError("Playback measurements must be finite")
    video = video.expanduser().resolve()
    # Progress, ledger and retry marker commit together across GUI and fallback.
    with db.connection_scope() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if db.episode_by_path(video) is None:
            return 0
        active = active_seconds
        marker = None
        if session:
            marker = "playback_save:v1:" + hashlib.sha256((str(video) + session).encode()).hexdigest()
            previous = json.loads(db.get_state(marker, '{"sequence":-1,"total":0}'))
            if sequence <= previous['sequence']:
                return 0
            active = max(0.0, active_total - float(previous['total']))
        (ledger or ConsumptionLedger(db)).record_anime_playback(video, position=position,
            duration=duration, active_seconds=active)
        db.record_playback(video, position, duration, active)
        if marker:
            db.set_state(marker, json.dumps({'sequence':sequence,
                'total':max(float(previous['total']), active_total)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument('--config', type=Path, default=DEFAULT_CONFIG_PATH)
    p.add_argument('--playback-save', action='store_true')
    p.add_argument('--playback-video', type=Path, required=True)
    p.add_argument('--playback-position', type=float, default=0)
    p.add_argument('--playback-duration', type=float, default=0)
    p.add_argument('--playback-active-seconds', type=float, default=0)
    p.add_argument('--playback-session', default='')
    p.add_argument('--playback-sequence', type=int, default=0)
    p.add_argument('--playback-active-total', type=float, default=0)
    a = p.parse_args(argv)
    try:
        cfg = load_config(a.config)
        db = Database(cfg.library.database_path, initialize=False)
        video = a.playback_video.expanduser().resolve()
        return save_playback(db, video, position=a.playback_position, duration=a.playback_duration,
            active_seconds=a.playback_active_seconds, session=a.playback_session,
            sequence=a.playback_sequence, active_total=a.playback_active_total)
    except Exception as exc:
        print(f'Playback save warning: {exc}', file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
