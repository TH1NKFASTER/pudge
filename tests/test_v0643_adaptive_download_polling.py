"""How often the window checks a running download (behaviour, page harness; audit 71Б rewrite).

The first check after learning about a download comes in 20 s; then the delay follows the
measured speed: half the remaining time, between 20 s and 60 s, but never sleeping past 80 %;
from 80 % every 5 s, from 95 % every 2 s (it used to be 90 % and 98 %)."""

from __future__ import annotations

import json

# The backend's answer: the download moves at RATE (fraction per second) from START.
MOVING = r"""
const started = Date.now();
const progress = () => Math.min(0.9995, START + RATE * (Date.now() - started) / 1000);
__api.poll_downloads_and_subtitles = async () =>
  ({state: {...(await __api.get_state()), downloads: [{...DOWNLOAD, progress: progress()}]}});
await __tick(SPAN);
return __apiCalls.filter(call => call.name === 'poll_downloads_and_subtitles')
  .map(call => ({at: call.at, progress: Math.min(0.9995, START + RATE * call.at / 1000)}));
"""
DOWNLOAD = {"name": "Show 03", "torrent_hash": "abc", "state": "downloading"}


def _polls(page, start: float, rate: float, span: int) -> list[dict]:
    code = (
        MOVING.replace("START", repr(start))
        .replace("RATE", repr(rate))
        .replace("SPAN", str(span))
        .replace("DOWNLOAD", json.dumps(DOWNLOAD))
    )
    # Torrents are on (with them off nothing can move: tests/test_fix_torrent_startup_polling.py).
    state = {"downloads": [{**DOWNLOAD, "progress": start}], "settings": {"torrents_enabled": True}}
    return page.run(code, state=state).value


def _gaps(polls: list[dict]) -> list[tuple[float, int]]:
    """(progress at a check, time until the next check) pairs."""
    return [(earlier["progress"], later["at"] - earlier["at"]) for earlier, later in zip(polls, polls[1:])]


def test_download_polling_is_eta_adaptive_and_uses_requested_thresholds(page) -> None:
    # 0.1 %/s from 50 %: about eight minutes left.
    polls = _polls(page, 0.5, 0.001, 520_000)
    assert polls[0]["at"] == 20_000  # speed unknown yet
    gaps = _gaps(polls)
    below = [gap for progress, gap in gaps if progress < 0.80]
    # Far from 80 %: the 60 s ceiling; the last wait is shortened to land on 80 %.
    assert below == [60_000, 60_000, 60_000, 60_000, 40_000]
    assert all(gap == 5_000 for progress, gap in gaps if 0.80 <= progress < 0.95)
    assert all(gap == 2_000 for progress, gap in gaps if 0.95 <= progress < 0.999)
    assert any(0.90 <= progress < 0.95 for progress, _gap in gaps)  # the old 90 % step is gone
    assert any(0.95 <= progress < 0.98 for progress, _gap in gaps)  # and the old 98 % one
    # Finished: back to the idle check every 15 s.
    assert polls[-2]["progress"] >= 0.999 and gaps[-1][1] == 15_000


def test_fast_download_waits_half_its_remaining_time(page) -> None:
    # 1 %/s from 20 %: after the 20 s probe 60 s remain -> wait 30 s; then 30 s remain,
    # but 80 % is 10 s away -> wait 10 s; then 5 s steps.
    polls = _polls(page, 0.2, 0.01, 75_000)
    assert [poll["at"] for poll in polls[:4]] == [20_000, 50_000, 60_000, 65_000]
    # 1.4 %/s: at 50 % about 36 s remain and 80 % is 21 s away -> the 20 s minimum.
    polls = _polls(page, 0.22, 0.014, 40_000)
    assert [poll["at"] for poll in polls[:2]] == [20_000, 40_000]


def test_stalled_download_is_checked_once_a_minute(page) -> None:
    polls = _polls(page, 0.3, 0.0, 200_000)
    assert [poll["at"] for poll in polls] == [20_000, 80_000, 140_000, 200_000]
