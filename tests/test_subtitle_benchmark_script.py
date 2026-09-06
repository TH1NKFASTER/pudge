from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import json
from typing import Any

import pytest

import pudge.subtitle_benchmark_cli as benchmark_script


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _Client:
    pages: list[dict[str, Any]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._index = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def post(self, *_args: Any, **_kwargs: Any) -> _Response:
        payload = type(self).pages[self._index]
        self._index += 1
        return _Response(payload)


def _media(media_id: int, title: str, episodes: int | None) -> dict[str, Any]:
    return {
        "id": media_id,
        "title": {
            "romaji": title,
            "english": None,
            "native": None,
            "userPreferred": title,
        },
        "synonyms": [],
        "episodes": episodes,
        "format": "TV",
        "seasonYear": 2020,
        "status": "FINISHED",
        "duration": 24,
        "popularity": 1000 - media_id,
        "meanScore": 80,
        "relations": {"edges": []},
    }


def test_top_plan_expands_known_episodes_and_keeps_unresolved(monkeypatch) -> None:
    _Client.pages = [
        {
            "data": {
                "Page": {
                    "pageInfo": {"hasNextPage": False},
                    "media": [_media(1, "One", 2), _media(2, "Unknown", None)],
                }
            }
        }
    ]
    monkeypatch.setattr(benchmark_script.httpx, "Client", _Client)
    payload = benchmark_script.fetch_top_anime_plan(
        "https://example.invalid/graphql",
        limit=2,
        sort="popularity",
        sample_episodes=0,
        seed=7,
    )
    assert payload["anime_count"] == 2
    assert payload["episode_count"] == 2
    assert [row["episode"] for row in payload["episodes"]] == [1, 2]
    assert payload["unresolved_anime"][0]["media_id"] == 2


def test_top_plan_sampling_is_deterministic(monkeypatch) -> None:
    _Client.pages = [
        {
            "data": {
                "Page": {
                    "pageInfo": {"hasNextPage": False},
                    "media": [_media(10, "Ten", 10)],
                }
            }
        }
    ]
    monkeypatch.setattr(benchmark_script.httpx, "Client", _Client)
    first = benchmark_script.fetch_top_anime_plan(
        "https://example.invalid/graphql",
        limit=1,
        sort="score",
        sample_episodes=3,
        seed=123,
    )
    _Client.pages = [
        {
            "data": {
                "Page": {
                    "pageInfo": {"hasNextPage": False},
                    "media": [_media(10, "Ten", 10)],
                }
            }
        }
    ]
    second = benchmark_script.fetch_top_anime_plan(
        "https://example.invalid/graphql",
        limit=1,
        sort="score",
        sample_episodes=3,
        seed=123,
    )
    assert first["episodes"] == second["episodes"]
    assert len(first["episodes"]) == 3


def test_video_file_choice_prefers_requested_episode() -> None:
    files = [
        {"index": 0, "name": "Series - 01.mkv", "size": 500_000_000},
        {"index": 1, "name": "Series - 02.mkv", "size": 450_000_000},
        {"index": 2, "name": "cover.jpg", "size": 100_000},
    ]
    choice = benchmark_script._video_file_choice(files, 2)
    assert choice is not None
    index, row = choice
    assert index == 1
    assert row["name"].endswith("02.mkv")


def test_aggregate_counts_missing_candidates_as_candidate_miss(monkeypatch, tmp_path) -> None:
    cases = [
        {"candidates": [], "current_pudge": None},
        {
            "candidates": [{"oracle": {"same_episode": False, "evaluable": True}}],
            "current_pudge": {"false_accept": False, "false_reject": False},
        },
        {
            "candidates": [
                {
                    "oracle": {
                        "same_episode": True,
                        "evaluable": True,
                        "good_alignment": True,
                        "content_match_score": 1.0,
                        "candidate_coverage": 1.0,
                        "oracle_coverage": 1.0,
                        "p90_abs_error_seconds": 0.0,
                        "p99_abs_error_seconds": 0.0,
                        "within_1s_ratio": 1.0,
                        "within_2s_ratio": 1.0,
                    }
                }
            ],
            "current_pudge": {
                "false_accept": False,
                "false_reject": False,
                "selection_failure": False,
                "alignment_failure": False,
            },
        },
    ]
    monkeypatch.setattr(benchmark_script, "collect_case_reports", lambda _corpus: cases)

    summary = benchmark_script._aggregate_corpus(tmp_path)

    assert summary["cases"] == 3
    assert summary["cases_with_same_episode_candidate"] == 1
    assert summary["candidate_misses"] == 2
    assert summary["candidate_miss_ratio"] == 0.6667


def test_top_plan_retries_429_and_caches_page(monkeypatch, tmp_path) -> None:
    class Response:
        def __init__(self, status_code: int, payload: dict[str, Any], headers=None) -> None:
            self.status_code = status_code
            self._payload = payload
            self.headers = dict(headers or {})

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                request = benchmark_script.httpx.Request("POST", "https://example.invalid/graphql")
                response = benchmark_script.httpx.Response(self.status_code, request=request)
                raise benchmark_script.httpx.HTTPStatusError("boom", request=request, response=response)

        def json(self) -> dict[str, Any]:
            return self._payload

    class Client:
        calls = 0

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def post(self, *_args: Any, **_kwargs: Any):
            type(self).calls += 1
            if type(self).calls == 1:
                return Response(429, {}, {"Retry-After": "0"})
            return Response(
                200,
                {
                    "data": {
                        "Page": {
                            "pageInfo": {"hasNextPage": False},
                            "media": [_media(7, "Seven", 1)],
                        }
                    }
                },
            )

    sleeps: list[float] = []
    monkeypatch.setattr(benchmark_script.httpx, "Client", Client)
    monkeypatch.setattr(benchmark_script.time, "sleep", sleeps.append)

    payload = benchmark_script.fetch_top_anime_plan(
        "https://example.invalid/graphql",
        limit=1,
        sort="popularity",
        sample_episodes=0,
        seed=1,
        cache_dir=tmp_path,
        max_retries=2,
        retry_base_seconds=0.1,
    )

    assert payload["anime_count"] == 1
    assert Client.calls == 2
    assert sleeps == [0.1]
    cache = tmp_path / "popularity_desc-page-0001.json"
    assert cache.is_file()
    assert json.loads(cache.read_text(encoding="utf-8"))["data"]["Page"]["media"][0]["id"] == 7


def test_top_plan_uses_persistent_cache_without_network(monkeypatch, tmp_path) -> None:
    cache = tmp_path / "popularity_desc-page-0001.json"
    cache.write_text(
        json.dumps(
            {
                "data": {
                    "Page": {
                        "pageInfo": {"hasNextPage": False},
                        "media": [_media(11, "Cached", 2)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    class NoNetwork(_Client):
        def post(self, *_args: Any, **_kwargs: Any) -> _Response:
            raise AssertionError("network should not be used when page cache exists")

    monkeypatch.setattr(benchmark_script.httpx, "Client", NoNetwork)
    payload = benchmark_script.fetch_top_anime_plan(
        "https://example.invalid/graphql",
        limit=1,
        sort="popularity",
        sample_episodes=0,
        seed=1,
        cache_dir=tmp_path,
    )
    assert payload["anime_count"] == 1
    assert payload["episode_count"] == 2


def _download_config(*, aria_enabled: bool, qbit_enabled: bool):
    from types import SimpleNamespace

    return SimpleNamespace(
        aria2=SimpleNamespace(enabled=aria_enabled),
        qbittorrent=SimpleNamespace(enabled=qbit_enabled),
    )


def test_download_client_auto_prefers_aria2(monkeypatch) -> None:
    calls: list[str] = []

    class Aria:
        def ensure_running(self) -> None:
            calls.append("aria2")

        def close(self) -> None:
            calls.append("aria2-close")

    monkeypatch.setattr(benchmark_script, "_aria2_client", lambda _config: Aria())
    monkeypatch.setattr(
        benchmark_script,
        "_qbit_client",
        lambda _config: (_ for _ in ()).throw(AssertionError("qBittorrent should not be created")),
    )

    name, _client = benchmark_script._download_client(
        _download_config(aria_enabled=True, qbit_enabled=True),
        "auto",
    )
    assert name == "aria2"
    assert calls == ["aria2"]


def test_download_client_auto_falls_back_to_qbittorrent(monkeypatch) -> None:
    calls: list[str] = []

    class Aria:
        def ensure_running(self) -> None:
            raise benchmark_script.Aria2Error("offline")

        def close(self) -> None:
            calls.append("aria2-close")

    class Qbit:
        def login(self) -> None:
            calls.append("qbit")

        def close(self) -> None:
            calls.append("qbit-close")

    monkeypatch.setattr(benchmark_script, "_aria2_client", lambda _config: Aria())
    monkeypatch.setattr(benchmark_script, "_qbit_client", lambda _config: Qbit())

    name, _client = benchmark_script._download_client(
        _download_config(aria_enabled=True, qbit_enabled=True),
        "auto",
    )
    assert name == "qbittorrent"
    assert calls == ["aria2-close", "qbit"]


def test_download_client_explicit_aria2_requires_enabled_config() -> None:
    try:
        benchmark_script._download_client(
            _download_config(aria_enabled=False, qbit_enabled=True),
            "aria2",
        )
    except RuntimeError as exc:
        assert "aria2 backend must be configured/enabled" in str(exc)
    else:
        raise AssertionError("explicit aria2 must not silently switch backend")


def test_benchmark_resolution_720_rejects_1080_and_unknown() -> None:
    from types import SimpleNamespace

    releases = [
        SimpleNamespace(title="Show - 01 [1080p]"),
        SimpleNamespace(title="Show - 01 [720p]"),
        SimpleNamespace(title="Show - 01 [480p]"),
        SimpleNamespace(title="Show - 01 WEB-DL"),
    ]

    filtered = benchmark_script._filter_benchmark_resolution(releases, "720p")

    assert [row.title for row in filtered] == [
        "Show - 01 [720p]",
        "Show - 01 [480p]",
    ]


def test_benchmark_resolution_any_keeps_everything() -> None:
    from types import SimpleNamespace

    releases = [
        SimpleNamespace(title="Show - 01 [2160p]"),
        SimpleNamespace(title="Show - 01 WEB-DL"),
    ]
    assert benchmark_script._filter_benchmark_resolution(releases, "any") == releases


def test_wait_for_download_reports_and_aborts_zero_speed_stall(monkeypatch, capsys) -> None:
    now = [100.0]

    class Client:
        def files(self, _torrent_hash: str):
            return [{"index": 0, "name": "episode.mkv", "size": 100 * 1024 * 1024, "progress": 0.0}]

        def torrent_status(self, _torrent_hash: str):
            return {
                "dlspeed": 0,
                "connections": 0,
                "num_seeders": 0,
            }

    monkeypatch.setattr(benchmark_script.time, "monotonic", lambda: now[0])

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(benchmark_script.time, "sleep", sleep)

    try:
        benchmark_script._wait_for_download(
            Client(),
            "hash",
            0,
            timeout_seconds=600,
            stall_timeout_seconds=15,
            progress_interval_seconds=2,
        )
    except benchmark_script.QBittorrentError as exc:
        assert "stalled for 15s" in str(exc)
    else:
        raise AssertionError("zero-speed benchmark torrent must be abandoned")

    output = capsys.readouterr().out
    assert "download   0.0%" in output
    assert "0.0 B/s" in output
    assert "peers=0 seeders=0" in output


def test_wait_for_download_resets_stall_clock_when_target_progress_moves(monkeypatch) -> None:
    now = [100.0]
    progresses = iter([0.0, 0.10, 0.10, 1.0])
    current = [0.0]

    class Client:
        def files(self, _torrent_hash: str):
            try:
                current[0] = next(progresses)
            except StopIteration:
                pass
            return [{"index": 0, "name": "episode.mkv", "size": 1000, "progress": current[0]}]

        def torrent_status(self, _torrent_hash: str):
            return {"dlspeed": 100, "connections": 1, "num_seeders": 1}

    monkeypatch.setattr(benchmark_script.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(benchmark_script.time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))

    row = benchmark_script._wait_for_download(
        Client(),
        "hash",
        0,
        timeout_seconds=600,
        stall_timeout_seconds=15,
        progress_interval_seconds=2,
    )
    assert row["progress"] == 1.0


def test_video_file_choice_requires_exact_episode_in_multifile_pack() -> None:
    files = [
        {"index": 0, "name": "Show - 01.mkv", "size": 1000},
        {"index": 1, "name": "Show - 03.mkv", "size": 1000},
    ]
    assert benchmark_script._video_file_choice(files, 2) is None
    choice = benchmark_script._video_file_choice(files, 3)
    assert choice is not None
    assert choice[0] == 1


def test_source_special_mismatch_rejects_tv_special_but_allows_ova() -> None:
    assert benchmark_script._source_special_mismatch(
        {"format": "TV"},
        "Chuunibyou demo Koi ga Shitai! Ren - 03 - Special [720p]",
    )
    assert not benchmark_script._source_special_mismatch(
        {"format": "OVA"},
        "Kiss x Sis OVA - 08 [480p]",
    )


def test_benchmark_source_sort_prefers_live_pack() -> None:
    from types import SimpleNamespace

    dead_single = SimpleNamespace(
        title="Show - 02 [720p]", seeders=0, is_batch=False, score=100.0, size_bytes=300_000_000
    )
    live_pack = SimpleNamespace(
        title="Show Complete [720p]", seeders=8, is_batch=True, score=10.0, size_bytes=8_000_000_000
    )
    assert benchmark_script._benchmark_source_sort_key(live_pack) > benchmark_script._benchmark_source_sort_key(dead_single)


def test_download_release_resumes_benchmark_owned_existing(tmp_path) -> None:
    from types import SimpleNamespace

    root = tmp_path / "downloads"
    root.mkdir()
    release = SimpleNamespace(info_hash="ABCDEF", title="Show - 02 [720p]", torrent_url="", link="")

    class Client:
        def __init__(self) -> None:
            self.deleted: list[tuple[str, bool]] = []
            self.started = False
            self.added = False

        def torrents(self):
            return [SimpleNamespace(torrent_hash="abcdef", save_path=str(root))]

        def delete(self, torrent_hash: str, *, delete_files: bool = True) -> None:
            self.deleted.append((torrent_hash, delete_files))

        def add_release(self, *_args, **_kwargs):
            self.added = True
            return "abcdef"

        def files(self, _torrent_hash: str):
            progress = 1.0 if self.started else 0.4
            path = root / "Show - 02.mkv"
            if self.started:
                path.write_bytes(b"ok")
            return [{"index": 0, "name": path.name, "size": 2, "progress": progress, "priority": 1}]

        def set_file_priority(self, *_args, **_kwargs):
            return None

        def start(self, _torrent_hash: str):
            self.started = True

        def torrent_status(self, _torrent_hash: str):
            return {"save_path": str(root), "dlspeed": 1, "connections": 1, "num_seeders": 1}

    client = Client()
    torrent_hash, path = benchmark_script._download_release_video(
        client,
        release,
        episode=2,
        root=root,
        metadata_timeout_seconds=1,
        download_timeout_seconds=10,
        max_target_bytes=100,
        stall_timeout_seconds=15,
        progress_interval_seconds=2,
    )
    assert torrent_hash == "abcdef"
    assert path.is_file()
    assert not client.added
    assert client.deleted == []


def test_download_release_rejects_pack_episode_over_size_limit(tmp_path) -> None:
    from types import SimpleNamespace

    root = tmp_path / "downloads"
    root.mkdir()
    release = SimpleNamespace(info_hash="abcdef", title="Show Complete [720p]", torrent_url="", link="")

    class Client:
        def torrents(self):
            return []

        def add_release(self, *_args, **_kwargs):
            return "abcdef"

        def files(self, _torrent_hash: str):
            return [
                {"index": 0, "name": "Show - 01.mkv", "size": 10, "progress": 0.0, "priority": 1},
                {"index": 1, "name": "Show - 02.mkv", "size": 500, "progress": 0.0, "priority": 1},
            ]

        def delete(self, *_args, **_kwargs):
            return None

    try:
        benchmark_script._download_release_video(
            Client(),
            release,
            episode=2,
            root=root,
            metadata_timeout_seconds=1,
            download_timeout_seconds=10,
            max_target_bytes=100,
        )
    except benchmark_script.QBittorrentError as exc:
        assert "selected episode is too large" in str(exc)
    else:
        raise AssertionError("pack target file must honor max-download-gb")


def test_nyaa_source_search_combines_single_and_batch_and_filters_dead(monkeypatch) -> None:
    from types import SimpleNamespace

    def release(title: str, *, seeders: int, batch: bool, score: float, size: int):
        return SimpleNamespace(
            title=title,
            info_hash=title,
            torrent_url="",
            link="",
            seeders=seeders,
            is_batch=batch,
            score=score,
            size_bytes=size,
        )

    dead = release("Show - 02 [720p]", seeders=0, batch=False, score=200.0, size=200)
    single = release("Show - 02 [720p]", seeders=2, batch=False, score=20.0, size=200)
    pack = release("Show Complete [720p]", seeders=9, batch=True, score=5.0, size=20_000)
    calls: list[bool] = []

    def fake_search_ranked(_client, _anime, *, batch: bool, **_kwargs):
        calls.append(batch)
        return [pack] if batch else [dead, single]

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self):
            pass

    monkeypatch.setattr(benchmark_script, "NyaaClient", Client)
    monkeypatch.setattr(benchmark_script, "search_ranked", fake_search_ranked)
    config = SimpleNamespace(
        nyaa=SimpleNamespace(
            base_url="https://example.invalid",
            proxy_mode="direct",
            proxy_url="",
            pre_search_command="",
            category="1_2",
            trusted_groups=[],
            preferred_groups=[],
            blocked_groups=[],
            min_seeders=1,
            episode_min_size_mb=1,
            episode_max_size_mb=3500,
            preferred_video_codecs=["AVC"],
            preferred_sources=["WEB-DL"],
            require_japanese_audio=True,
            avoid_upscaled=True,
        )
    )
    rows = benchmark_script._nyaa_ranked_for_plan(
        {
            "media_id": 1,
            "title": "Show",
            "titles": [],
            "synonyms": [],
            "episodes": 12,
            "format": "TV",
            "season_year": 2014,
            "episode": 2,
        },
        config,
        resolution="720p",
        min_source_seeders=1,
    )
    assert calls == [False, True]
    assert dead not in rows
    assert pack in rows
    assert rows[0] is pack


def test_source_affinity_distinguishes_netflix_from_plain_erai() -> None:
    assert benchmark_script._benchmark_source_affinity(
        "[Group] Show S01E03 NF WEB-DL [720p]"
    ) == (4, "netflix")
    assert benchmark_script._benchmark_source_affinity(
        "[Erai-raws] Show - 03 [720p][Multiple Subtitle]"
    ) == (1, "erai-multisub-no-jp")
    assert benchmark_script._benchmark_source_affinity(
        "[Erai-raws] Show - 03 [720p]"
    ) == (0, "generic")


def test_benchmark_source_sort_prefers_480_within_same_source_tier() -> None:
    from types import SimpleNamespace

    common = dict(seeders=5, is_batch=False, score=20.0, size_bytes=200_000_000)
    low = SimpleNamespace(title="Show - 03 [480p]", **common)
    high = SimpleNamespace(title="Show - 03 [720p]", **common)
    assert benchmark_script._benchmark_source_sort_key(low) > benchmark_script._benchmark_source_sort_key(high)


def test_benchmark_source_sort_prefers_explicit_netflix_over_generic_480() -> None:
    from types import SimpleNamespace

    netflix = SimpleNamespace(
        title="Show - 03 [720p] NF WEB-DL",
        seeders=1,
        is_batch=False,
        score=5.0,
        size_bytes=300_000_000,
    )
    generic = SimpleNamespace(
        title="Show - 03 [480p]",
        seeders=20,
        is_batch=False,
        score=100.0,
        size_bytes=150_000_000,
    )
    assert benchmark_script._benchmark_source_sort_key(netflix) > benchmark_script._benchmark_source_sort_key(generic)


def test_benchmark_source_queries_explicitly_probe_netflix_nf_and_erai() -> None:
    queries = benchmark_script._benchmark_source_queries(
        {"title": "Show", "titles": ["Show English"], "synonyms": []}
    )
    assert "Show Netflix" in queries
    assert "Show NF WEB-DL" in queries
    assert "Show Erai-raws" in queries


def test_video_file_choices_selects_multiple_exact_pack_episodes() -> None:
    files = [
        {"index": 0, "name": "Show - 01.mkv", "size": 1000},
        {"index": 1, "name": "Show - 03.mkv", "size": 1000},
        {"index": 2, "name": "Show - 08.mkv", "size": 1000},
        {"index": 3, "name": "cover.jpg", "size": 10},
    ]
    choices = benchmark_script._video_file_choices(files, [3, 8])
    assert set(choices) == {3, 8}
    assert choices[3][0] == 1
    assert choices[8][0] == 2


def test_download_release_videos_selects_multiple_pack_files(tmp_path) -> None:
    from types import SimpleNamespace

    root = tmp_path / "downloads"
    root.mkdir()
    release = SimpleNamespace(
        info_hash="abcdef",
        title="Show 01-12 [480p]",
        torrent_url="",
        link="",
        is_batch=True,
    )

    class Client:
        def __init__(self) -> None:
            self.started = False
            self.priorities: list[tuple[tuple[int, ...], int]] = []

        def torrents(self):
            return []

        def add_release(self, *_args, **_kwargs):
            return "abcdef"

        def files(self, _torrent_hash: str):
            progress = 1.0 if self.started else 0.0
            rows = [
                {"index": 0, "name": "Show - 01.mkv", "size": 10, "progress": 0.0},
                {"index": 1, "name": "Show - 03.mkv", "size": 10, "progress": progress},
                {"index": 2, "name": "Show - 08.mkv", "size": 10, "progress": progress},
            ]
            if self.started:
                (root / "Show - 03.mkv").write_bytes(b"three")
                (root / "Show - 08.mkv").write_bytes(b"eight")
            return rows

        def set_file_priority(self, _torrent_hash: str, indexes, priority: int):
            self.priorities.append((tuple(indexes), priority))

        def start(self, _torrent_hash: str):
            self.started = True

        def torrent_status(self, _torrent_hash: str):
            return {"save_path": str(root), "dlspeed": 10, "connections": 1, "num_seeders": 1}

        def delete(self, *_args, **_kwargs):
            return None

    client = Client()
    torrent_hash, paths = benchmark_script._download_release_videos(
        client,
        release,
        episodes=[3, 8],
        root=root,
        metadata_timeout_seconds=1,
        download_timeout_seconds=10,
        max_target_bytes=100,
    )
    assert torrent_hash == "abcdef"
    assert set(paths) == {3, 8}
    assert paths[3].name == "Show - 03.mkv"
    assert paths[8].name == "Show - 08.mkv"
    assert ((0,), 0) in client.priorities
    assert ((1, 2), 1) in client.priorities


def test_acquire_plan_batches_same_anime_rows_into_one_pack_download(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    rows = [
        {"media_id": 7, "title": "Show", "episode": 2, "rank": 1},
        {"media_id": 7, "title": "Show", "episode": 5, "rank": 1},
        {"media_id": 7, "title": "Show", "episode": 9, "rank": 1},
    ]
    release = SimpleNamespace(
        info_hash="pack",
        title="Show 01-12 [480p]",
        torrent_url="",
        link="",
        is_batch=True,
        score=100.0,
        seeders=10,
        size_bytes=10_000,
        size_text="10 GiB",
    )
    config = SimpleNamespace(
        jimaku=SimpleNamespace(api_key="key"),
        nyaa=SimpleNamespace(min_seeders=1),
    )

    class Downloader:
        def delete(self, *_args, **_kwargs):
            return None

        def close(self):
            return None

    requested: list[list[int]] = []
    created: list[int] = []

    monkeypatch.setattr(benchmark_script, "load_config", lambda _path: config)
    monkeypatch.setattr(benchmark_script, "_completed_episode_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_read_plan", lambda _path: rows)
    monkeypatch.setattr(benchmark_script, "_download_client", lambda _config, _backend: ("aria2", Downloader()))
    monkeypatch.setattr(benchmark_script, "_nyaa_ranked_for_plan", lambda *_args, **_kwargs: [release])

    def fake_download(_client, _release, *, episodes, root, **_kwargs):
        episode_list = list(episodes)
        requested.append(episode_list)
        paths = {}
        for episode in episode_list:
            path = root / f"Show - {episode:02d}.mkv"
            path.write_bytes(b"x")
            paths[episode] = path
        return "pack", paths

    def fake_case(*, episode: int, **_kwargs):
        created.append(episode)
        path = tmp_path / f"case-{episode}"
        path.mkdir(exist_ok=True)
        return path

    monkeypatch.setattr(benchmark_script, "_download_release_videos", fake_download)
    monkeypatch.setattr(benchmark_script, "create_case_from_video", fake_case)
    monkeypatch.setattr(benchmark_script, "_aggregate_corpus", lambda _corpus: {"cases": len(created)})
    monkeypatch.setattr(benchmark_script, "write_json", lambda *_args, **_kwargs: None)

    args = SimpleNamespace(
        config=None,
        corpus=tmp_path / "corpus",
        plan=tmp_path / "plan.json",
        start_index=0,
        limit=3,
        backend="aria2",
        episode_batch_size=3,
        max_download_gb=2.0,
        resolution="720p",
        min_source_seeders=1,
        max_release_attempts=8,
        metadata_timeout_seconds=1.0,
        download_timeout_minutes=1.0,
        stall_timeout_seconds=15.0,
        progress_interval_seconds=2.0,
    )

    assert benchmark_script.acquire_plan(args) == 0
    assert requested == [[2, 5, 9]]
    assert created == [2, 5, 9]


def test_top_plan_sample_episodes_is_per_anime(monkeypatch) -> None:
    _Client.pages = [
        {
            "data": {
                "Page": {
                    "pageInfo": {"hasNextPage": False},
                    "media": [_media(21, "A", 8), _media(22, "B", 6)],
                }
            }
        }
    ]
    monkeypatch.setattr(benchmark_script.httpx, "Client", _Client)
    payload = benchmark_script.fetch_top_anime_plan(
        "https://example.invalid/graphql",
        limit=2,
        sort="popularity",
        sample_episodes=3,
        seed=99,
    )
    rows = payload["episodes"]
    assert payload["episode_count"] == 6
    assert sum(int(row["media_id"]) == 21 for row in rows) == 3
    assert sum(int(row["media_id"]) == 22 for row in rows) == 3


def test_source_affinity_prioritizes_explicit_jpn_tag_example() -> None:
    title = (
        "[Erai-raws] The Fable - 15 [720p][HEVC][Multiple Subtitle]"
        "[ENG][POR-BR][SPA][FRE][GER][JPN][KOR]"
    )
    assert benchmark_script._benchmark_source_affinity(title) == (8, "jp-tag")


def test_benchmark_source_queries_probe_jp_tags_before_netflix() -> None:
    queries = benchmark_script._benchmark_source_queries(
        {"title": "Show", "titles": [], "synonyms": []}
    )
    assert queries[:4] == [
        "Show JPN",
        "Show JP",
        "Show Japanese Subtitle",
        "Show Multiple Subtitle JPN",
    ]
    assert queries.index("Show JPN") < queries.index("Show Netflix")


def test_diagnostic_release_ledger_skips_same_no_oracle_source(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(
        benchmark_script,
        "collect_diagnostic_reports",
        lambda _corpus: [
            {
                "media_id": 7,
                "episode": 2,
                "source_release_name": "Show - 02 [720p].mkv",
                "source_release": {"info_hash": "ABC"},
            }
        ],
    )
    keys = benchmark_script._diagnostic_release_keys(tmp_path)
    release = SimpleNamespace(info_hash="abc", title="Show - 02 [720p]", torrent_url="")
    assert benchmark_script._release_diagnostic_key(
        {"media_id": 7, "episode": 2}, release
    ) in keys


def test_acquire_limit_counts_gold_not_plan_rows(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    rows = [
        {"media_id": 1, "title": "A", "episode": 1, "rank": 1},
        {"media_id": 2, "title": "B", "episode": 1, "rank": 2},
        {"media_id": 3, "title": "C", "episode": 1, "rank": 3},
    ]
    releases = {
        1: SimpleNamespace(info_hash="a", title="A - 01 [480p]", torrent_url="", link="", is_batch=False, score=1.0, seeders=1, size_bytes=10, size_text="10 B"),
        2: SimpleNamespace(info_hash="b", title="B - 01 [480p]", torrent_url="", link="", is_batch=False, score=1.0, seeders=1, size_bytes=10, size_text="10 B"),
        3: SimpleNamespace(info_hash="c", title="C - 01 [480p]", torrent_url="", link="", is_batch=False, score=1.0, seeders=1, size_bytes=10, size_text="10 B"),
    }
    config = SimpleNamespace(jimaku=SimpleNamespace(api_key="key"), nyaa=SimpleNamespace(min_seeders=1))

    class Downloader:
        def delete(self, *_args, **_kwargs):
            return None
        def close(self):
            return None

    monkeypatch.setattr(benchmark_script, "load_config", lambda _path: config)
    monkeypatch.setattr(benchmark_script, "_completed_episode_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_diagnostic_release_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_read_plan", lambda _path: rows)
    monkeypatch.setattr(benchmark_script, "_download_client", lambda *_args: ("aria2", Downloader()))
    monkeypatch.setattr(
        benchmark_script,
        "_nyaa_ranked_for_plan",
        lambda row, *_args, **_kwargs: [releases[int(row["media_id"])]],
    )

    def fake_download(_client, _release, *, episodes, root, **_kwargs):
        episode = list(episodes)[0]
        path = root / f"episode-{episode}.mkv"
        path.write_bytes(b"x")
        return str(_release.info_hash), {episode: path}

    attempts: list[int] = []
    def fake_case(*, media_id: int, **_kwargs):
        attempts.append(media_id)
        if media_id == 1:
            raise benchmark_script.SubtitleBenchmarkError("video has no embedded Japanese text subtitle")
        path = tmp_path / f"gold-{media_id}"
        path.mkdir(exist_ok=True)
        return path

    monkeypatch.setattr(benchmark_script, "_download_release_videos", fake_download)
    monkeypatch.setattr(benchmark_script, "create_case_from_video", fake_case)
    monkeypatch.setattr(benchmark_script, "create_diagnostic_case_from_video", lambda **_kwargs: tmp_path / "diag")
    monkeypatch.setattr(benchmark_script, "_aggregate_corpus", lambda _corpus: {"cases": 1})
    monkeypatch.setattr(benchmark_script, "write_json", lambda *_args, **_kwargs: None)

    args = SimpleNamespace(
        config=None, corpus=tmp_path / "corpus", plan=tmp_path / "plan.json",
        start_index=0, limit=1, backend="aria2", episode_batch_size=1,
        max_download_gb=2.0, resolution="720p", min_source_seeders=1,
        max_release_attempts=8, metadata_timeout_seconds=1.0,
        download_timeout_minutes=1.0, stall_timeout_seconds=15.0,
        progress_interval_seconds=2.0,
    )
    assert benchmark_script.acquire_plan(args) == 0
    assert attempts == [1, 2]


def test_nyaa_source_first_short_circuits_generic_search_on_live_jpn(monkeypatch) -> None:
    from types import SimpleNamespace

    release = SimpleNamespace(
        title="[Erai-raws] Show - 02 [480p][Multiple Subtitle][ENG][JPN]",
        info_hash="jpn",
        torrent_url="",
        link="",
        seeders=4,
        is_batch=False,
        score=0.0,
        size_bytes=100_000_000,
    )

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass
        def search(self, query: str):
            return [release] if query.endswith("JPN") else []
        def close(self):
            return None

    monkeypatch.setattr(benchmark_script, "NyaaClient", Client)
    monkeypatch.setattr(
        benchmark_script,
        "search_ranked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("generic search must not run when explicit JPN source is live")
        ),
    )
    monkeypatch.setattr(benchmark_script, "score_release", lambda release, *_args, **_kwargs: release)
    config = SimpleNamespace(
        nyaa=SimpleNamespace(
            base_url="https://example.invalid",
            proxy_mode="direct",
            proxy_url="",
            pre_search_command="",
            category="1_2",
            trusted_groups=[],
            preferred_groups=[],
            blocked_groups=[],
            min_seeders=1,
            episode_min_size_mb=1,
            episode_max_size_mb=3500,
            preferred_video_codecs=["AVC"],
            preferred_sources=["WEB-DL"],
            require_japanese_audio=True,
            avoid_upscaled=True,
        )
    )
    rows = benchmark_script._nyaa_ranked_for_plan(
        {
            "media_id": 1,
            "title": "Show",
            "titles": [],
            "synonyms": [],
            "episodes": 12,
            "format": "TV",
            "season_year": 2024,
            "episode": 2,
        },
        config,
        resolution="720p",
        min_source_seeders=1,
    )
    assert rows == [release]



def test_source_identity_rejects_real_aot_final_season_regression() -> None:
    row = {
        "media_id": 16498,
        "title": "Shingeki no Kyojin",
        "titles": ["Shingeki no Kyojin", "Attack on Titan", "進撃の巨人"],
        "synonyms": ["SnK", "AoT"],
        "relations": [],
        "episode": 2,
    }
    source = (
        "[Erai-raws] Shingeki no Kyojin - The Final Season Part 2 - 01 "
        "[480p][Multiple Subtitle][F8326E36].mkv"
    )
    assert benchmark_script._source_identity_mismatch_reason(row, source) == "cross_season_source_mismatch"


def test_source_identity_allows_first_season_marker_for_relation_confirmed_first_season() -> None:
    row = {
        "title": "Attack on Titan",
        "titles": ["Shingeki no Kyojin"],
        "relations": [],
    }
    assert benchmark_script._source_identity_mismatch_reason(
        row, "[Group] Attack on Titan Season 1 - 02 [480p].mkv"
    ) is None


def test_source_identity_rejects_conflicting_explicit_season_but_allows_matching_season() -> None:
    row = {
        "title": "Example Second Season",
        "titles": ["Example Season 2"],
        "relations": [{"relation_type": "PREQUEL", "media_id": 1}],
    }
    assert benchmark_script._source_identity_mismatch_reason(
        row, "[Group] Example Season 2 - 03 [480p].mkv"
    ) is None
    assert benchmark_script._source_identity_mismatch_reason(
        row, "[Group] Example Season 3 - 03 [480p].mkv"
    ) == "cross_season_source_mismatch"


def test_source_identity_does_not_treat_episode_after_ordinal_season_as_another_season() -> None:
    row = {
        "title": "Ranma 1/2 (2024) 2nd Season",
        "titles": ["Ranma 1/2 (2024) 2nd Season"],
        "relations": [],
    }
    source = (
        "[Erai-raws] Ranma 1/2 (2024) 2nd Season - 12 "
        "[720p NF WEB-DL AVC AAC][MultiSub][EF24474B]"
    )
    assert benchmark_script._source_identity_mismatch_reason(row, source) is None


def test_nyaa_source_first_filters_wrong_season_before_high_confidence_short_circuit(monkeypatch) -> None:
    from types import SimpleNamespace

    wrong = SimpleNamespace(
        title="[Erai-raws] Shingeki no Kyojin - The Final Season Part 2 - 01 [480p][Multiple Subtitle][JPN]",
        info_hash="wrong",
        torrent_url="",
        link="",
        seeders=10,
        is_batch=False,
        score=0.0,
        size_bytes=100_000_000,
    )
    correct = SimpleNamespace(
        title="[Group] Shingeki no Kyojin - 02 [480p]",
        info_hash="correct",
        torrent_url="",
        link="",
        seeders=4,
        is_batch=False,
        score=50.0,
        size_bytes=100_000_000,
    )

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass
        def search(self, query: str):
            return [wrong] if query.endswith("JPN") else []
        def close(self):
            return None

    monkeypatch.setattr(benchmark_script, "NyaaClient", Client)
    monkeypatch.setattr(benchmark_script, "score_release", lambda release, *_args, **_kwargs: release)
    calls: list[bool] = []
    def generic(*_args, batch: bool, **_kwargs):
        calls.append(batch)
        return [correct] if not batch else []
    monkeypatch.setattr(benchmark_script, "search_ranked", generic)
    config = SimpleNamespace(
        nyaa=SimpleNamespace(
            base_url="https://example.invalid", proxy_mode="direct", proxy_url="",
            pre_search_command="", category="1_2", trusted_groups=[], preferred_groups=[],
            blocked_groups=[], min_seeders=1, episode_min_size_mb=1, episode_max_size_mb=3500,
            preferred_video_codecs=["AVC"], preferred_sources=["WEB-DL"],
            require_japanese_audio=True, avoid_upscaled=True,
        )
    )
    rows = benchmark_script._nyaa_ranked_for_plan(
        {
            "media_id": 16498, "title": "Shingeki no Kyojin",
            "titles": ["Attack on Titan"], "synonyms": [], "episodes": 25,
            "format": "TV", "season_year": 2013, "episode": 2, "relations": [],
        },
        config,
        resolution="720p",
        min_source_seeders=1,
    )
    assert [row.info_hash for row in rows] == ["correct"]
    assert calls == [False, True]


def test_aggregate_reclassifies_old_wrong_media_false_accept_as_adversarial(monkeypatch, tmp_path) -> None:
    diagnostic = {
        "schema": "pudge-subtitle-benchmark-diagnostic-v1",
        "tier": "diagnostic",
        "media_id": 16498,
        "title": "Shingeki no Kyojin",
        "episode": 2,
        "reason": "no_embedded_japanese_text_subtitle",
        "source_release_name": (
            "[Erai-raws] Shingeki no Kyojin - The Final Season Part 2 - 01 "
            "[480p][Multiple Subtitle][F8326E36].mkv"
        ),
        "source_release": {
            "title": "[Erai-raws] Shingeki no Kyojin - The Final Season Part 2 - 01 [480p][Multiple Subtitle]"
        },
        "current_pudge": {
            "accepted": True,
            "selected": {"name": "進撃の巨人.S01E02.WEBRip.Netflix.ja[cc].srt"},
        },
    }
    monkeypatch.setattr(benchmark_script, "collect_case_reports", lambda _corpus: [])
    monkeypatch.setattr(benchmark_script, "collect_diagnostic_reports", lambda _corpus: [diagnostic])
    summary = benchmark_script._aggregate_corpus(tmp_path)
    assert summary["records_total"] == 1
    assert summary["tiers"] == {"gold": 0, "silver": 0, "diagnostic": 0, "adversarial": 1}
    assert summary["adversarial_reason_counts"]["cross_season_source_mismatch"] == 1
    assert summary["adversarial_reason_counts"]["false_accept_wrong_media"] == 1
    assert summary["adversarial_examples"][0]["media_id"] == 16498


def test_old_anilist_cache_without_relations_is_refetched(monkeypatch, tmp_path) -> None:
    stale = tmp_path / "popularity_desc-page-0001.json"
    old_media = _media(31, "Old Cached", 1)
    old_media.pop("relations")
    stale.write_text(
        json.dumps({"data": {"Page": {"pageInfo": {"hasNextPage": False}, "media": [old_media]}}}),
        encoding="utf-8",
    )
    fresh = _media(32, "Fresh", 1)
    _Client.pages = [
        {"data": {"Page": {"pageInfo": {"hasNextPage": False}, "media": [fresh]}}}
    ]
    monkeypatch.setattr(benchmark_script.httpx, "Client", _Client)
    payload = benchmark_script.fetch_top_anime_plan(
        "https://example.invalid/graphql",
        limit=1,
        sort="popularity",
        sample_episodes=0,
        seed=1,
        cache_dir=tmp_path,
    )
    assert payload["episodes"][0]["media_id"] == 32
    assert payload["episodes"][0]["relations"] == []


def test_acquire_refreshes_summary_on_keyboard_interrupt(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    row = {
        "media_id": 1, "title": "Show", "episode": 1, "rank": 1,
        "titles": [], "synonyms": [], "episodes": 12, "format": "TV",
        "season_year": 2024, "relations": [],
    }
    config = SimpleNamespace(jimaku=SimpleNamespace(api_key="key"), nyaa=SimpleNamespace(min_seeders=1))
    class Downloader:
        def close(self):
            return None
    writes: list[dict[str, object]] = []
    monkeypatch.setattr(benchmark_script, "load_config", lambda _path: config)
    monkeypatch.setattr(benchmark_script, "_completed_episode_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_diagnostic_release_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_read_plan", lambda _path: [row])
    monkeypatch.setattr(benchmark_script, "_download_client", lambda *_args: ("aria2", Downloader()))
    monkeypatch.setattr(
        benchmark_script,
        "_nyaa_ranked_for_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(
        benchmark_script,
        "_aggregate_corpus",
        lambda _corpus: {"schema": "summary", "tiers": {"gold": 0, "adversarial": 1}},
    )
    monkeypatch.setattr(benchmark_script, "write_json", lambda _path, payload: writes.append(payload))
    args = SimpleNamespace(
        config=None, corpus=tmp_path / "corpus", plan=tmp_path / "plan.json",
        start_index=0, limit=1, backend="aria2", episode_batch_size=1,
        max_download_gb=2.0, resolution="720p", min_source_seeders=1,
        max_release_attempts=8, metadata_timeout_seconds=1.0,
        download_timeout_minutes=1.0, stall_timeout_seconds=15.0,
        progress_interval_seconds=2.0,
    )
    try:
        benchmark_script.acquire_plan(args)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("KeyboardInterrupt should propagate")
    assert writes == [{"schema": "summary", "tiers": {"gold": 0, "adversarial": 1}}]



def test_source_affinity_downranks_erai_multisub_without_jpn_below_web_multisub() -> None:
    erai = "[Erai-raws] Show - 03 [720p][Multiple Subtitle][ENG][FRE]"
    web = "[Group] Show - 03 [720p][WEB-DL][MultiSub][ENG][FRE]"
    assert benchmark_script._benchmark_source_sort_key(
        SimpleNamespace(title=web, seeders=1, is_batch=False, score=0.0, size_bytes=100)
    ) > benchmark_script._benchmark_source_sort_key(
        SimpleNamespace(title=erai, seeders=20, is_batch=False, score=100.0, size_bytes=100)
    )
    assert not benchmark_script._benchmark_has_explicit_jp_signal(erai)
    assert benchmark_script._benchmark_has_explicit_jp_signal(
        "[Erai-raws] Show - 03 [720p][Multiple Subtitle][ENG][JPN]"
    )


def test_acquire_defaults_bound_slow_mass_benchmark_downloads() -> None:
    args = benchmark_script._parser().parse_args(
        ["acquire-plan", "--plan", "plan.json", "--corpus", "corpus"]
    )
    assert args.download_timeout_minutes == 30.0
    assert args.stall_timeout_seconds == 10.0


def test_download_stall_ignores_backend_speed_without_file_progress(monkeypatch) -> None:
    class Clock:
        now = 0.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, seconds):
            cls.now += float(seconds)

    class Client:
        def files(self, _torrent_hash):
            return [{"index": 0, "name": "Show.mkv", "size": 100, "progress": 0.0}]

        def torrent_status(self, _torrent_hash):
            return {"dlspeed": 128 * 1024, "connections": 3, "num_seeders": 1}

    monkeypatch.setattr(benchmark_script.time, "monotonic", Clock.monotonic)
    monkeypatch.setattr(benchmark_script.time, "sleep", Clock.sleep)
    try:
        benchmark_script._wait_for_download_many(
            Client(),
            "dead",
            [0],
            timeout_seconds=30 * 60,
            stall_timeout_seconds=15,
            progress_interval_seconds=2,
        )
    except benchmark_script.QBittorrentError as exc:
        assert "without selected-file progress" in str(exc)
        assert "backend aggregate reports 128.0 KiB/s" in str(exc)
    else:
        raise AssertionError("reported backend speed must not hide a zero-progress stall")


def test_download_rejects_slow_projected_completion_after_grace(monkeypatch) -> None:
    mib = 1024 * 1024

    class Clock:
        now = 0.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, seconds):
            cls.now += float(seconds)

    class Client:
        calls = 0

        def files(self, _torrent_hash):
            type(self).calls += 1
            completed = min(1024 * mib, type(self).calls * (mib // 2))
            return [
                {
                    "index": 0,
                    "name": "Slow pack episode.mkv",
                    "size": 1024 * mib,
                    "progress": completed / (1024 * mib),
                }
            ]

        def torrent_status(self, _torrent_hash):
            return {"dlspeed": 256 * 1024, "connections": 3, "num_seeders": 1}

    monkeypatch.setattr(benchmark_script.time, "monotonic", Clock.monotonic)
    monkeypatch.setattr(benchmark_script.time, "sleep", Clock.sleep)
    try:
        benchmark_script._wait_for_download_many(
            Client(),
            "slow",
            [0],
            timeout_seconds=30 * 60,
            stall_timeout_seconds=15,
            progress_interval_seconds=2,
        )
    except benchmark_script.QBittorrentError as exc:
        assert "download too slow" in str(exc)
        assert "projected ETA" in str(exc)
        assert "remaining timeout" in str(exc)
    else:
        raise AssertionError("multi-hour projected download must be rejected after grace")


def test_download_allows_rate_projected_inside_timeout(monkeypatch) -> None:
    mib = 1024 * 1024

    class Clock:
        now = 0.0

        @classmethod
        def monotonic(cls):
            return cls.now

        @classmethod
        def sleep(cls, seconds):
            cls.now += float(seconds)

    class Client:
        calls = 0

        def files(self, _torrent_hash):
            type(self).calls += 1
            completed = min(128 * mib, type(self).calls * 8 * mib)
            return [
                {
                    "index": 0,
                    "name": "Fast episode.mkv",
                    "size": 128 * mib,
                    "progress": completed / (128 * mib),
                }
            ]

        def torrent_status(self, _torrent_hash):
            return {"dlspeed": 4 * mib, "connections": 3, "num_seeders": 1}

    monkeypatch.setattr(benchmark_script.time, "monotonic", Clock.monotonic)
    monkeypatch.setattr(benchmark_script.time, "sleep", Clock.sleep)
    rows = benchmark_script._wait_for_download_many(
        Client(),
        "fast",
        [0],
        timeout_seconds=30 * 60,
        stall_timeout_seconds=15,
        progress_interval_seconds=2,
    )
    assert rows[0]["progress"] == 1.0


def test_download_pack_probes_smallest_episode_and_skips_rest_without_jp(monkeypatch, tmp_path) -> None:
    release = SimpleNamespace(
        info_hash="probe-no-jp",
        title="[Erai-raws] Show 01-12 [720p][Multiple Subtitle][ENG][FRE]",
        torrent_url="",
        link="",
        is_batch=True,
    )
    root = tmp_path / "downloads"
    root.mkdir()
    files = [
        {"index": 0, "name": "Show - 02.mkv", "size": 600},
        {"index": 1, "name": "Show - 11.mkv", "size": 300},
        {"index": 2, "name": "Show - 16.mkv", "size": 500},
    ]

    class Client:
        def __init__(self) -> None:
            self.priorities: list[tuple[tuple[int, ...], int]] = []
        def torrents(self):
            return []
        def add_release(self, *_args, **_kwargs):
            return "probe-no-jp"
        def set_file_priority(self, _hash, indexes, priority):
            self.priorities.append((tuple(indexes), priority))
        def start(self, _hash):
            return None
        def torrent_status(self, _hash):
            return {"save_path": str(root)}
        def delete(self, *_args, **_kwargs):
            return None

    calls: list[set[int]] = []
    monkeypatch.setattr(benchmark_script, "_wait_for_files", lambda *_args, **_kwargs: files)
    def wait_many(_client, _hash, indexes, **_kwargs):
        selected = set(int(value) for value in indexes)
        calls.append(selected)
        rows = {}
        for row in files:
            if int(row["index"]) not in selected:
                continue
            (root / str(row["name"])).write_bytes(b"video")
            rows[int(row["index"])] = dict(row, progress=1.0)
        return rows
    monkeypatch.setattr(benchmark_script, "_wait_for_download_many", wait_many)
    monkeypatch.setattr(benchmark_script, "_video_has_embedded_japanese_text", lambda *_args, **_kwargs: False)

    client = Client()
    torrent_hash, paths = benchmark_script._download_release_videos(
        client,
        release,
        episodes=[2, 11, 16],
        root=root,
        metadata_timeout_seconds=1,
        download_timeout_seconds=10,
        max_target_bytes=1000,
        probe_first=True,
        max_unverified_probe_bytes=700,
    )
    assert torrent_hash == "probe-no-jp"
    assert set(paths) == {11}
    assert calls == [{1}]
    assert ((0, 2), 0) in client.priorities
    assert ((1,), 1) in client.priorities


def test_download_pack_adds_remaining_episodes_after_jp_probe(monkeypatch, tmp_path) -> None:
    release = SimpleNamespace(
        info_hash="probe-jp",
        title="[Group] Show 01-12 [720p][MultiSub]",
        torrent_url="",
        link="",
        is_batch=True,
    )
    root = tmp_path / "downloads"
    root.mkdir()
    files = [
        {"index": 0, "name": "Show - 02.mkv", "size": 600},
        {"index": 1, "name": "Show - 11.mkv", "size": 300},
        {"index": 2, "name": "Show - 16.mkv", "size": 500},
    ]

    class Client:
        def __init__(self) -> None:
            self.priorities: list[tuple[tuple[int, ...], int]] = []
        def torrents(self):
            return []
        def add_release(self, *_args, **_kwargs):
            return "probe-jp"
        def set_file_priority(self, _hash, indexes, priority):
            self.priorities.append((tuple(indexes), priority))
        def start(self, _hash):
            return None
        def torrent_status(self, _hash):
            return {"save_path": str(root)}
        def delete(self, *_args, **_kwargs):
            return None

    calls: list[set[int]] = []
    monkeypatch.setattr(benchmark_script, "_wait_for_files", lambda *_args, **_kwargs: files)
    def wait_many(_client, _hash, indexes, **_kwargs):
        selected = set(int(value) for value in indexes)
        calls.append(selected)
        rows = {}
        for row in files:
            if int(row["index"]) not in selected:
                continue
            (root / str(row["name"])).write_bytes(b"video")
            rows[int(row["index"])] = dict(row, progress=1.0)
        return rows
    monkeypatch.setattr(benchmark_script, "_wait_for_download_many", wait_many)
    monkeypatch.setattr(benchmark_script, "_video_has_embedded_japanese_text", lambda *_args, **_kwargs: True)

    client = Client()
    _torrent_hash, paths = benchmark_script._download_release_videos(
        client,
        release,
        episodes=[2, 11, 16],
        root=root,
        metadata_timeout_seconds=1,
        download_timeout_seconds=10,
        max_target_bytes=1000,
        probe_first=True,
        max_unverified_probe_bytes=700,
    )
    assert set(paths) == {2, 11, 16}
    assert calls == [{1}, {0, 1, 2}]
    assert ((0, 2), 1) in client.priorities


def test_unverified_pack_size_guard_is_bypassed_for_resumable_benchmark_torrent(monkeypatch, tmp_path) -> None:
    release = SimpleNamespace(
        info_hash="resume-big",
        title="[Erai-raws] Show 01-12 [720p][Multiple Subtitle][ENG][FRE]",
        torrent_url="",
        link="",
        is_batch=True,
    )
    root = tmp_path / "downloads"
    root.mkdir()
    files = [{"index": 0, "name": "Show - 02.mkv", "size": 1000}]

    class Client:
        def torrents(self):
            return [SimpleNamespace(torrent_hash="resume-big", save_path=str(root))]
        def set_file_priority(self, *_args, **_kwargs):
            return None
        def start(self, *_args, **_kwargs):
            return None
        def torrent_status(self, _hash):
            return {"save_path": str(root)}
        def delete(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(benchmark_script, "_wait_for_files", lambda *_args, **_kwargs: files)
    def wait_many(_client, _hash, indexes, **_kwargs):
        (root / "Show - 02.mkv").write_bytes(b"video")
        return {0: dict(files[0], progress=1.0)}
    monkeypatch.setattr(benchmark_script, "_wait_for_download_many", wait_many)
    monkeypatch.setattr(benchmark_script, "_video_has_embedded_japanese_text", lambda *_args, **_kwargs: False)

    torrent_hash, paths = benchmark_script._download_release_videos(
        Client(),
        release,
        episodes=[2],
        root=root,
        metadata_timeout_seconds=1,
        download_timeout_seconds=10,
        probe_first=True,
        max_unverified_probe_bytes=100,
    )
    assert torrent_hash == "resume-big"
    assert set(paths) == {2}


def test_diagnostic_media_release_ledger_blocks_no_jp_pack_across_sampled_episodes(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        benchmark_script,
        "collect_diagnostic_reports",
        lambda _corpus: [
            {
                "media_id": 7,
                "episode": 11,
                "reason": "no_embedded_japanese_text_subtitle",
                "source_release": {"info_hash": "PACK", "is_batch": True},
            },
            {
                "media_id": 8,
                "episode": 1,
                "reason": "no_embedded_japanese_text_subtitle",
                "source_release": {"info_hash": "SINGLE", "is_batch": False},
            },
        ],
    )
    assert benchmark_script._diagnostic_media_release_keys(tmp_path) == {(7, "pack")}


def test_acquire_resumes_benchmark_torrent_before_nyaa_search(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    row = {
        "media_id": 1,
        "title": "Show",
        "titles": ["Show"],
        "synonyms": [],
        "episode": 2,
        "episodes": 12,
        "rank": 1,
        "format": "TV",
        "season_year": 2024,
        "relations": [],
    }
    corpus = tmp_path / "corpus"
    download_root = corpus / "downloads"
    download_root.mkdir(parents=True)
    config = SimpleNamespace(
        jimaku=SimpleNamespace(api_key="key"),
        nyaa=SimpleNamespace(min_seeders=1),
    )

    class Downloader:
        def torrents(self):
            return [
                SimpleNamespace(
                    torrent_hash="resume-hash",
                    name="Show 01-12 [720p][BATCH]",
                    save_path=str(download_root),
                    is_batch=True,
                    raw={"total_size": 1_000, "listed_seeders": 0},
                )
            ]

        def files(self, _torrent_hash):
            return [{"index": 0, "name": "Show - 02.mkv", "size": 100}]

        def delete(self, *_args, **_kwargs):
            return None

        def close(self):
            return None

    downloader = Downloader()
    nyaa_calls: list[int] = []
    downloaded: list[str] = []

    monkeypatch.setattr(benchmark_script, "load_config", lambda _path: config)
    monkeypatch.setattr(benchmark_script, "_completed_episode_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_diagnostic_release_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_diagnostic_media_release_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_read_plan", lambda _path: [row])
    monkeypatch.setattr(
        benchmark_script, "_download_client", lambda *_args: ("aria2", downloader)
    )

    def fail_nyaa(*_args, **_kwargs):
        nyaa_calls.append(1)
        raise AssertionError("Nyaa discovery must not run before resumable torrent")

    monkeypatch.setattr(benchmark_script, "_nyaa_ranked_for_plan", fail_nyaa)

    def fake_download(_client, release, *, episodes, root, **_kwargs):
        downloaded.append(str(release.info_hash))
        path = root / "Show - 02.mkv"
        path.write_bytes(b"video")
        return str(release.info_hash), {2: path}

    monkeypatch.setattr(benchmark_script, "_download_release_videos", fake_download)
    monkeypatch.setattr(
        benchmark_script,
        "create_case_from_video",
        lambda **_kwargs: tmp_path / "gold",
    )
    monkeypatch.setattr(benchmark_script, "_aggregate_corpus", lambda _corpus: {"cases": 1})
    monkeypatch.setattr(benchmark_script, "write_json", lambda *_args, **_kwargs: None)

    args = SimpleNamespace(
        config=None,
        corpus=corpus,
        plan=tmp_path / "plan.json",
        start_index=0,
        limit=1,
        backend="aria2",
        episode_batch_size=1,
        max_download_gb=2.0,
        max_unverified_probe_mib=700.0,
        resolution="720p",
        min_source_seeders=1,
        max_release_attempts=8,
        metadata_timeout_seconds=1.0,
        download_timeout_minutes=1.0,
        stall_timeout_seconds=15.0,
        progress_interval_seconds=2.0,
    )

    assert benchmark_script.acquire_plan(args) == 0
    assert nyaa_calls == []
    assert downloaded == ["resume-hash"]


def test_acquire_falls_back_to_fresh_nyaa_after_dead_resume(
    monkeypatch, tmp_path, capsys
) -> None:
    from types import SimpleNamespace

    row = {
        "media_id": 1,
        "title": "Show",
        "titles": ["Show"],
        "synonyms": [],
        "episode": 2,
        "episodes": 12,
        "rank": 1,
        "format": "TV",
        "season_year": 2024,
        "relations": [],
    }
    resume = SimpleNamespace(
        info_hash="dead-resume",
        title="Show - 02 [720p]",
        torrent_url="",
        link="",
        is_batch=False,
        score=1_000_000.0,
        seeders=0,
        size_bytes=100,
        size_text="100 B",
    )
    fresh = SimpleNamespace(
        info_hash="fresh-source",
        title="Show - 02 [480p]",
        torrent_url="",
        link="",
        is_batch=False,
        score=100.0,
        seeders=2,
        size_bytes=100,
        size_text="100 B",
    )
    config = SimpleNamespace(
        jimaku=SimpleNamespace(api_key="key"),
        nyaa=SimpleNamespace(min_seeders=1),
    )

    class Downloader:
        def close(self):
            return None

        def delete(self, *_args, **_kwargs):
            return None

    attempts: list[str] = []
    nyaa_calls: list[int] = []
    monkeypatch.setattr(benchmark_script, "load_config", lambda _path: config)
    monkeypatch.setattr(benchmark_script, "_completed_episode_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_diagnostic_release_keys", lambda _corpus: set())
    monkeypatch.setattr(
        benchmark_script, "_diagnostic_media_release_keys", lambda _corpus: set()
    )
    monkeypatch.setattr(
        benchmark_script, "_diagnostic_media_release_family_keys", lambda _corpus: set()
    )
    monkeypatch.setattr(benchmark_script, "_source_rejection_family_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_read_plan", lambda _path: [row])
    monkeypatch.setattr(
        benchmark_script,
        "_download_client",
        lambda *_args: ("aria2", Downloader()),
    )
    monkeypatch.setattr(
        benchmark_script,
        "_benchmark_resumable_releases",
        lambda *_args, **_kwargs: [resume],
    )

    def discover(*_args, **_kwargs):
        nyaa_calls.append(1)
        # Nyaa may still list the same hash. It must not consume the fresh
        # phase's one-attempt budget after the resume already stalled.
        return [resume, fresh]

    monkeypatch.setattr(benchmark_script, "_nyaa_ranked_for_plan", discover)

    def download(_client, release, *, episodes, root, **_kwargs):
        attempts.append(str(release.info_hash))
        if release.info_hash == "dead-resume":
            raise benchmark_script.Aria2Error(
                "benchmark download stalled for 90s at 0.0% (0.0 B/s)"
            )
        path = root / "Show - 02.mkv"
        path.write_bytes(b"video")
        return str(release.info_hash), {list(episodes)[0]: path}

    monkeypatch.setattr(benchmark_script, "_download_release_videos", download)
    monkeypatch.setattr(
        benchmark_script,
        "create_case_from_video",
        lambda **_kwargs: tmp_path / "gold",
    )
    monkeypatch.setattr(
        benchmark_script, "_aggregate_corpus", lambda _corpus: {"cases": 1}
    )
    monkeypatch.setattr(benchmark_script, "write_json", lambda *_args, **_kwargs: None)

    args = SimpleNamespace(
        config=None,
        corpus=tmp_path / "corpus",
        plan=tmp_path / "plan.json",
        start_index=0,
        limit=1,
        backend="aria2",
        episode_batch_size=1,
        max_download_gb=2.0,
        max_unverified_probe_mib=700.0,
        resolution="720p",
        min_source_seeders=1,
        max_release_attempts=1,
        metadata_timeout_seconds=1.0,
        download_timeout_minutes=1.0,
        stall_timeout_seconds=15.0,
        progress_interval_seconds=2.0,
    )

    assert benchmark_script.acquire_plan(args) == 0
    output = capsys.readouterr().out
    assert "try before fresh Nyaa discovery" in output
    assert "resumable sources produced no GOLD case" in output
    assert attempts == ["dead-resume", "fresh-source"]
    assert nyaa_calls == [1]


def test_acquire_nyaa_failure_ends_cleanly_and_preserves_summary(monkeypatch, tmp_path, capsys) -> None:
    from types import SimpleNamespace

    row = {
        "media_id": 1,
        "title": "Show",
        "titles": ["Show"],
        "synonyms": [],
        "episode": 2,
        "episodes": 12,
        "rank": 1,
        "format": "TV",
        "season_year": 2024,
        "relations": [],
    }
    config = SimpleNamespace(
        jimaku=SimpleNamespace(api_key="key"),
        nyaa=SimpleNamespace(min_seeders=1),
    )

    class Downloader:
        def torrents(self):
            return []
        def close(self):
            return None

    writes: list[dict[str, object]] = []
    monkeypatch.setattr(benchmark_script, "load_config", lambda _path: config)
    monkeypatch.setattr(benchmark_script, "_completed_episode_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_diagnostic_release_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_diagnostic_media_release_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_read_plan", lambda _path: [row])
    monkeypatch.setattr(
        benchmark_script,
        "_download_client",
        lambda *_args: ("aria2", Downloader()),
    )
    monkeypatch.setattr(
        benchmark_script,
        "_nyaa_ranked_for_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            benchmark_script.NyaaError(
                "Show 02: Nyaa temporarily paused after repeated network failures; "
                "Show 2: Nyaa temporarily paused after repeated network failures"
            )
        ),
    )
    monkeypatch.setattr(
        benchmark_script,
        "_aggregate_corpus",
        lambda _corpus: {"schema": "summary", "cases": 0},
    )
    monkeypatch.setattr(
        benchmark_script,
        "write_json",
        lambda _path, payload: writes.append(dict(payload)),
    )

    args = SimpleNamespace(
        config=None,
        corpus=tmp_path / "corpus",
        plan=tmp_path / "plan.json",
        start_index=0,
        limit=1,
        backend="aria2",
        episode_batch_size=1,
        max_download_gb=2.0,
        max_unverified_probe_mib=700.0,
        resolution="720p",
        min_source_seeders=1,
        max_release_attempts=8,
        metadata_timeout_seconds=1.0,
        download_timeout_minutes=1.0,
        stall_timeout_seconds=15.0,
        progress_interval_seconds=2.0,
    )

    assert benchmark_script.acquire_plan(args) == 0
    output = capsys.readouterr().out
    assert "Nyaa unavailable; ending acquisition cleanly" in output
    assert "rerun acquire-plan later" in output
    assert "Traceback" not in output
    assert writes


def test_new_benchmark_torrent_records_resume_identity_tags(monkeypatch, tmp_path) -> None:
    from types import SimpleNamespace

    release = SimpleNamespace(
        info_hash="tagged",
        title="Show 01-12 [720p][BATCH]",
        torrent_url="",
        link="",
        is_batch=True,
    )
    root = tmp_path / "downloads"
    root.mkdir()
    files = [{"index": 0, "name": "Show - 02.mkv", "size": 100}]

    class Client:
        def __init__(self) -> None:
            self.tags = None
        def torrents(self):
            return []
        def add_release(self, _release, *, tags, **_kwargs):
            self.tags = list(tags)
            return "tagged"
        def set_file_priority(self, *_args, **_kwargs):
            return None
        def start(self, *_args, **_kwargs):
            return None
        def torrent_status(self, _hash):
            return {"save_path": str(root)}
        def delete(self, *_args, **_kwargs):
            return None

    client = Client()
    monkeypatch.setattr(benchmark_script, "_wait_for_files", lambda *_args, **_kwargs: files)

    def wait_many(_client, _hash, indexes, **_kwargs):
        assert set(indexes) == {0}
        (root / "Show - 02.mkv").write_bytes(b"video")
        return {0: dict(files[0], progress=1.0)}

    monkeypatch.setattr(benchmark_script, "_wait_for_download_many", wait_many)
    benchmark_script._download_release_videos(
        client,
        release,
        episodes=[2],
        root=root,
        metadata_timeout_seconds=1,
        download_timeout_seconds=10,
        identity_row={"media_id": 123, "title": "Show", "episode": 2},
    )
    assert client.tags == [
        "pudge-benchmark",
        "anilist:123",
        "anime:Show",
        "episode:2",
        "batch",
    ]


def test_declared_multisub_language_list_skips_only_when_japanese_is_absent(
    tmp_path,
) -> None:
    no_jp = (
        "[Erai-raws] Shingeki no Kyojin - 01 ~ 25 [720p][BATCH][Multiple Subtitle] "
        "[ENG][POR-BR][SPA-LA][ARA][FRE][RUS]"
    )
    with_jp = (
        "[Erai-raws] The Fable - 15 [720p][HEVC][Multiple Subtitle]"
        "[ENG][POR-BR][SPA-LA][SPA][FRE][GER][ITA][JPN][POR][POL][DUT]"
        "[NOB][FIN][TUR][SWE][GRE][RUM][KOR][DAN][CHI][HUN][CES][SLO]"
    )
    assert benchmark_script._benchmark_declares_no_japanese_subtitles(no_jp) is True
    assert "JPN" not in benchmark_script._benchmark_declared_subtitle_languages(no_jp)
    assert benchmark_script._benchmark_declares_no_japanese_subtitles(with_jp) is False
    assert "JPN" in benchmark_script._benchmark_declared_subtitle_languages(with_jp)

    netflix = SimpleNamespace(
        title=(
            "[Erai-raws] Show - 03 [720p][NF WEB-DL][Multiple Subtitle]"
            "[ENG][FRE]"
        ),
        info_hash="nf-probe",
        torrent_url="",
        is_batch=False,
        size_bytes=300_000_000,
    )
    assert benchmark_script._benchmark_declares_no_japanese_subtitles(netflix.title)
    usable = benchmark_script._usable_benchmark_releases(
        [netflix],
        row={"media_id": 7, "episode": 3},
        corpus=tmp_path,
        max_bytes=500_000_000,
        diagnostic_releases=set(),
        diagnostic_media_releases=set(),
        diagnostic_media_families=set(),
    )
    assert usable == [netflix]


def test_no_jp_release_family_collapses_480p_and_720p_variants() -> None:
    low = SimpleNamespace(
        title=(
            "[Erai-raws] Shingeki no Kyojin - 01 ~ 25 [480p][BATCH][Multiple Subtitle] "
            "[ENG][POR-BR][SPA-LA][ARA][FRE][RUS]"
        ),
        group="Erai-raws",
    )
    high = SimpleNamespace(
        title=(
            "[Erai-raws] Shingeki no Kyojin - 01 ~ 25 [720p][Multiple Subtitle] "
            "[ENG][POR-BR][SPA-LA][ARA][FRE][RUS]"
        ),
        group="Erai-raws",
    )
    assert benchmark_script._benchmark_release_family_key(low)
    assert benchmark_script._benchmark_release_family_key(low) == benchmark_script._benchmark_release_family_key(high)


def test_acquire_skips_declared_no_jp_release_without_downloading(monkeypatch, tmp_path) -> None:
    release = SimpleNamespace(
        title=(
            "[Erai-raws] Shingeki no Kyojin - 01 ~ 25 [480p][BATCH][Multiple Subtitle] "
            "[ENG][POR-BR][SPA-LA][ARA][FRE][RUS]"
        ),
        info_hash="family-no-jp",
        torrent_url="",
        link="",
        seeders=10,
        is_batch=True,
        score=300.0,
        size_bytes=9 * 1024**3,
        size_text="9.0 GiB",
        group="Erai-raws",
    )
    row = {
        "media_id": 16498,
        "title": "Shingeki no Kyojin",
        "titles": ["Attack on Titan"],
        "synonyms": [],
        "episodes": 25,
        "format": "TV",
        "season_year": 2013,
        "episode": 11,
        "rank": 1,
        "relations": [],
    }
    config = SimpleNamespace(
        jimaku=SimpleNamespace(api_key="key"),
        nyaa=SimpleNamespace(min_seeders=1),
    )

    class Downloader:
        def torrents(self):
            return []
        def close(self):
            return None
        def add_release(self, *_args, **_kwargs):
            raise AssertionError("declared no-JP source must not be downloaded")

    monkeypatch.setattr(benchmark_script, "load_config", lambda _path: config)
    monkeypatch.setattr(benchmark_script, "_completed_episode_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_diagnostic_release_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_diagnostic_media_release_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_diagnostic_media_release_family_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_read_plan", lambda _path: [row])
    monkeypatch.setattr(benchmark_script, "_download_client", lambda *_args: ("aria2", Downloader()))
    monkeypatch.setattr(benchmark_script, "_benchmark_resumable_releases", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(benchmark_script, "_nyaa_ranked_for_plan", lambda *_args, **_kwargs: [release])
    monkeypatch.setattr(benchmark_script, "_write_corpus_summary", lambda _corpus: None)

    args = SimpleNamespace(
        config=None,
        corpus=tmp_path / "corpus",
        plan=tmp_path / "plan.json",
        start_index=0,
        limit=1,
        backend="aria2",
        episode_batch_size=1,
        max_download_gb=2.0,
        max_unverified_probe_mib=700.0,
        resolution="720p",
        min_source_seeders=1,
        max_release_attempts=8,
        metadata_timeout_seconds=1.0,
        download_timeout_minutes=1.0,
        stall_timeout_seconds=15.0,
        progress_interval_seconds=2.0,
    )
    assert benchmark_script.acquire_plan(args) == 0
    rows = benchmark_script._collect_source_rejections(args.corpus)
    assert len(rows) == 1
    assert rows[0]["reason"] == "declared_subtitle_languages_without_japanese"
    assert "RUS" in rows[0]["declared_subtitle_languages"]


def test_source_rejection_family_is_persistent(tmp_path) -> None:
    release = SimpleNamespace(
        title=(
            "[Erai-raws] Show - 01 ~ 12 [480p][Multiple Subtitle]"
            "[ENG][FRE][RUS]"
        ),
        info_hash="hash",
        seeders=1,
        is_batch=True,
        group="Erai-raws",
    )
    row = {"media_id": 7, "episode": 2}
    family = benchmark_script._record_source_rejection(
        tmp_path, row, release, reason="declared_subtitle_languages_without_japanese"
    )
    assert (7, family) in benchmark_script._source_rejection_family_keys(tmp_path)


def test_resumable_release_identity_is_fail_closed_for_wrong_anime(tmp_path) -> None:
    row = {
        "media_id": 16498,
        "title": "Shingeki no Kyojin",
        "titles": ["Shingeki no Kyojin", "Attack on Titan"],
        "synonyms": [],
        "episode": 2,
        "episodes": 25,
        "rank": 1,
        "format": "TV",
        "season_year": 2013,
        "relations": [],
    }
    root = tmp_path / "downloads"
    root.mkdir()

    class Downloader:
        def torrents(self):
            return [
                # New tagged benchmark torrent: explicit foreign AniList id must win
                # over a coincidental episode-number match in its files.
                SimpleNamespace(
                    torrent_hash="kimetsu-tagged",
                    name="[NoobSubs] Kimetsu no Yaiba (720p Blu-ray 8bit AAC MP4)",
                    save_path=str(root),
                    media_id=38000,
                    is_batch=True,
                    raw={"total_size": 1000},
                ),
                # Legacy pre-tag torrent must also fail closed on production title
                # identity instead of matching solely because it contains E02.
                SimpleNamespace(
                    torrent_hash="kimetsu-legacy",
                    name="[NoobSubs] Kimetsu no Yaiba (720p Blu-ray 8bit AAC MP4)",
                    save_path=str(root),
                    media_id=None,
                    is_batch=True,
                    raw={"total_size": 1000},
                ),
                SimpleNamespace(
                    torrent_hash="aot-legacy",
                    name="[Erai-raws] Shingeki no Kyojin - 01 ~ 25 [720p][BATCH]",
                    save_path=str(root),
                    media_id=None,
                    is_batch=True,
                    raw={"total_size": 1000, "listed_seeders": 1},
                ),
            ]

        def files(self, torrent_hash):
            # All three torrents deliberately expose the same episode number.
            # Anime identity, not file numbering, must decide resumability.
            return [{"index": 0, "name": "Episode 02.mkv", "size": 100}]

    releases = benchmark_script._benchmark_resumable_releases(
        Downloader(), row, episodes=[2], root=root
    )
    assert [item.info_hash for item in releases] == ["aot-legacy"]


def test_plan_relation_graph_propagates_negative_titles_across_franchise_chain() -> None:
    rows = [
        {
            "media_id": 101922,
            "title": "Kimetsu no Yaiba",
            "titles": ["Demon Slayer: Kimetsu no Yaiba"],
            "synonyms": [],
            "relations": [
                {
                    "relation_type": "SEQUEL",
                    "media_id": 129874,
                    "title": "Kimetsu no Yaiba: Mugen Ressha-hen (TV)",
                }
            ],
        },
        {
            "media_id": 129874,
            "title": "Kimetsu no Yaiba: Mugen Ressha-hen (TV)",
            "titles": [],
            "synonyms": [],
            "relations": [
                {
                    "relation_type": "SEQUEL",
                    "media_id": 142329,
                    "title": "Kimetsu no Yaiba: Yuukaku-hen",
                }
            ],
        },
        {
            "media_id": 142329,
            "title": "Kimetsu no Yaiba: Yuukaku-hen",
            "titles": [],
            "synonyms": [],
            "relations": [
                {
                    "relation_type": "SEQUEL",
                    "media_id": 145139,
                    "title": "Kimetsu no Yaiba: Katanakaji no Sato-hen",
                }
            ],
        },
        {
            "media_id": 145139,
            "title": "Kimetsu no Yaiba: Katanakaji no Sato-hen",
            "titles": [],
            "synonyms": [],
            "relations": [
                {
                    "relation_type": "SEQUEL",
                    "media_id": 166240,
                    "title": "Kimetsu no Yaiba: Hashira Geiko-hen",
                }
            ],
        },
        {
            "media_id": 166240,
            "title": "Kimetsu no Yaiba: Hashira Geiko-hen",
            "titles": [],
            "synonyms": [],
            "relations": [],
        },
    ]

    negatives = benchmark_script._plan_negative_titles_by_media(rows)[101922]
    assert "Kimetsu no Yaiba: Mugen Ressha-hen (TV)" in negatives
    assert "Kimetsu no Yaiba: Yuukaku-hen" in negatives
    assert "Kimetsu no Yaiba: Katanakaji no Sato-hen" in negatives
    assert "Kimetsu no Yaiba: Hashira Geiko-hen" in negatives
    assert "Kimetsu no Yaiba" not in negatives


def test_benchmark_source_first_rejects_related_kimetsu_arc_even_with_jpn_signal(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    wrong = SimpleNamespace(
        title=(
            "[Erai-raws] Kimetsu no Yaiba - Mugen Ressha Hen (TV) - 05 "
            "[720p][Multiple Subtitle][JPN]"
        ),
        info_hash="wrong-related",
        torrent_url="",
        link="",
        seeders=10,
        is_batch=False,
        score=100.0,
        size_bytes=100_000_000,
    )
    correct = SimpleNamespace(
        title="[Group] Kimetsu no Yaiba - 05 [480p][JPN]",
        info_hash="correct-root",
        torrent_url="",
        link="",
        seeders=4,
        is_batch=False,
        score=50.0,
        size_bytes=100_000_000,
    )

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def search(self, query: str):
            return [wrong] if query.endswith("JPN") else []

        def close(self):
            return None

    monkeypatch.setattr(benchmark_script, "NyaaClient", Client)
    monkeypatch.setattr(
        benchmark_script, "score_release", lambda release, *_args, **_kwargs: release
    )
    calls: list[tuple[bool, tuple[str, ...]]] = []

    def generic(*_args, batch: bool, negative_titles=(), **_kwargs):
        calls.append((batch, tuple(negative_titles)))
        return [correct] if not batch else []

    monkeypatch.setattr(benchmark_script, "search_ranked", generic)
    config = SimpleNamespace(
        nyaa=SimpleNamespace(
            base_url="https://example.invalid",
            proxy_mode="direct",
            proxy_url="",
            pre_search_command="",
            category="1_2",
            trusted_groups=[],
            preferred_groups=[],
            blocked_groups=[],
            min_seeders=1,
            episode_min_size_mb=1,
            episode_max_size_mb=3500,
            preferred_video_codecs=["AVC"],
            preferred_sources=["WEB-DL"],
            require_japanese_audio=True,
            avoid_upscaled=True,
        )
    )
    negative_titles = ("Kimetsu no Yaiba: Mugen Ressha-hen (TV)",)
    rows = benchmark_script._nyaa_ranked_for_plan(
        {
            "media_id": 101922,
            "title": "Kimetsu no Yaiba",
            "titles": ["Demon Slayer: Kimetsu no Yaiba"],
            "synonyms": [],
            "episodes": 26,
            "format": "TV",
            "season_year": 2019,
            "episode": 5,
            "relations": [],
        },
        config,
        resolution="720p",
        min_source_seeders=1,
        negative_titles=negative_titles,
    )
    assert [row.info_hash for row in rows] == ["correct-root"]
    assert calls == [(False, negative_titles), (True, negative_titles)]


def test_old_plan_without_relations_still_uses_distinct_anilist_title_extensions_as_negatives() -> None:
    rows = [
        {
            "media_id": 101922,
            "title": "Kimetsu no Yaiba",
            "titles": ["Demon Slayer: Kimetsu no Yaiba"],
            "synonyms": [],
            "relations": [],
        },
        {
            "media_id": 166240,
            "title": "Kimetsu no Yaiba: Hashira Geiko-hen",
            "titles": ["Demon Slayer: Kimetsu no Yaiba Hashira Training Arc"],
            "synonyms": [],
            "relations": [],
        },
    ]

    negatives = benchmark_script._plan_negative_titles_by_media(rows)[101922]
    assert "Kimetsu no Yaiba: Hashira Geiko-hen" in negatives
    assert "Demon Slayer: Kimetsu no Yaiba Hashira Training Arc" in negatives


def test_acquire_defaults_to_strict_erai_jp_feed() -> None:
    args = benchmark_script._parser().parse_args(
        ["acquire-plan", "--plan", "plan.json", "--corpus", "corpus"]
    )
    assert args.source_strategy == "erai-jp-feed"
    assert args.feed_query == []
    assert args.feed_category == "1_0"
    assert benchmark_script._DEFAULT_BENCHMARK_FEED_QUERIES == (
        "Erai [ENG] [JP] 480",
        "Erai [ENG] [JPN] 480",
        "Erai NF 720",
    )


def test_strict_feed_contract_accepts_erai_jp_or_nf_single_and_live() -> None:
    def release(title: str, *, seeders: int = 3, is_batch: bool = False):
        return SimpleNamespace(
            title=title,
            seeders=seeders,
            is_batch=is_batch,
            size_bytes=180_000_000,
        )

    valid = release("[Erai-raws] Show - 03 [480p][Multiple Subtitle][ENG][JP]")
    assert benchmark_script._benchmark_feed_release_is_eligible(
        valid, resolution="720p", minimum_seeders=1
    )
    netflix = release(
        "[Erai-raws] Show - 03 [720p][NF WEB-DL][Multiple Subtitle][ENG][FRE]"
    )
    assert benchmark_script._benchmark_feed_release_is_eligible(
        netflix, resolution="720p", minimum_seeders=1
    )
    for invalid in (
        release("[Erai-raws] Show - 03 [480p][Multiple Subtitle][ENG]"),
        release("[Erai-raws] Show - 03 [480p][Multiple Subtitle][JP]"),
        release("[Other] Show - 03 [480p][Multiple Subtitle][ENG][JP]"),
        release("[Other] Show - 03 [720p][NF WEB-DL][Multiple Subtitle][ENG]"),
        release("[Erai-raws] Show 01-12 [480p][Multiple Subtitle][ENG][JP]", is_batch=True),
        release("[Erai-raws] Show - 03 [480p][Multiple Subtitle][ENG][JP]", seeders=0),
    ):
        assert not benchmark_script._benchmark_feed_release_is_eligible(
            invalid, resolution="480p", minimum_seeders=1
        )


def test_feed_match_uses_release_episode_and_most_specific_exact_plan_title() -> None:
    release = SimpleNamespace(
        title="[Erai-raws] Show Plus - 07 [480p][Multiple Subtitle][ENG][JPN]"
    )
    plan = [
        {
            "media_id": 1,
            "title": "Show",
            "titles": ["Show"],
            "synonyms": [],
            "episodes": 12,
            "episode": 2,
            "format": "TV",
            "relations": [],
        },
        {
            "media_id": 2,
            "title": "Show Plus",
            "titles": ["Show Plus"],
            "synonyms": [],
            "episodes": 12,
            "episode": 4,
            "format": "TV",
            "relations": [],
        },
    ]
    row, reason = benchmark_script._match_benchmark_feed_release(
        release,
        plan,
        negative_titles_by_media={1: (), 2: ()},
    )
    assert reason == "matched"
    assert row is not None
    assert (row["media_id"], row["episode"]) == (2, 7)


def test_feed_match_rejects_ambiguous_duplicate_plan_alias() -> None:
    release = SimpleNamespace(
        title="[Erai-raws] Shared Name - 03 [480p][Multiple Subtitle][ENG][JP]"
    )
    plan = [
        {
            "media_id": media_id,
            "title": "Shared Name",
            "titles": ["Shared Name"],
            "synonyms": [],
            "episodes": 12,
            "episode": 1,
            "format": "TV",
            "relations": [],
        }
        for media_id in (10, 20)
    ]
    row, reason = benchmark_script._match_benchmark_feed_release(
        release,
        plan,
        negative_titles_by_media={10: (), 20: ()},
    )
    assert row is None
    assert reason == "ambiguous-plan-title"


def test_feed_discovery_adds_nf_720_and_requests_server_seed_sort(
    monkeypatch,
) -> None:
    exact = SimpleNamespace(
        title="[Erai-raws] Show - 05 [480p][Multiple Subtitle][ENG][JP]",
        info_hash="exact",
        torrent_url="",
        link="",
        seeders=8,
        downloads=100,
        size_bytes=180_000_000,
        is_batch=False,
        score=0.0,
    )
    missing_jp = SimpleNamespace(
        title="[Erai-raws] Other - 05 [480p][Multiple Subtitle][ENG]",
        info_hash="missing-jp",
        torrent_url="",
        link="",
        seeders=10,
        downloads=200,
        size_bytes=170_000_000,
        is_batch=False,
        score=0.0,
    )
    netflix = SimpleNamespace(
        title=(
            "[Erai-raws] Show - 05 [720p][NF WEB-DL]"
            "[Multiple Subtitle][ENG][FRE]"
        ),
        info_hash="netflix",
        torrent_url="",
        link="",
        seeders=20,
        downloads=300,
        size_bytes=350_000_000,
        is_batch=False,
        score=0.0,
    )
    calls: list[tuple[str, str | None, int, str | None, str | None]] = []

    class Client:
        def __init__(self, *_args, **kwargs):
            assert kwargs["category"] == "1_0"

        def search(
            self,
            query: str,
            *,
            category=None,
            filter_id=0,
            sort_by=None,
            order=None,
        ):
            calls.append((query, category, filter_id, sort_by, order))
            if "[JP]" in query:
                return [exact, missing_jp]
            if "[JPN]" in query:
                return [exact]
            return [netflix]

        def close(self):
            return None

    monkeypatch.setattr(benchmark_script, "NyaaClient", Client)
    monkeypatch.setattr(
        benchmark_script, "score_release", lambda release, *_args, **_kwargs: release
    )
    config = SimpleNamespace(
        nyaa=SimpleNamespace(
            base_url="https://example.invalid",
            proxy_mode="direct",
            proxy_url="",
            pre_search_command="",
            trusted_groups=[],
            preferred_groups=[],
            blocked_groups=[],
            min_seeders=1,
            episode_min_size_mb=1,
            episode_max_size_mb=3500,
            preferred_video_codecs=["AVC"],
            preferred_sources=["WEB-DL"],
            require_japanese_audio=True,
            avoid_upscaled=True,
        )
    )
    plan = [
        {
            "media_id": 7,
            "title": "Show",
            "titles": ["Show"],
            "synonyms": [],
            "episodes": 12,
            "episode": 2,
            "format": "TV",
            "relations": [],
        }
    ]
    rows, releases, stats = benchmark_script._discover_benchmark_feed(
        plan,
        config,
        queries=benchmark_script._DEFAULT_BENCHMARK_FEED_QUERIES,
        category="1_0",
        resolution="720p",
        minimum_seeders=1,
        negative_titles_by_media={7: ()},
    )
    assert calls == [
        ("Erai [ENG] [JP] 480", "1_0", 0, "seeders", "desc"),
        ("Erai [ENG] [JPN] 480", "1_0", 0, "seeders", "desc"),
        ("Erai NF 720", "1_0", 0, "seeders", "desc"),
    ]
    assert [(row["media_id"], row["episode"]) for row in rows] == [(7, 5)]
    assert [item.info_hash for item in releases[(7, 5)]] == ["netflix", "exact"]
    assert stats == {
        "raw": 3,
        "eligible": 2,
        "matched": 1,
        "plan_matched": 2,
        "anilist_matched": 0,
        "cache_matched": 0,
        "unmatched": 0,
        "ambiguous": 0,
        "anilist_errors": 0,
    }


def test_strict_feed_acquisition_never_calls_resume_or_generic_discovery(
    monkeypatch, tmp_path
) -> None:
    row = {
        "media_id": 7,
        "title": "Show",
        "titles": ["Show"],
        "synonyms": [],
        "episodes": 12,
        "episode": 5,
        "rank": 10,
        "format": "TV",
        "relations": [],
    }
    release = SimpleNamespace(
        title="[Erai-raws] Show - 05 [480p][Multiple Subtitle][ENG][JP]",
        info_hash="feed-only",
        torrent_url="",
        link="",
        seeders=8,
        downloads=100,
        size_bytes=180_000_000,
        size_text="171.7 MiB",
        is_batch=False,
        score=100.0,
    )
    config = SimpleNamespace(
        jimaku=SimpleNamespace(api_key="key"),
        nyaa=SimpleNamespace(min_seeders=1),
    )

    class Downloader:
        def close(self):
            return None

    monkeypatch.setattr(benchmark_script, "load_config", lambda _path: config)
    monkeypatch.setattr(benchmark_script, "_read_plan", lambda _path: [row])
    monkeypatch.setattr(benchmark_script, "_completed_episode_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "_diagnostic_release_keys", lambda _corpus: set())
    monkeypatch.setattr(
        benchmark_script, "_diagnostic_media_release_keys", lambda _corpus: set()
    )
    monkeypatch.setattr(
        benchmark_script, "_diagnostic_media_release_family_keys", lambda _corpus: set()
    )
    monkeypatch.setattr(
        benchmark_script, "_source_rejection_family_keys", lambda _corpus: set()
    )
    monkeypatch.setattr(
        benchmark_script,
        "_discover_benchmark_feed",
        lambda *_args, **_kwargs: (
            [row],
            {(7, 5): [release]},
            {
                "raw": 1,
                "eligible": 1,
                "matched": 1,
                "plan_matched": 1,
                "anilist_matched": 0,
                "cache_matched": 0,
                "unmatched": 0,
                "ambiguous": 0,
                "anilist_errors": 0,
            },
        ),
    )
    monkeypatch.setattr(
        benchmark_script, "_download_client", lambda *_args: ("aria2", Downloader())
    )
    monkeypatch.setattr(
        benchmark_script,
        "_benchmark_resumable_releases",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("strict feed must not inspect unrelated resumable torrents")
        ),
    )
    monkeypatch.setattr(
        benchmark_script,
        "_nyaa_ranked_for_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("strict feed must not run generic discovery")
        ),
    )
    attempts: list[str] = []

    def stalled(_client, selected, **_kwargs):
        attempts.append(str(selected.info_hash))
        raise benchmark_script.Aria2Error("feed swarm stalled")

    monkeypatch.setattr(benchmark_script, "_download_release_videos", stalled)
    monkeypatch.setattr(benchmark_script, "_aggregate_corpus", lambda _corpus: {"cases": 0})
    monkeypatch.setattr(benchmark_script, "write_json", lambda *_args, **_kwargs: None)

    args = SimpleNamespace(
        config=None,
        corpus=tmp_path / "corpus",
        plan=tmp_path / "plan.json",
        source_strategy="erai-jp-feed",
        feed_query=[],
        feed_category="1_0",
        start_index=0,
        limit=1,
        backend="aria2",
        episode_batch_size=3,
        max_download_gb=2.0,
        max_unverified_probe_mib=700.0,
        resolution="720p",
        min_source_seeders=1,
        max_release_attempts=8,
        metadata_timeout_seconds=1.0,
        download_timeout_minutes=1.0,
        stall_timeout_seconds=15.0,
        progress_interval_seconds=2.0,
    )
    assert benchmark_script.acquire_plan(args) == 0
    assert attempts == ["feed-only"]


def test_feed_identity_strips_erai_tags_and_uses_the_release_episode() -> None:
    release = SimpleNamespace(
        title=(
            "[Erai-raws] Blue Lock vs. U-20 Japan - 07 "
            "[480p][Multiple Subtitle][ENG][JPN]"
        )
    )
    identity = benchmark_script._benchmark_feed_identity(release)
    assert identity.title == "Blue Lock vs U-20 Japan"
    assert identity.episode == 7


def test_feed_identity_preserves_slash_inside_anime_title() -> None:
    release = SimpleNamespace(
        title=(
            "[Erai-raws] Ranma 1/2 (2024) 2nd Season - 12 "
            "[720p NF WEB-DL AVC AAC][MultiSub][EF24474B]"
        )
    )
    identity = benchmark_script._benchmark_feed_identity(release)
    assert identity.title == "Ranma 1/2 (2024) 2nd Season"
    assert identity.episode == 12
    assert identity.raw_name == release.title


def test_feed_anilist_match_prefers_exact_sequel_over_popular_root() -> None:
    identity = benchmark_script._benchmark_feed_identity(
        SimpleNamespace(
            title="[Erai-raws] Show Plus - 07 [480p][Multiple Subtitle][ENG][JP]"
        )
    )
    root = SimpleNamespace(
        id=1,
        titles=["Show"],
        synonyms=[],
        episodes=12,
        format="TV",
        season_year=2020,
        score=110.0,
    )
    sequel = SimpleNamespace(
        id=2,
        titles=["Show Plus"],
        synonyms=[],
        episodes=12,
        format="TV",
        season_year=2024,
        score=80.0,
    )
    row, reason = benchmark_script._benchmark_feed_anilist_row(
        identity,
        [root, sequel],
        release_title=identity.raw_name,
    )
    assert reason == "matched"
    assert row is not None
    assert (row["media_id"], row["episode"], row["title"]) == (2, 7, "Show Plus")


def test_feed_anilist_match_fails_closed_for_duplicate_exact_titles() -> None:
    identity = benchmark_script._benchmark_feed_identity(
        SimpleNamespace(
            title="[Erai-raws] Shared - 01 [480p][Multiple Subtitle][ENG][JP]"
        )
    )
    candidates = [
        SimpleNamespace(
            id=media_id,
            titles=["Shared"],
            synonyms=[],
            episodes=12,
            format="TV",
            season_year=year,
            score=100.0,
        )
        for media_id, year in ((10, 2000), (20, 2020))
    ]
    row, reason = benchmark_script._benchmark_feed_anilist_row(
        identity,
        candidates,
        release_title=identity.raw_name,
    )
    assert row is None
    assert reason == "anilist-ambiguous-title"


def test_feed_anilist_search_resolves_every_episode_and_reuses_persistent_cache(
    monkeypatch, tmp_path
) -> None:
    releases = [
        SimpleNamespace(
            title=(
                f"[Erai-raws] New Feed Show - {episode:02d} "
                "[480p][Multiple Subtitle][ENG][JPN]"
            ),
            info_hash=f"feed-{episode}",
            torrent_url="",
            link="",
            seeders=10 - episode,
            leechers=2,
            downloads=100,
            published="Sat, 29 Aug 2026 10:00:00 +0000",
            size_bytes=180_000_000,
            is_batch=False,
            score=0.0,
        )
        for episode in (5, 6)
    ]

    class Nyaa:
        def __init__(self, *_args, **_kwargs):
            pass

        def search(self, query: str, **_kwargs):
            return releases if "[JP]" in query else list(reversed(releases))

        def close(self):
            return None

    search_calls: list[str] = []

    class AniList:
        def __init__(self, *_args, **_kwargs):
            pass

        def search(self, identity):
            search_calls.append(identity.title)
            return [
                SimpleNamespace(
                    id=777,
                    titles=["New Feed Show"],
                    synonyms=[],
                    episodes=12,
                    format="TV",
                    season_year=2026,
                    score=104.0,
                )
            ]

        def close(self):
            return None

    monkeypatch.setattr(benchmark_script, "NyaaClient", Nyaa)
    monkeypatch.setattr(benchmark_script, "AniListClient", AniList)
    monkeypatch.setattr(
        benchmark_script, "score_release", lambda release, *_args, **_kwargs: release
    )
    config = SimpleNamespace(
        anilist=SimpleNamespace(
            endpoint="https://example.invalid/graphql",
            access_token="",
        ),
        nyaa=SimpleNamespace(
            base_url="https://example.invalid",
            proxy_mode="direct",
            proxy_url="",
            pre_search_command="",
            trusted_groups=[],
            preferred_groups=[],
            blocked_groups=[],
            min_seeders=1,
            episode_min_size_mb=1,
            episode_max_size_mb=3500,
            preferred_video_codecs=["AVC"],
            preferred_sources=["WEB-DL"],
            require_japanese_audio=True,
            avoid_upscaled=True,
        ),
    )
    cache_path = tmp_path / "feed-anilist-cache.json"
    rows, mapped, stats = benchmark_script._discover_benchmark_feed(
        [],
        config,
        queries=benchmark_script._DEFAULT_BENCHMARK_FEED_QUERIES,
        category="1_0",
        resolution="720p",
        minimum_seeders=1,
        negative_titles_by_media={},
        anilist_cache_path=cache_path,
    )
    assert search_calls == ["New Feed Show"]
    assert [(row["media_id"], row["episode"]) for row in rows] == [(777, 5), (777, 6)]
    assert sorted(mapped) == [(777, 5), (777, 6)]
    assert stats["matched"] == 2
    assert stats["anilist_matched"] == 1
    assert stats["cache_matched"] == 1
    assert cache_path.is_file()

    class NoNetworkAniList:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("persistent title cache must avoid a second AniList search")

    monkeypatch.setattr(benchmark_script, "AniListClient", NoNetworkAniList)
    rows_again, _mapped_again, stats_again = benchmark_script._discover_benchmark_feed(
        [],
        config,
        queries=benchmark_script._DEFAULT_BENCHMARK_FEED_QUERIES,
        category="1_0",
        resolution="720p",
        minimum_seeders=1,
        negative_titles_by_media={},
        anilist_cache_path=cache_path,
    )
    assert [(row["media_id"], row["episode"]) for row in rows_again] == [
        (777, 5),
        (777, 6),
    ]
    assert stats_again["cache_matched"] == 2
    assert stats_again["anilist_matched"] == 0


def test_feed_sort_prefers_more_seeders_then_recent_active_swarm() -> None:
    def release(*, seeders: int, leechers: int, published: str):
        return SimpleNamespace(
            seeders=seeders,
            leechers=leechers,
            downloads=10,
            published=published,
            size_bytes=100,
        )

    old_many = release(
        seeders=5,
        leechers=0,
        published="Mon, 01 Jan 2024 00:00:00 +0000",
    )
    fresh_five = release(
        seeders=5,
        leechers=2,
        published="Sat, 29 Aug 2026 10:00:00 +0000",
    )
    fresh_four = release(
        seeders=4,
        leechers=9,
        published="Sat, 29 Aug 2026 11:00:00 +0000",
    )
    assert benchmark_script._benchmark_feed_release_sort_key(fresh_five) > benchmark_script._benchmark_feed_release_sort_key(old_many)
    assert benchmark_script._benchmark_feed_release_sort_key(old_many) > benchmark_script._benchmark_feed_release_sort_key(fresh_four)


def test_random_stress_rows_choose_one_episode_per_anime_and_shuffle(monkeypatch, tmp_path) -> None:
    payload = {
        "schema": "pudge-subtitle-benchmark-plan-v1",
        "episodes": [
            {"media_id": 1, "title": "One", "episode": 7, "rank": 1, "format": "TV", "relations": []},
            {"media_id": 2, "title": "Two", "episode": 3, "rank": 2, "format": "ONA", "relations": []},
            {"media_id": 3, "title": "Three", "episode": 11, "rank": 3, "format": "OVA", "relations": []},
            {"media_id": 4, "title": "Movie", "episode": 1, "rank": 4, "format": "MOVIE", "relations": []},
        ],
    }
    calls = []

    def fake_fetch(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return payload

    monkeypatch.setattr(benchmark_script, "fetch_top_anime_plan", fake_fetch)
    rows, plan = benchmark_script._random_stress_rows(
        "https://example.invalid/graphql",
        anime_limit=1000,
        sort="popularity",
        seed=42,
        cache_dir=tmp_path,
        max_retries=6,
        retry_base_seconds=2.0,
    )

    assert plan is payload
    assert sorted((row["media_id"], row["episode"]) for row in rows) == [(1, 7), (2, 3), (3, 11)]
    assert [row["media_id"] for row in rows] == [2, 1, 3]
    assert calls[0][1]["sample_episodes"] == 1
    assert calls[0][1]["seed"] == 42



def test_stress_discovery_queries_are_episode_first_and_not_jp_oriented() -> None:
    row = {
        "title": "Sousou no Frieren",
        "titles": ["Frieren: Beyond Journey's End"],
        "synonyms": ["Frieren"],
        "episode": 14,
    }
    exact = benchmark_script._stress_discovery_queries(row, batch=False)
    batch = benchmark_script._stress_discovery_queries(row, batch=True)
    joined = " ".join([*exact, *batch]).casefold()
    assert "sousou no frieren 14" in joined
    assert "frieren: beyond journey's end e14" in joined
    assert " jpn" not in joined
    assert " japanese subtitle" not in joined
    assert "multiple subtitle" not in joined
    assert "netflix" not in joined


def test_stress_identity_allows_plain_exact_title_but_keeps_explicit_sequel_conflict(monkeypatch) -> None:
    row = {
        "media_id": 1,
        "title": "To Be Hero X",
        "titles": ["To Be Hero X"],
        "synonyms": [],
        "episode": 9,
        "episodes": 24,
        "relations": [],
    }
    monkeypatch.setattr(
        benchmark_script,
        "_source_identity_mismatch_reason",
        lambda *_args: "cross_season_source_mismatch",
    )
    monkeypatch.setattr(
        benchmark_script,
        "release_title_is_plausible",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        benchmark_script,
        "_stress_parsed_release_title",
        lambda _title: "To Be Hero X",
    )
    assert benchmark_script._stress_source_identity_mismatch_reason(
        row, "[EMBER] To Be Hero X - 09 [1080p]"
    ) is None
    assert benchmark_script._stress_source_identity_mismatch_reason(
        row, "[Group] To Be Hero X Season 2 - 09 [1080p]"
    ) == "cross_season_source_mismatch"


def test_stress_title_identity_rejects_ambiguous_one_word_substring(monkeypatch) -> None:
    row = {
        "media_id": 1,
        "title": "Another",
        "titles": ["Another"],
        "synonyms": [],
        "episode": 4,
        "episodes": 12,
        "relations": [],
    }
    monkeypatch.setattr(
        benchmark_script,
        "release_title_is_plausible",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        benchmark_script,
        "_stress_parsed_release_title",
        lambda _title: "16bit Sensation Another Layer",
    )
    assert not benchmark_script._stress_release_title_is_plausible(
        row, "[SubsPlease] 16bit Sensation - Another Layer - 04 [1080p]"
    )


def test_stress_existing_download_sizes_resume_unique_episode_budget(tmp_path) -> None:
    events = tmp_path / "stress-events.jsonl"
    rows = [
        {"event": "download_ok", "media_id": 1, "episode": 2, "bytes": 100},
        {"event": "download_ok", "media_id": 1, "episode": 2, "bytes": 120},
        {"event": "attempt_failed", "media_id": 2, "episode": 3, "bytes": 999},
        {"event": "download_ok", "media_id": 3, "episode": 4, "bytes": 80},
    ]
    events.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    assert benchmark_script._stress_existing_download_sizes(tmp_path) == {
        (1, 2): 120,
        (3, 4): 80,
    }


def test_random_stress_downloads_exact_episode_and_runs_diagnostic_pipeline(monkeypatch, tmp_path) -> None:
    row = {
        "media_id": 123,
        "title": "Stress Show",
        "titles": ["Stress Show"],
        "synonyms": [],
        "episode": 3,
        "episodes": 12,
        "rank": 9,
        "relations": [],
    }
    plan = {"schema": "pudge-subtitle-benchmark-plan-v1", "episodes": [row]}
    monkeypatch.setattr(benchmark_script, "_random_stress_rows", lambda *_args, **_kwargs: ([row], plan))
    monkeypatch.setattr(benchmark_script, "_completed_episode_keys", lambda _corpus: set())
    monkeypatch.setattr(benchmark_script, "collect_diagnostic_reports", lambda _corpus: [])
    monkeypatch.setattr(benchmark_script, "_write_corpus_summary", lambda _corpus: {})
    monkeypatch.setattr(benchmark_script, "_stress_source_identity_mismatch_reason", lambda *_args: None)
    monkeypatch.setattr(benchmark_script, "_register_stress_library_video", lambda *_args, **_kwargs: {})

    config = SimpleNamespace(
        jimaku=SimpleNamespace(api_key="key"),
        anilist=SimpleNamespace(endpoint="https://example.invalid/graphql"),
    )
    monkeypatch.setattr(benchmark_script, "load_config", lambda _path: config)

    release = SimpleNamespace(
        title="[Group] Stress Show - 03 [720p]",
        info_hash="abc",
        seeders=8,
        size_bytes=123,
        is_batch=False,
    )
    monkeypatch.setattr(
        benchmark_script,
        "_nyaa_ranked_for_stress",
        lambda *_args, **_kwargs: ([release], {"reject_counts": {}}),
    )

    class Downloader:
        def __init__(self):
            self.deleted = []
            self.closed = False

        def delete(self, torrent_hash, *, delete_files):
            self.deleted.append((torrent_hash, delete_files))

        def close(self):
            self.closed = True

    downloader = Downloader()
    monkeypatch.setattr(benchmark_script, "_download_client", lambda *_args: ("aria2", downloader))

    video = tmp_path / "Stress Show - 03.mkv"
    video.write_bytes(b"video-bytes")
    download_calls = []

    def fake_download(_downloader, _release, **kwargs):
        download_calls.append(kwargs)
        return "abc", {3: video}

    monkeypatch.setattr(benchmark_script, "_download_release_videos", fake_download)

    def no_oracle(**_kwargs):
        raise benchmark_script.SubtitleBenchmarkError("no embedded Japanese text subtitle")

    monkeypatch.setattr(benchmark_script, "create_case_from_video", no_oracle)
    diagnostic_dir = tmp_path / "corpus" / "diagnostics" / "case"
    diagnostic_dir.mkdir(parents=True)
    diagnostic_calls = []

    def fake_diagnostic(**kwargs):
        diagnostic_calls.append(kwargs)
        return diagnostic_dir

    monkeypatch.setattr(benchmark_script, "create_diagnostic_case_from_video", fake_diagnostic)

    args = SimpleNamespace(
        config=tmp_path / "config.toml",
        corpus=tmp_path / "corpus",
        target_gb=0.1,
        anime_limit=1000,
        sort="popularity",
        seed=42,
        resolution="720p",
        backend="aria2",
        max_release_attempts=5,
        min_source_seeders=2,
        max_download_gb=2.0,
        download_timeout_minutes=30.0,
        metadata_timeout_seconds=120.0,
        stall_timeout_seconds=60.0,
        progress_interval_seconds=10.0,
        cache_dir=None,
        anilist_retries=6,
        retry_base_seconds=2.0,
        keep_videos=False,
    )

    assert benchmark_script.acquire_random_stress(args) == 0
    assert download_calls[0]["episodes"] == [3]
    assert download_calls[0]["identity_row"] == row
    assert download_calls[0]["probe_first"] is False
    assert diagnostic_calls[0]["media_id"] == 123
    assert diagnostic_calls[0]["episode"] == 3
    assert diagnostic_calls[0]["fetch_jimaku"] is True
    assert diagnostic_calls[0]["run_pudge"] is True
    assert downloader.deleted == [("abc", True)]
    assert downloader.closed is True

    events = [json.loads(line) for line in (args.corpus / "stress-events.jsonl").read_text().splitlines()]
    assert [row["event"] for row in events] == ["download_ok", "pipeline_ok"]
    assert events[0]["video_name"] == "Stress Show - 03.mkv"
    summary = json.loads((args.corpus / "stress-summary.json").read_text())
    assert summary["completed_cases"] == 1
    assert summary["failed_targets"] == 0


def test_stress_nyaa_discovery_sorts_by_seeders_and_records_rejections(monkeypatch) -> None:
    row = {
        "media_id": 1,
        "title": "Another",
        "titles": ["Another"],
        "synonyms": [],
        "episode": 4,
        "episodes": 12,
        "relations": [],
    }
    wrong = SimpleNamespace(
        title="[SubsPlease] 16bit Sensation - Another Layer - 04 (1080p)",
        info_hash="wrong",
        torrent_url="",
        link="",
        seeders=30,
        size_bytes=500_000_000,
        is_batch=False,
        score=0.0,
    )
    correct = SimpleNamespace(
        title="[Group] Another - 04 (1080p)",
        info_hash="correct",
        torrent_url="",
        link="",
        seeders=12,
        size_bytes=500_000_000,
        is_batch=False,
        score=0.0,
    )
    calls: list[tuple[str, str | None, str | None]] = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        def search(self, query: str, *, sort_by=None, order=None, **_kwargs):
            calls.append((query, sort_by, order))
            return [wrong, correct]

        def close(self):
            return None

    monkeypatch.setattr(benchmark_script, "NyaaClient", Client)
    monkeypatch.setattr(
        benchmark_script,
        "_stress_release_rejection_reason",
        lambda _row, release, **_kwargs: (
            "title_identity_mismatch" if release.info_hash == "wrong" else None
        ),
    )
    monkeypatch.setattr(
        benchmark_script,
        "_score_stress_release",
        lambda release, *_args, **_kwargs: release,
    )
    config = SimpleNamespace(
        nyaa=SimpleNamespace(
            base_url="https://example.invalid",
            proxy_mode="direct",
            proxy_url="",
            pre_search_command="",
            category="1_2",
            min_seeders=1,
        )
    )

    releases, diagnostics = benchmark_script._nyaa_ranked_for_stress(
        row,
        config,
        resolution="1080p",
        min_source_seeders=1,
    )

    assert [release.info_hash for release in releases] == ["correct"]
    assert len(calls) == 1
    assert all(sort_by == "seeders" and order == "desc" for _, sort_by, order in calls)
    assert "jpn" not in calls[0][0].casefold()
    assert diagnostics["reject_counts"] == {"title_identity_mismatch": 1}
    assert diagnostics["rejected_examples"][0]["title"] == wrong.title



def test_stress_library_anime_uses_plan_metadata_without_resetting_user_progress() -> None:
    row = {
        "media_id": 123,
        "title": "Canonical Stress Show",
        "titles": ["Canonical Stress Show", "Stress Show"],
        "synonyms": ["Stress Alias"],
        "episodes": 12,
        "format": "TV",
        "season_year": 2024,
        "status": "FINISHED",
        "duration": 24,
        "mean_score": 81,
        "relations": [],
    }
    existing = benchmark_script.LibraryAnime(
        media_id=123,
        title="[Group] Stress Show - 01 ~ 12 [1080p]",
        cover_url="https://img.invalid/cover.jpg",
        site_url="",
        status="CURRENT",
        progress=7,
        episodes=12,
        format="TV",
        user_score=9.0,
    )

    repaired = benchmark_script._stress_library_anime(row, existing)

    assert repaired.title == "Canonical Stress Show"
    assert repaired.site_url == "https://anilist.co/anime/123"
    assert repaired.status == "CURRENT"
    assert repaired.progress == 7
    assert repaired.user_score == 9.0
    assert repaired.cover_url == "https://img.invalid/cover.jpg"
    assert repaired.media_status == "FINISHED"
    assert repaired.mean_score == 81


def test_register_stress_library_video_is_idempotent_and_uses_known_media_episode(
    monkeypatch, tmp_path
) -> None:
    row = {
        "media_id": 123,
        "title": "Canonical Stress Show",
        "titles": ["Canonical Stress Show"],
        "synonyms": [],
        "episode": 3,
        "episodes": 12,
        "format": "TV",
        "status": "FINISHED",
        "relations": [],
    }
    video = tmp_path / "[Group] Stress Show - 103.mkv"
    video.write_bytes(b"video")

    class FakeDB:
        def __init__(self):
            self.anime = benchmark_script.LibraryAnime(
                media_id=123,
                title="[Group] bad torrent title",
                status="CURRENT",
                progress=2,
            )
            self.saved_anime = None
            self.saved_episode = None

        def get_anime(self, media_id):
            assert media_id == 123
            return self.anime

        def upsert_anime(self, anime):
            self.saved_anime = anime

        def episode_by_path(self, _path):
            return None

        def upsert_episode(self, episode, *, downloaded_at=None):
            self.saved_episode = (episode, downloaded_at)

    fake = FakeDB()
    monkeypatch.setattr(benchmark_script, "Database", lambda _path: fake)
    config = SimpleNamespace(library=SimpleNamespace(database_path=tmp_path / "library.sqlite3"))

    result = benchmark_script._register_stress_library_video(
        config, row, video, torrent_hash="abc"
    )

    assert fake.saved_anime.title == "Canonical Stress Show"
    assert fake.saved_anime.status == "CURRENT"
    episode, downloaded_at = fake.saved_episode
    assert episode.media_id == 123
    # The plan target is authoritative; a release may use absolute numbering.
    assert episode.media_episode == 3
    assert episode.release_episode == 103
    assert episode.title == "Canonical Stress Show"
    assert episode.torrent_hash == "abc"
    assert downloaded_at is not None
    assert result["media_id"] == 123


def test_repair_stress_library_drops_historical_false_target_mapping(
    monkeypatch, tmp_path
) -> None:
    corpus = tmp_path / "corpus"
    downloads = corpus / "downloads"
    downloads.mkdir(parents=True)
    video = downloads / "[SubsPlease] 16bit Sensation - Another Layer - 04 (1080p).mkv"
    video.write_bytes(b"video")
    event = {
        "event": "download_ok",
        "media_id": 1,
        "episode": 4,
        "video_name": video.name,
        "source_release": {"title": video.name, "info_hash": "bad"},
    }
    (corpus / "stress-events.jsonl").write_text(json.dumps(event) + "\n")
    row = {
        "media_id": 1,
        "title": "Another",
        "titles": ["Another"],
        "synonyms": [],
        "episode": 4,
        "episodes": 12,
        "format": "TV",
        "relations": [],
    }

    existing = benchmark_script.LibraryEpisode(
        media_id=1,
        title="Another",
        episode=4,
        video_path=video.resolve(),
    )

    class FakeDB:
        def __init__(self):
            self.deleted = []

        def episode_by_path(self, _path):
            return existing

        def delete_episode_record(self, path):
            self.deleted.append(Path(path).resolve())

    fake = FakeDB()
    monkeypatch.setattr(benchmark_script, "Database", lambda _path: fake)
    config = SimpleNamespace(library=SimpleNamespace(database_path=tmp_path / "library.sqlite3"))

    result = benchmark_script._repair_stress_library_registrations(
        corpus, [row], config
    )

    assert result["identity_rejected"] == 1
    assert result["stale_rows_removed"] == 1
    assert fake.deleted == [video.resolve()]



def test_repair_stress_library_audits_historical_target_missing_from_current_plan(
    monkeypatch, tmp_path
) -> None:
    corpus = tmp_path / "corpus"
    downloads = corpus / "downloads"
    downloads.mkdir(parents=True)
    video = downloads / "[Victor1139] Digimon Adventure - 01 [720p].mkv"
    video.write_bytes(b"video")
    event = {
        "event": "download_ok",
        "media_id": 3786,
        "episode": 1,
        "title": "Shin Evangelion Movie:||",
        "video_name": video.name,
        "source_release": {
            "title": "[Victor1139] Digimon Adventure (1999) [Episodes 01-13] [720p]",
            "info_hash": "digimon-not-eva",
        },
    }
    (corpus / "stress-events.jsonl").write_text(json.dumps(event) + "\n")

    existing = benchmark_script.LibraryEpisode(
        media_id=3786,
        title="Shin Evangelion Movie:||",
        episode=1,
        video_path=video.resolve(),
    )

    class FakeDB:
        def __init__(self):
            self.deleted = []

        def episode_by_path(self, _path):
            return existing

        def delete_episode_record(self, path):
            self.deleted.append(Path(path).resolve())

    fake = FakeDB()
    monkeypatch.setattr(benchmark_script, "Database", lambda _path: fake)
    config = SimpleNamespace(library=SimpleNamespace(database_path=tmp_path / "library.sqlite3"))

    # Current v102+ stress plans omit movies, but v101 event history must still
    # be audited rather than silently skipped.
    result = benchmark_script._repair_stress_library_registrations(corpus, [], config)

    assert result["identity_rejected"] == 1
    assert result["stale_rows_removed"] == 1
    assert result["invalid_targets"][0]["video_name"] == video.name
    assert fake.deleted == [video.resolve()]


def test_repair_stress_library_validates_selected_video_not_only_release_title(
    monkeypatch, tmp_path
) -> None:
    corpus = tmp_path / "corpus"
    downloads = corpus / "downloads"
    downloads.mkdir(parents=True)
    video = downloads / "Grisaia no Kajitsu - 09.mkv"
    video.write_bytes(b"video")
    event = {
        "event": "download_ok",
        "media_id": 21006,
        "episode": 9,
        "title": "Grisaia no Rakuen",
        "video_name": video.name,
        "source_release": {"title": "Grisaia Complete Collection", "info_hash": "franchise-pack"},
    }
    (corpus / "stress-events.jsonl").write_text(json.dumps(event) + "\n")
    row = {
        "media_id": 21006,
        "title": "Grisaia no Rakuen",
        "titles": ["Grisaia no Rakuen"],
        "synonyms": [],
        "episode": 9,
        "episodes": 10,
        "format": "TV",
        "relations": [
            {"relation_type": "PREQUEL", "media_id": 17729, "title": "Grisaia no Kajitsu"}
        ],
    }

    class FakeDB:
        def episode_by_path(self, _path):
            return None

    monkeypatch.setattr(benchmark_script, "Database", lambda _path: FakeDB())
    monkeypatch.setattr(benchmark_script, "_source_special_mismatch", lambda *_args: False)
    monkeypatch.setattr(benchmark_script, "_stress_source_identity_mismatch_reason", lambda *_args: None)
    monkeypatch.setattr(
        benchmark_script,
        "_stress_release_title_is_plausible",
        lambda _row, title, **_kwargs: "kajitsu" not in title.casefold(),
    )
    config = SimpleNamespace(library=SimpleNamespace(database_path=tmp_path / "library.sqlite3"))

    result = benchmark_script._repair_stress_library_registrations(corpus, [row], config)

    assert result["identity_rejected"] == 1
    assert result["registered"] == 0


def test_repair_stress_library_registers_valid_download_event(
    monkeypatch, tmp_path
) -> None:
    corpus = tmp_path / "corpus"
    downloads = corpus / "downloads"
    downloads.mkdir(parents=True)
    video = downloads / "[SubsPlease] Buddy Daddies - 07 (1080p).mkv"
    video.write_bytes(b"video")
    event = {
        "event": "download_ok",
        "media_id": 155907,
        "episode": 7,
        "video_name": video.name,
        "source_release": {"title": video.name, "info_hash": "good"},
    }
    (corpus / "stress-events.jsonl").write_text(json.dumps(event) + "\n")
    row = {
        "media_id": 155907,
        "title": "Buddy Daddies",
        "titles": ["Buddy Daddies"],
        "synonyms": [],
        "episode": 7,
        "episodes": 12,
        "format": "TV",
        "relations": [],
    }

    class FakeDB:
        def episode_by_path(self, _path):
            return None

    monkeypatch.setattr(benchmark_script, "Database", lambda _path: FakeDB())
    calls = []
    monkeypatch.setattr(
        benchmark_script,
        "_register_stress_library_video",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {},
    )
    config = SimpleNamespace(library=SimpleNamespace(database_path=tmp_path / "library.sqlite3"))

    result = benchmark_script._repair_stress_library_registrations(
        corpus, [row], config
    )

    assert result["registered"] == 1
    assert len(calls) == 1
    assert calls[0][0][1] == row
    assert calls[0][0][2] == video.resolve()



def _v105_identity_row(
    *,
    media_id: int = 1,
    title: str,
    episode: int,
    titles: list[str] | None = None,
    synonyms: list[str] | None = None,
    relations: list[dict] | None = None,
    season_year: int | None = None,
) -> dict:
    return {
        "media_id": media_id,
        "title": title,
        "titles": titles or [title],
        "synonyms": synonyms or [],
        "episode": episode,
        "episodes": 24,
        "format": "TV",
        "relations": relations or [],
        "season_year": season_year,
    }


def test_v105_selected_video_allows_translated_alias_when_source_is_valid(monkeypatch) -> None:
    row = _v105_identity_row(
        media_id=140596,
        title="Ijiranaide, Nagatoro-san 2nd Attack",
        episode=12,
        titles=["Ijiranaide, Nagatoro-san 2nd Attack", "Don't Toy with Me, Miss Nagatoro 2nd Attack"],
    )
    source = (
        "[Trix] Don't Toy with Me Miss Nagatoro - S02 (COMPLETE) [Multi Subs] "
        "(720p AV1) (Batch) - Ijiranaide, Nagatoro-san 2nd Attack (VOSTFR)"
    )
    video = "[Trix] Don't Toy with Me Miss Nagatoro - S02E12 - (720p AV1 AAC)[Multi Subs].mkv"

    assert benchmark_script._stress_selected_video_rejection_reason(
        row, source_title=source, video_name=video
    ) is None


def test_v105_selected_video_allows_episode_title_only_basename(monkeypatch) -> None:
    row = _v105_identity_row(
        media_id=146210,
        title="Kinsou no Vermeil: Gakeppuchi Majutsushi wa Saikyou no Yakusai to Mahou Sekai wo Tsukisusumu",
        episode=1,
    )
    source = "Vermeil in Gold Season 1 [1080p] [Batch]"
    video = "S01E01-The Desperate Magician and the Imprisoned Disaster [9E1A483D].mkv"
    monkeypatch.setattr(
        benchmark_script,
        "_stress_release_title_is_plausible",
        lambda _row, title, **_kwargs: title == source,
    )
    monkeypatch.setattr(
        benchmark_script,
        "_stress_source_identity_mismatch_reason",
        lambda *_args, **_kwargs: None,
    )

    assert benchmark_script._stress_selected_video_rejection_reason(
        row, source_title=source, video_name=video
    ) is None


def test_v105_selected_video_rejects_wrong_explicit_episode_even_if_title_alias_matches(monkeypatch) -> None:
    row = _v105_identity_row(
        media_id=116867,
        title="Itai no wa Iya nano de Bougyoryoku ni Kyokufuri Shitai to Omoimasu. 2",
        episode=2,
    )
    source = (
        "[Yameii] Itai no wa Iya nano de Bougyoryoku ni Kyokufuri Shitai to Omoimasu 2 - 05 | "
        "BOFURI - I Don't Want to Get Hurt, so I'll Max Out My Defense S2 [English Dub]"
    )
    monkeypatch.setattr(
        benchmark_script,
        "_stress_release_title_is_plausible",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        benchmark_script,
        "_stress_source_identity_mismatch_reason",
        lambda *_args, **_kwargs: None,
    )

    assert benchmark_script._stress_selected_video_rejection_reason(
        row,
        source_title=source,
        video_name="[Yameii] BOFURI S2 - 05.mkv",
    ) == "source_episode_mismatch"


def test_v105_selected_video_rejects_known_sibling_inside_franchise_pack(monkeypatch) -> None:
    row = _v105_identity_row(
        media_id=21006,
        title="Grisaia no Rakuen",
        episode=9,
        relations=[
            {"relation_type": "PREQUEL", "media_id": 17729, "title": "Grisaia no Kajitsu"}
        ],
    )
    source = "[DB] Grisaia (Complete Series+Specials+Movies) (Grisaia no Kajitsu+Grisaia no Rakuen)"
    negatives = ("Grisaia no Kajitsu",)
    monkeypatch.setattr(
        benchmark_script,
        "_stress_release_title_is_plausible",
        lambda _row, title, **_kwargs: title == source,
    )
    monkeypatch.setattr(
        benchmark_script,
        "_stress_source_identity_mismatch_reason",
        lambda *_args, **_kwargs: None,
    )

    assert benchmark_script._stress_selected_video_rejection_reason(
        row,
        source_title=source,
        video_name="[DB]Grisaia no Kajitsu_-_09_(10bit_BD1080p_x265).mkv",
        negative_titles=negatives,
    ) == "video_known_sibling_title"


def test_v105_selected_video_keeps_correct_bnha_s6_filename_when_source_confirms_season(monkeypatch) -> None:
    row = _v105_identity_row(
        media_id=139630,
        title="Boku no Hero Academia 6",
        episode=15,
        relations=[
            {"relation_type": "PREQUEL", "media_id": 21459, "title": "Boku no Hero Academia"}
        ],
    )
    source = "[Anime Time] Boku no Hero Academia (Season 6) [1080p] (My Hero Academia Season 6)"
    video = "[Anime Time] Boku no Hero Academia - S06E15 (EP 128) [1080p].mkv"
    monkeypatch.setattr(
        benchmark_script,
        "_stress_release_title_is_plausible",
        lambda _row, title, **_kwargs: title == source,
    )
    monkeypatch.setattr(
        benchmark_script,
        "_stress_source_identity_mismatch_reason",
        lambda *_args, **_kwargs: None,
    )

    assert benchmark_script._stress_selected_video_rejection_reason(
        row,
        source_title=source,
        video_name=video,
        negative_titles=("Boku no Hero Academia",),
    ) is None


def test_v105_repair_restores_row_removed_by_v104_false_positive(monkeypatch, tmp_path) -> None:
    corpus = tmp_path / "corpus"
    downloads = corpus / "downloads"
    downloads.mkdir(parents=True)
    video = downloads / "[Trix] Don't Toy with Me Miss Nagatoro - S02E12 - (720p AV1 AAC)[Multi Subs].mkv"
    video.write_bytes(b"video")
    source = (
        "[Trix] Don't Toy with Me Miss Nagatoro - S02 (COMPLETE) [Multi Subs] "
        "(720p AV1) (Batch) - Ijiranaide, Nagatoro-san 2nd Attack (VOSTFR)"
    )
    event = {
        "event": "download_ok",
        "media_id": 140596,
        "episode": 12,
        "title": "Ijiranaide, Nagatoro-san 2nd Attack",
        "video_name": video.name,
        "source_release": {"title": source, "info_hash": "good"},
    }
    (corpus / "stress-events.jsonl").write_text(json.dumps(event) + "\n")
    row = _v105_identity_row(
        media_id=140596,
        title="Ijiranaide, Nagatoro-san 2nd Attack",
        episode=12,
        titles=["Ijiranaide, Nagatoro-san 2nd Attack", "Don't Toy with Me, Miss Nagatoro 2nd Attack"],
    )

    class FakeDB:
        def episode_by_path(self, _path):
            return None

    monkeypatch.setattr(benchmark_script, "Database", lambda _path: FakeDB())
    calls = []
    monkeypatch.setattr(
        benchmark_script,
        "_register_stress_library_video",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {},
    )
    config = SimpleNamespace(library=SimpleNamespace(database_path=tmp_path / "library.sqlite3"))

    result = benchmark_script._repair_stress_library_registrations(corpus, [row], config)

    assert result["identity_rejected"] == 0
    assert result["registered"] == 1
    assert len(calls) == 1


def test_v105_r2_source_season_marker_must_match_target_season(monkeypatch) -> None:
    season_two = _v105_identity_row(
        media_id=140596,
        title="Ijiranaide, Nagatoro-san 2nd Attack",
        episode=12,
        titles=["Ijiranaide, Nagatoro-san 2nd Attack", "Don't Toy with Me, Miss Nagatoro 2nd Attack"],
    )
    season_one = _v105_identity_row(
        media_id=21459,
        title="Boku no Hero Academia",
        episode=5,
    )
    monkeypatch.setattr(
        benchmark_script,
        "_source_identity_mismatch_reason",
        lambda *_args, **_kwargs: "cross_season_source_mismatch",
    )
    monkeypatch.setattr(
        benchmark_script,
        "_stress_release_title_is_plausible",
        lambda *_args, **_kwargs: True,
    )

    assert benchmark_script._stress_source_identity_mismatch_reason(
        season_two,
        "Ijiranaide, Nagatoro-san 2nd Attack S02 batch",
    ) is None
    assert benchmark_script._stress_source_identity_mismatch_reason(
        season_one,
        "Boku no Hero Academia Season 2 - 05",
    ) == "cross_season_source_mismatch"


def test_v105_r4_safe_cross_season_override_is_not_rejected_twice(monkeypatch) -> None:
    row = _v105_identity_row(
        media_id=140596,
        title="Ijiranaide, Nagatoro-san 2nd Attack",
        episode=12,
        titles=[
            "Ijiranaide, Nagatoro-san 2nd Attack",
            "Don't Toy with Me, Miss Nagatoro 2nd Attack",
        ],
    )
    source = (
        "[Trix] Don't Toy with Me Miss Nagatoro - S02 (COMPLETE) [Multi Subs] "
        "(720p AV1) (Batch) - Ijiranaide, Nagatoro-san 2nd Attack (VOSTFR)"
    )
    video = "[Trix] Don't Toy with Me Miss Nagatoro - S02E12 - (720p AV1 AAC)[Multi Subs].mkv"

    # Reproduce the exact r3 failure shape: production rejects S02, the stress
    # title+season override accepts it, then production plausibility still says no.
    monkeypatch.setattr(
        benchmark_script,
        "_source_identity_mismatch_reason",
        lambda *_args, **_kwargs: "cross_season_source_mismatch",
    )
    monkeypatch.setattr(
        benchmark_script,
        "_stress_source_identity_mismatch_reason",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        benchmark_script,
        "_stress_release_title_is_plausible",
        lambda *_args, **_kwargs: False,
    )

    assert benchmark_script._stress_selected_video_rejection_reason(
        row, source_title=source, video_name=video
    ) is None


def test_v105_r2_single_episode_beats_false_title_number_range() -> None:
    assert benchmark_script._stress_explicit_single_episode(
        "[Yameii] BOFURI 2 - 05 | BOFURI S2 [English Dub]"
    ) == 5
    assert benchmark_script._stress_explicit_single_episode(
        "[Erai-raws] Show - 01 ~ 12 [Batch]"
    ) is None


def test_v106_parser_accepts_replay_stress() -> None:
    args = benchmark_script._parser().parse_args(
        ["replay-stress", "--corpus", "/tmp/corpus", "--start-index", "2", "--limit", "3"]
    )
    assert args.command == "replay-stress"
    assert args.corpus == Path("/tmp/corpus")
    assert args.start_index == 2
    assert args.limit == 3


def test_v106_replay_stress_skips_identity_rejected_reports(monkeypatch, tmp_path) -> None:
    corpus = tmp_path / "corpus"
    valid_dir = corpus / "diagnostics" / "valid"
    invalid_dir = corpus / "diagnostics" / "invalid"
    valid_dir.mkdir(parents=True)
    invalid_dir.mkdir(parents=True)
    valid_report = valid_dir / "diagnostic.json"
    invalid_report = invalid_dir / "diagnostic.json"
    valid_report.write_text(json.dumps({"media_id": 1, "episode": 2, "title": "Valid"}))
    invalid_report.write_text(json.dumps({"media_id": 3, "episode": 4, "title": "Invalid"}))
    (corpus / "stress-library-repair.json").write_text(
        json.dumps({"invalid_targets": [{"media_id": 3, "episode": 4}]})
    )

    calls = []
    monkeypatch.setattr(benchmark_script, "load_config", lambda _path: SimpleNamespace())
    monkeypatch.setattr(
        benchmark_script,
        "replay_stored_benchmark_case",
        lambda path, **_kwargs: calls.append(Path(path)) or {
            "report_path": str(path),
            "status": "replayed",
            "accepted": True,
            "candidate_count": 1,
        },
    )
    monkeypatch.setattr(benchmark_script, "_append_stress_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(benchmark_script, "_write_corpus_summary", lambda *_args, **_kwargs: None)
    args = SimpleNamespace(
        config=tmp_path / "config.toml",
        corpus=corpus,
        start_index=0,
        limit=0,
    )
    assert benchmark_script.replay_stress(args) == 0
    assert calls == [valid_report]
    summary = json.loads((corpus / "stress-replay-v106.json").read_text())
    assert summary["invalid_skipped"] == 1
    assert summary["replayed"] == 1
    assert summary["errors"] == 0


def test_v106_replay_stress_counts_stored_pipeline_error(monkeypatch, tmp_path) -> None:
    corpus = tmp_path / "corpus"
    report_dir = corpus / "diagnostics" / "one"
    report_dir.mkdir(parents=True)
    report = report_dir / "diagnostic.json"
    report.write_text(json.dumps({"media_id": 1, "episode": 1, "title": "One"}))
    monkeypatch.setattr(benchmark_script, "load_config", lambda _path: SimpleNamespace())
    monkeypatch.setattr(
        benchmark_script,
        "replay_stored_benchmark_case",
        lambda path, **_kwargs: {
            "report_path": str(path),
            "status": "error",
            "accepted": None,
            "candidate_count": 1,
            "error": "boom",
        },
    )
    monkeypatch.setattr(benchmark_script, "_append_stress_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(benchmark_script, "_write_corpus_summary", lambda *_args, **_kwargs: None)
    args = SimpleNamespace(config=tmp_path / "config.toml", corpus=corpus, start_index=0, limit=0)
    assert benchmark_script.replay_stress(args) == 1
    summary = json.loads((corpus / "stress-replay-v106.json").read_text())
    assert summary["replayed"] == 0
    assert summary["errors"] == 1


def test_v107_rejects_bnha_vigilantes_spinoff_from_source_and_filename() -> None:
    row = _v105_identity_row(
        media_id=21459,
        title="Boku no Hero Academia",
        episode=5,
        titles=["Boku no Hero Academia", "My Hero Academia"],
        relations=[
            {
                "relation_type": "SPIN_OFF",
                "media_id": 185736,
                "format": "TV",
                "title": "Vigilante: Boku no Hero Academia ILLEGALS",
            }
        ],
    )
    negatives = benchmark_script._plan_negative_titles_by_media([row])[21459]
    assert benchmark_script._stress_selected_video_rejection_reason(
        row,
        source_title=(
            "[Judas] Boku no Hero Academia Illegals (My Hero Academia Vigilantes) "
            "- S01E05 [1080p]"
        ),
        video_name="[Judas] Vigilantes - S01E05.mkv",
        negative_titles=negatives,
    ) == "video_known_sibling_title"


def test_v107_rejects_plural_special_selected_file_but_not_mixed_pack() -> None:
    row = _v105_identity_row(
        media_id=20829,
        title="Owari no Seraph",
        episode=9,
        titles=["Owari no Seraph", "Seraph of the End: Vampire Reign"],
    )
    assert benchmark_script._stress_selected_video_rejection_reason(
        row,
        source_title="[Mori] Owari no Seraph complete + specials batch",
        video_name="[Mori] Owari no Seraph Specials - Owaranai Seraph - 09.mkv",
    ) == "video_special_source_mismatch"

    violet = _v105_identity_row(
        media_id=21827,
        title="Violet Evergarden",
        episode=13,
        titles=["Violet Evergarden"],
    )
    assert benchmark_script._stress_selected_video_rejection_reason(
        violet,
        source_title="[Erai-raws] Violet Evergarden - 01 ~ 14 (Plus Specials) [BATCH]",
        video_name="[Erai-raws] Violet Evergarden - 13 [720p].mkv",
    ) is None


@pytest.mark.parametrize(
    ("row", "source", "video"),
    [
        (
            _v105_identity_row(
                media_id=8525,
                title="Kami nomi zo Shiru Sekai",
                episode=11,
                titles=["Kami nomi zo Shiru Sekai", "The World God Only Knows"],
            ),
            "The World God Only Knows - Complete [Blu-ray]",
            "The World God Only Knows - Goddesses Arc - 11.mkv",
        ),
        (
            _v105_identity_row(
                media_id=45,
                title="Rurouni Kenshin: Meiji Kenkaku Romantan",
                episode=1,
                titles=["Rurouni Kenshin: Meiji Kenkaku Romantan", "Rurouni Kenshin"],
            ),
            "Rurouni Kenshin - Kyoto Douran - 01",
            "Rurouni Kenshin - Kyoto Douran - 01.mkv",
        ),
        (
            _v105_identity_row(
                media_id=2025,
                title="DARKER THAN BLACK: Kuro no Keiyakusha",
                episode=10,
                titles=["DARKER THAN BLACK: Kuro no Keiyakusha", "Darker than Black"],
            ),
            "Darker than Black complete",
            "Darker than Black - Ryuusei no Gemini - 10.mkv",
        ),
    ],
)
def test_v107_rejects_strict_target_title_extensions(row, source, video) -> None:
    assert benchmark_script._stress_selected_video_rejection_reason(
        row, source_title=source, video_name=video
    ) == "video_target_title_extension"


def test_v107_keeps_target_year_parenthetical_translation_and_episode_title_basename() -> None:
    hunter = _v105_identity_row(
        media_id=136,
        title="HUNTER×HUNTER",
        episode=55,
        titles=["HUNTER×HUNTER", "Hunter x Hunter"],
        season_year=1999,
        relations=[
            {
                "relation_type": "ALTERNATIVE",
                "media_id": 11061,
                "format": "TV",
                "title": "HUNTER×HUNTER (2011)",
            }
        ],
    )
    negatives = benchmark_script._plan_negative_titles_by_media([hunter])[136]
    assert benchmark_script._stress_selected_video_rejection_reason(
        hunter,
        source_title="Hunter x Hunter (1999) Season 1 batch",
        video_name="Hunter x Hunter (1999) - S01E55.mkv",
        negative_titles=negatives,
    ) is None

    gate = _v105_identity_row(
        media_id=20994,
        title="GATE: Jieitai Kanochi nite, Kaku Tatakaeri",
        episode=12,
        titles=["GATE: Jieitai Kanochi nite, Kaku Tatakaeri", "Gate"],
        season_year=2015,
    )
    assert benchmark_script._stress_selected_video_rejection_reason(
        gate,
        source_title=(
            "Gate Jieitai Kanochi nite, Kaku Tatakaeri "
            "(Gate Thus the JSDF Fought There!) Complete"
        ),
        video_name=(
            "Gate Jieitai Kanochi nite, Kaku Tatakaeri "
            "(Gate Thus the JSDF Fought There!) - S01E12 - Itami Might.mkv"
        ),
    ) is None

    beast = _v105_identity_row(
        media_id=150695,
        title="Yuusha Party wo Tsuihou Sareta Beast Tamer, Saikyoushu no Nekomimi Shoujo to Deau",
        episode=9,
        titles=[
            "Yuusha Party wo Tsuihou Sareta Beast Tamer, Saikyoushu no Nekomimi Shoujo to Deau",
            "Beast Tamer",
        ],
    )
    assert benchmark_script._stress_selected_video_rejection_reason(
        beast,
        source_title="Beast Tamer Season 1 batch",
        video_name="S01E09-Beast Tamer VS Beast Tamer.mkv",
    ) is None


def test_v107_replay_reaudits_current_identity_instead_of_trusting_stale_repair(monkeypatch, tmp_path) -> None:
    corpus = tmp_path / "corpus"
    downloads = corpus / "downloads"
    report_dir = corpus / "diagnostics" / "bad"
    downloads.mkdir(parents=True)
    report_dir.mkdir(parents=True)

    row = _v105_identity_row(
        media_id=21459,
        title="Boku no Hero Academia",
        episode=5,
        titles=["Boku no Hero Academia", "My Hero Academia"],
        relations=[
            {
                "relation_type": "SPIN_OFF",
                "media_id": 185736,
                "format": "TV",
                "title": "Vigilante: Boku no Hero Academia ILLEGALS",
            }
        ],
    )
    (corpus / "stress-plan.json").write_text(json.dumps({"episodes": [row]}))
    (corpus / "stress-events.jsonl").write_text(
        json.dumps(
            {
                "event": "download_ok",
                "media_id": 21459,
                "episode": 5,
                "title": "Boku no Hero Academia",
                "video_name": "[Judas] Vigilantes - S01E05.mkv",
                "source_release": {
                    "title": "Boku no Hero Academia Illegals (My Hero Academia Vigilantes) - S01E05"
                },
            }
        )
        + "\n"
    )
    # Deliberately stale previous repair: v107 replay must not trust it alone.
    (corpus / "stress-library-repair.json").write_text(json.dumps({"invalid_targets": []}))
    report = report_dir / "diagnostic.json"
    report.write_text(json.dumps({"media_id": 21459, "episode": 5, "title": "Boku no Hero Academia"}))

    calls = []
    monkeypatch.setattr(benchmark_script, "load_config", lambda _path: SimpleNamespace())
    monkeypatch.setattr(
        benchmark_script,
        "replay_stored_benchmark_case",
        lambda path, **_kwargs: calls.append(path) or {"status": "replayed", "accepted": True},
    )
    monkeypatch.setattr(benchmark_script, "_append_stress_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(benchmark_script, "_write_corpus_summary", lambda *_args, **_kwargs: None)

    args = SimpleNamespace(config=tmp_path / "config.toml", corpus=corpus, start_index=0, limit=0)
    assert benchmark_script.replay_stress(args) == 0
    assert calls == []
    audit = json.loads((corpus / "stress-identity-audit-v107.json").read_text())
    assert audit["invalid_targets"][0]["reason"] == "video_known_sibling_title"
    summary = json.loads((corpus / "stress-replay-v107.json").read_text())
    assert summary["invalid_known"] == 1
    assert summary["invalid_skipped"] == 1
    assert summary["errors"] == 0
