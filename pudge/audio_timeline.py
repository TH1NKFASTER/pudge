"""Where decoded audio sits on the playback timeline (subtitle timing).

Subtitles are shown against mpv's playback time.  With mpv's default
``--rebase-start-time=yes`` playback time 0 is the file's start time: the
``format.start_time`` that libavformat/ffprobe report (the earliest audio or
video packet, e.g. 1.4 s in an MPEG-TS capture).  External subtitle times are
compared with that playback time, and the embedded text tracks Pudge extracts
(``ffmpeg -i video -map 0:s:N out.srt``) come out on it too: FFmpeg subtracts
the same start time.  Every "seconds in the extracted audio" therefore has to
be measured from the file start.  What FFmpeg 6.1 does (checked on synthetic
MKV/MP4/MPEG-TS files, see tests/test_fix_audio_playback_timeline.py):

* A stream can start after the file: a DUAL/remuxed release whose Japanese
  track begins 1.0 s after the video.  Decoding it to WAV/FLAC/raw PCM writes
  its first decoded sample first (these formats keep no timestamps), so the
  leading gap disappears and every time measured in the extracted audio is that
  delay early.  The same holds for ffsubsync and ALASS (both decode the video
  with FFmpeg into raw PCM), Whisper STT on an extracted FLAC and FFmpeg
  windows that start before the audio.  In MPEG-TS, mapping the audio alone
  even makes FFmpeg count from the audio's own start (also with
  ``aresample=first_pts=0``) and an input ``-ss`` lands 0.3-1.8 s off.
  ``delay = stream start_time - format start_time`` (ffprobe) is the leading
  silence to add (``adelay``; for raw speech arrays, leading non-speech frames):
  then the extracted audio starts at playback time 0.  Windows of such a track
  are cut from one padded whole-track FLAC (exact seeking in every container).
  A gap inside a track collapses the same way (a 1.5 s remux seam put every
  later window 1.5 s early), so that copy is made with ``aresample=async=1``,
  which fills inner gaps from the timestamps.
* Such a gap also collapses in a track that starts on time: every whole-track
  decode (the STT FLAC, ALASS, windows across it) was early by the gap after
  it. ``inner_gaps`` finds them from the packet timestamps (one ffprobe pass
  over the packets, no decoding: about 1 s for a 24-minute episode); a file
  with gaps is then read like a late track (the padded FLAC, ``delay`` 0).
  In MP4 the packet before the gap carries it as its duration, so the gap is
  measured against the track's usual packet duration. ffsubsync decodes with
  ``aresample=async=1`` itself.
* ``-ss X`` before ``-i`` seeks relative to the file start: a window from X is
  exact once every stream has started (MKV/MP4, and MPEG-TS without a delay).
  Seeking to 0 in Matroska/MPEG-TS lands on the first video keyframe, so when
  the audio starts *before* the video (the opposite case) the audio before
  that keyframe is lost (0.5 s audio lead: the window was 0.53 s early); a
  window from the file start therefore reads without ``-ss``.  A stream that
  starts first defines the file start: no leading silence is needed for it.
* AAC/Opus encoder delay makes ordinary files start their audio 6-23 ms
  before the video; below MIN_OFFSET_SECONDS (a frame at 50 fps) a start
  difference is treated as none, so those files keep their old caches.
* A file that does not start at 0 (an MPEG-TS capture at 1.4 s, an
  audio + subtitles proxy whose first packet is at 1.02 s) needs nothing
  more: FFmpeg's output time 0 and its extracted subtitle times are that
  start, as mpv's playback time 0 is.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# A stream starting less than this after the file start starts with it.  Also
# the delay from which already prepared subtitles are prepared again.
MIN_OFFSET_SECONDS = 0.02
# Audio that starts this much before the video: a window from 0 read with
# ``-ss`` lost it (below this the loss is the encoder delay of normal files).
VIDEO_LEAD_MIN_SECONDS = 0.05
# Recorded in alignment results made with playback-timeline audio (rebuild rule).
# 1: late tracks padded; 2: gaps inside a track filled as well.
VERSION = 2
# The smallest timestamp jump ``aresample=async=1`` fills (its min_hard_comp).
GAP_MIN_SECONDS = 0.1

_NOT_MEDIA = frozenset(
    {".srt", ".ass", ".ssa", ".vtt", ".sup", ".sub", ".idx", ".npz", ".npy", ".json", ".txt"}
)
# Audio files Pudge writes itself (and plain audio files): one stream, it is the start.
_AUDIO_ONLY = frozenset({".flac", ".wav", ".mp3", ".m4a", ".aac", ".opus", ".ogg"})


def probe_command(ffprobe: str, video: Path) -> list[str]:
    """The one ffprobe call that selects the Japanese track and reads the start times."""
    return [
        str(ffprobe),
        "-v",
        "error",
        "-show_entries",
        "stream=index,codec_type,codec_name,start_time,channels"
        ":stream_tags=language,title:stream_disposition=default,attached_pic"
        ":format=start_time",
        "-of",
        "json",
        str(video),
    ]


def _seconds(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) < 1e9 else None


def is_audio_row(row: dict[str, Any]) -> bool:
    # Rows without codec_type come from probes restricted to audio streams.
    return str(row.get("codec_type") or "audio").casefold() == "audio"


@dataclass(frozen=True)
class AudioTimeline:
    """Start times of one file, relative to its playback time 0."""

    format_start: float
    audio_starts: tuple[float | None, ...]  # per audio position (0:a:N)
    audio_indexes: tuple[int | None, ...]
    audio_channels: tuple[int, ...]
    video_start: float | None
    audio_defaults: tuple[bool, ...] = ()  # the default disposition flag per audio position

    @classmethod
    def from_payload(cls, payload: object) -> AudioTimeline | None:
        if not isinstance(payload, dict):
            return None
        rows = [row for row in payload.get("streams") or [] if isinstance(row, dict)]
        audio = [row for row in rows if is_audio_row(row)]
        video = [
            row
            for row in rows
            if str(row.get("codec_type") or "").casefold() == "video"
            and not int(_disposition(row).get("attached_pic") or 0)
        ]
        audio_starts = tuple(_seconds(row.get("start_time")) for row in audio)
        video_starts = [
            value for value in (_seconds(row.get("start_time")) for row in video) if value is not None
        ]
        fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}
        format_start = _seconds(fmt.get("start_time"))
        if format_start is None:
            known = [value for value in (*audio_starts, *video_starts) if value is not None]
            if not known:
                return None
            format_start = min(known)
        indexes: list[int | None] = []
        channels: list[int] = []
        defaults = tuple(bool(int(_disposition(row).get("default") or 0)) for row in audio)
        for row in audio:
            try:
                indexes.append(int(row.get("index")))
            except (TypeError, ValueError):
                indexes.append(None)
            try:
                channels.append(int(row.get("channels") or 0))
            except (TypeError, ValueError):
                channels.append(0)
        return cls(
            format_start=float(format_start),
            audio_starts=audio_starts,
            audio_indexes=tuple(indexes),
            audio_channels=tuple(channels),
            video_start=min(video_starts) if video_starts else None,
            audio_defaults=defaults,
        )

    def position_of(self, index: int | None) -> int | None:
        if index is None:
            return None
        try:
            return self.audio_indexes.index(int(index))
        except ValueError:
            return None

    def default_position(self) -> int | None:
        """FFmpeg's own audio choice without ``-map`` (ffmpeg_mux_init.c in 6.1/7).

        Its score is ``channels + 5000000 * default``: a default track first, then
        the most channels, then the first such track.
        """
        if not self.audio_starts:
            return None

        def score(item: int) -> tuple[bool, int, int]:
            default = self.audio_defaults[item] if item < len(self.audio_defaults) else False
            return (default, self.audio_channels[item], -item)

        return max(range(len(self.audio_starts)), key=score)

    def delay(self, *, position: int | None = None, index: int | None = None) -> float:
        """Seconds of playback time before that audio stream's first sample (0.0 below the minimum).

        ``index``: the absolute stream index; ``position``: ``0:a:N``; neither: the first track.
        """
        if index is not None:
            position = self.position_of(index)
            if position is None:
                return 0.0
        if position is None:
            position = 0
        if not 0 <= position < len(self.audio_starts):
            return 0.0
        start = self.audio_starts[position]
        if start is None:
            return 0.0
        value = start - self.format_start
        return round(value, 6) if value >= MIN_OFFSET_SECONDS else 0.0

    def max_delay(self) -> float:
        return max((self.delay(position=item) for item in range(len(self.audio_starts))), default=0.0)

    def video_lead(self) -> float:
        """Seconds the video starts after the file start (audio first), 0.0 below the minimum."""
        if self.video_start is None:
            return 0.0
        value = self.video_start - self.format_start
        return round(value, 6) if value >= VIDEO_LEAD_MIN_SECONDS else 0.0


def _disposition(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("disposition")
    return value if isinstance(value, dict) else {}


_CACHE: dict[tuple[str, int, int], AudioTimeline | None] = {}
_MISS = object()


def _key(video: Path) -> tuple[str, int, int] | None:
    try:
        stat = video.stat()
        return (str(video.resolve()), int(stat.st_size), int(stat.st_mtime_ns))
    except OSError:
        return None


def remember(video: Path, payload: object) -> AudioTimeline | None:
    """Keep the start times from a probe (one per file and stat)."""
    key = _key(Path(video))
    timeline = AudioTimeline.from_payload(payload)
    if key is not None:
        if len(_CACHE) >= 512:
            _CACHE.clear()
        _CACHE[key] = timeline
    return timeline


def timeline(video: Path, ffprobe: str | None) -> AudioTimeline | None:
    """The file's start times (cached per stat), or None for audio files or without ffprobe.

    Only a probe that ran is cached. A failed one (a busy disk, a timeout) is tried
    again by the next caller instead of standing for "no delay" for the process.
    """
    video = Path(video)
    suffix = video.suffix.casefold()
    if suffix in _NOT_MEDIA or suffix in _AUDIO_ONLY:
        return None
    key = _key(video)
    if key is None:
        return None
    cached = _CACHE.get(key, _MISS)
    if cached is not _MISS:
        return cached  # type: ignore[return-value]
    binary = str(ffprobe or "").strip()
    if not binary:
        return None
    payload: object = None
    try:
        completed = subprocess.run(
            probe_command(binary, video),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode == 0:
            payload = json.loads(completed.stdout or "{}")
    except Exception:  # noqa: BLE001 - a best-effort probe: unknown means "no delay"
        payload = None
    if not isinstance(payload, dict) or not isinstance(payload.get("streams"), list):
        return None
    return remember(video, payload)


def audio_delay(
    video: Path, ffprobe: str | None, *, position: int | None = None, index: int | None = None
) -> float:
    found = timeline(video, ffprobe)
    return found.delay(position=position, index=index) if found is not None else 0.0


def ffprobe_beside(ffmpeg: str | None) -> str | None:
    """The ffprobe installed with that ffmpeg (Homebrew installs both), else PATH's for a bare name."""
    command = str(ffmpeg or "ffmpeg")
    resolved = shutil.which(command) if "/" not in command else command
    if resolved and Path(resolved).is_file():
        sibling = Path(resolved).with_name("ffprobe")
        if sibling.is_file():
            return str(sibling)
    return shutil.which("ffprobe") if "/" not in command else None


_GAPS: dict[tuple[tuple[str, int, int], str], tuple[tuple[float, float], ...]] = {}


def gaps_command(ffprobe: str, video: Path, selector: str) -> list[str]:
    """Packet timestamps of one audio stream (demuxing only, no decoding)."""
    return [
        str(ffprobe),
        "-v",
        "error",
        "-select_streams",
        selector,
        "-show_entries",
        "packet=pts_time,duration_time",
        "-of",
        "csv=p=0",
        str(video),
    ]


def gaps_from_packets(rows: list[tuple[float, float | None]]) -> tuple[tuple[float, float], ...]:
    """(container time where the gap starts, its length) for each jump of GAP_MIN_SECONDS or more.

    A packet is expected to end after its own duration, at most the track's usual
    one: MP4 stores a gap as the duration of the packet before it, which still
    decodes to one frame.
    """
    durations = sorted(duration for _pts, duration in rows if duration is not None and duration > 0)
    if not durations:
        deltas = sorted(b[0] - a[0] for a, b in zip(rows, rows[1:]) if b[0] > a[0])
        durations = deltas
    if not durations:
        return ()
    usual = durations[len(durations) // 2]
    found: list[tuple[float, float]] = []
    previous: tuple[float, float | None] | None = None
    # By time, then duration (the old tuple order); a missing duration sorts first instead
    # of raising TypeError when two packets share a timestamp and only one has a duration.
    for row in sorted(rows, key=lambda item: (item[0], item[1] is not None, item[1] or 0.0)):
        if previous is not None:
            pts, duration = previous
            expected = pts + (min(duration, usual) if duration is not None and duration > 0 else usual)
            gap = row[0] - expected
            if gap >= GAP_MIN_SECONDS:
                found.append((round(expected, 6), round(gap, 6)))
        previous = row
    return tuple(found)


def inner_gaps(
    video: Path, ffprobe: str | None, *, position: int | None = None, index: int | None = None
) -> tuple[tuple[float, float], ...] | None:
    """Timestamp gaps inside one audio track (cached per file and stat); None when not probed.

    ``index``: the absolute stream index; ``position``: ``0:a:N``; neither: the first track.
    Raw PCM/FLAC decoded from such a track loses each gap: what follows it comes out
    early by the gap unless the decode fills it (``delay_filter``).
    """
    video = Path(video)
    suffix = video.suffix.casefold()
    if suffix in _NOT_MEDIA:
        return None
    key = _key(video)
    binary = str(ffprobe or "").strip()
    if key is None or not binary:
        return None
    if index is not None:
        # The same track by its absolute index and by ``0:a:N`` is one packet scan (the STT
        # step asks by index, the segment windows and ALASS by position): use the position
        # whenever this file's probe already told it.
        known = _CACHE.get(key)
        found = known.position_of(index) if known is not None else None
        selector = f"a:{found}" if found is not None else str(int(index))
    else:
        selector = f"a:{int(position or 0)}"
    cached = _GAPS.get((key, selector))
    if cached is not None:
        return cached
    try:
        completed = subprocess.run(
            gaps_command(binary, video, selector),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=300,
            check=False,
        )
    except Exception:  # noqa: BLE001 - a best-effort probe: unknown is tried again later
        return None
    if completed.returncode != 0:
        return None
    rows: list[tuple[float, float | None]] = []
    for line in (completed.stdout or "").splitlines():
        parts = line.strip().strip(",").split(",")
        pts = _seconds(parts[0]) if parts and parts[0] else None
        if pts is None:
            continue
        rows.append((pts, _seconds(parts[1]) if len(parts) > 1 else None))
    gaps = gaps_from_packets(rows)
    if len(_GAPS) >= 512:
        _GAPS.clear()
    _GAPS[(key, selector)] = gaps
    return gaps


def gaps_marker(gaps: tuple[tuple[float, float], ...] | None) -> str:
    """Cache-name part of a gapped track (empty for none: those keep their names)."""
    if not gaps:
        return ""
    return f":inner-gaps-v{VERSION}={len(gaps)}/{sum(length for _at, length in gaps):.3f}"


def delay_filter(delay: float) -> str:
    """Leading silence that puts decoded audio on the playback timeline.

    ``aresample=async=1`` first: raw PCM/FLAC keeps no timestamps, so a gap inside
    the track (a remux seam) would otherwise collapse and everything after it be
    early by the gap. It fills such gaps; it never fills the leading one (its first
    timestamp is its start), which ``adelay`` adds.
    """
    return f"aresample=async=1,adelay={max(0, int(round(float(delay) * 1000)))}:all=1"


def spec_position(spec: str | None) -> int | None:
    """``2`` for the ``0:a:2`` stream specifier, None for anything else."""
    text = str(spec or "").strip().rstrip("?")
    prefix = "0:a:"
    if text.startswith(prefix) and text[len(prefix) :].isdigit():
        return int(text[len(prefix) :])
    return None
