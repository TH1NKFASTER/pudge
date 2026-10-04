from __future__ import annotations

import json
from pathlib import Path

import pytest

from pudge.providers import seadex as sd

SAMPLE = json.loads((Path(__file__).parent / "fixtures" / "seadex_entry_165790.json").read_text())
HASH_A = SAMPLE["items"][0]["expand"]["trs"][2]["infoHash"]
HASH_B = SAMPLE["items"][0]["expand"]["trs"][3]["infoHash"]


def _with_best_nyaa(sample):
    data = json.loads(json.dumps(sample))
    data["items"][0]["expand"]["trs"][3]["isBest"] = True  # synthetic: a public best release
    return data


def test_parse_real_response():
    rec = sd.parse_entries(165790, SAMPLE, fetched_at=1.0)
    assert rec.status == "found" and rec.best_groups == ("-ZR-",)
    assert rec.preferred_hashes == frozenset(), "the best torrent is private (AB): nothing public to match"
    assert rec.alternative_hashes == {HASH_A.casefold(), HASH_B.casefold()}
    assert rec.alternative_nyaa_ids == {"1880265", "1883348"}
    assert rec.raw_counts == {"torrents": 4, "nyaa": 2, "private": 2}
    assert sd.parse_entries(1, {"items": []}, fetched_at=1.0).status == "missing"
    with pytest.raises(ValueError):
        sd.parse_entries(1, {"oops": 1}, fetched_at=1.0)


def test_match_is_exact_hash_or_nyaa_id_never_group():
    rec = sd.parse_entries(165790, _with_best_nyaa(SAMPLE), fetched_at=1.0)
    assert rec.match(HASH_B.upper()) == "preferred"
    assert rec.match("", "https://nyaa.si/view/1880265") == "alternative"
    assert rec.match("f" * 40, "https://nyaa.si/view/999") == ""
    assert sd.bonus_for(rec, HASH_B)[0] == sd.PREFERRED_BONUS
    assert sd.bonus_for(rec, HASH_A)[1] == ["seadex-exact", "seadex-alternative", "seadex-bonus=40"]
    assert sd.bonus_for(rec, "f" * 40) == (0.0, [])
    assert sd.bonus_for(None, HASH_B) == (0.0, [])


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


def test_cache_ttls_and_errors(tmp_path):
    clock = Clock()
    calls = []
    responses = [(200, SAMPLE), (200, {"items": []})]

    def fetch(url, params, timeout):
        calls.append(params["filter"])
        return responses.pop(0) if responses else (403, None)

    client = sd.SeaDexClient(tmp_path, fetch=fetch, now=clock)
    assert client.recommendations(165790).status == "found"
    assert client.recommendations(165790).status == "found" and len(calls) == 1, "24 h cache"
    assert client.recommendations(2).status == "missing"
    clock.t += sd.MISSING_TTL_SECONDS + 1
    stale_missing = client.recommendations(2)  # refetch -> 403
    assert stale_missing.status == "missing" and stale_missing.stale and "403" in stale_missing.error
    clock.t += sd.FOUND_TTL_SECONDS
    stale = client.recommendations(165790)
    assert stale.status == "found" and stale.stale, "network error keeps the last good snapshot"
    assert client.recommendations(3).status == "unavailable", "an error is never 'no entry'"
    assert client.recommendations(4, allow_network=False).status == "unavailable"
    assert calls == ["alID=165790", "alID=2", "alID=2", "alID=165790", "alID=3"]


def test_score_release_bonus_is_additive_and_not_doubled(monkeypatch):
    from pudge.manager_models import LibraryAnime, NyaaRelease
    from pudge.providers import nyaa
    from pudge.providers.nyaa import score_release

    # Title similarity is not under test here (and needs native rapidfuzz).
    monkeypatch.setattr(nyaa, "_title_match_score", lambda anime, title, alternatives=(): (100.0, ["title=100"]))

    anime = LibraryAnime(media_id=165790, title="365 Days to the Wedding", titles=["365 Days to the Wedding"], episodes=12)
    kwargs = dict(
        episode=1, batch=False, trusted_groups=[], preferred_groups=[], blocked_groups=[], preferred_resolution="1080p",
        min_seeders=1, target_episode_min_bytes=250 * 1024 * 1024, target_episode_max_bytes=3500 * 1024 * 1024,
    )

    def release(info_hash):
        return NyaaRelease(
            title="[SubsPlease] 365 Days to the Wedding - 01 (1080p) [ABCD1234].mkv", link="https://nyaa.si/view/1", torrent_url="",
            info_hash=info_hash, size_text="1.4 GiB", size_bytes=1400 * 1024 * 1024, seeders=30, leechers=2, downloads=500,
            trusted=True, remake=False, group="SubsPlease",
        )

    rec = sd.parse_entries(165790, _with_best_nyaa(SAMPLE), fetched_at=1.0)
    plain = score_release(release(HASH_B), anime, **kwargs)
    boosted = score_release(release(HASH_B), anime, **kwargs, seadex=rec)
    again = score_release(boosted, anime, **kwargs, seadex=rec)
    same_group_other_hash = score_release(release("e" * 40), anime, **kwargs, seadex=rec)
    assert boosted.score == pytest.approx(plain.score + 60)
    assert again.score == boosted.score, "re-scoring does not stack the bonus"
    assert same_group_other_hash.score == pytest.approx(plain.score)
    assert "seadex-preferred" in boosted.reasons and "seadex-bonus=60" in boosted.reasons
    assert boosted.trusted is plain.trusted
