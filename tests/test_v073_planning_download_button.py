from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from pudge.manager_models import LibraryAnime, LibraryEpisode
from pudge.web_app import WebAppApi
from test_web_app import make_api


def _anime(*, media_status: str = "FINISHED", released: int = 3):
    return SimpleNamespace(
        media_id=99,
        title="Planning Test",
        format="TV",
        media_status=media_status,
        episodes=3,
        released_episodes=released,
    )


def _api(tmp_path: Path, episodes=None):
    rows = list(episodes or [])
    manager = SimpleNamespace(
        db=SimpleNamespace(
            downloads=lambda: [],
            episodes=lambda _media_id=None: rows,
        ),
        incomplete_download_paths=lambda: (),
        _path_within=lambda _path, _roots: False,
    )
    api = object.__new__(WebAppApi)
    api.manager = manager
    api._planning_episode_download_state = {
        "running": False,
        "media_id": None,
    }
    return api


def _episode(tmp_path: Path, number: int):
    path = tmp_path / f"{number:02d}.mkv"
    path.write_bytes(b"x")
    return SimpleNamespace(video_path=path, episode=number)


def test_planning_download_button_hidden_while_job_is_running(tmp_path: Path) -> None:
    api = _api(tmp_path)
    api._planning_episode_download_state = {"running": True, "media_id": 99}

    assert api._planning_download_button_hidden(_anime(), downloads=[]) is True


def test_planning_download_button_hidden_for_active_managed_torrent(tmp_path: Path) -> None:
    api = _api(tmp_path)
    downloads = [
        SimpleNamespace(media_id=99, state="downloading", progress=0.12),
    ]

    assert api._planning_download_button_hidden(_anime(), downloads=downloads) is True


def test_planning_download_button_remains_for_stalled_torrent(tmp_path: Path) -> None:
    api = _api(tmp_path)
    downloads = [
        SimpleNamespace(media_id=99, state="stalledDL", progress=0.0),
    ]

    assert api._planning_download_button_hidden(_anime(), downloads=downloads) is False


def test_planning_download_button_hidden_for_completed_torrent(tmp_path: Path) -> None:
    api = _api(tmp_path)
    downloads = [
        SimpleNamespace(media_id=99, state="stalledUP", progress=1.0),
    ]

    assert api._planning_download_button_hidden(_anime(), downloads=downloads) is True


def test_planning_download_button_not_hidden_for_broken_torrent(tmp_path: Path) -> None:
    api = _api(tmp_path)
    downloads = [
        SimpleNamespace(media_id=99, state="missingFiles", progress=0.0),
    ]

    assert api._planning_download_button_hidden(_anime(), downloads=downloads) is False


def test_planning_download_button_hidden_when_all_released_episodes_are_local(
    tmp_path: Path,
) -> None:
    api = _api(
        tmp_path,
        [_episode(tmp_path, 1), _episode(tmp_path, 2), _episode(tmp_path, 3)],
    )

    assert api._planning_download_button_hidden(_anime(), downloads=[]) is True


def test_planning_download_button_remains_for_partial_local_set(tmp_path: Path) -> None:
    api = _api(
        tmp_path,
        [_episode(tmp_path, 1), _episode(tmp_path, 2)],
    )

    assert api._planning_download_button_hidden(_anime(), downloads=[]) is False


def test_releasing_planning_title_only_requires_currently_released_episodes(
    tmp_path: Path,
) -> None:
    api = _api(
        tmp_path,
        [_episode(tmp_path, 1), _episode(tmp_path, 2)],
    )

    assert (
        api._planning_download_button_hidden(
            _anime(media_status="RELEASING", released=2),
            downloads=[],
        )
        is True
    )


def test_planning_page_offers_auto_download_only_for_titles_not_yet_local(page, tmp_path: Path) -> None:
    # The real backend state: "Local Show" has all three episodes on disk, "Remote Show" none.
    api = make_api(tmp_path)
    for media_id, title in ((98, "Remote Show"), (99, "Local Show")):
        api.manager.db.upsert_anime(
            LibraryAnime(
                media_id=media_id,
                title=title,
                status="PLANNING",
                episodes=3,
                media_status="FINISHED",
                format="TV",
            )
        )
    folder = api.config.library.root_dir / "Local Show"
    folder.mkdir(parents=True)
    for number in (1, 2, 3):
        video = folder / f"Local Show - {number:02d}.mkv"
        video.write_bytes(b"video")
        api.manager.db.upsert_episode(
            LibraryEpisode(media_id=99, title="Local Show", episode=number, video_path=video, state="local")
        )
    state = json.loads(json.dumps(api.get_state(), default=str))

    for lang in ("en", "ru"):
        result = page.run(
            r"""
            setPage('planned', true); await __tick(50);
            const buttons = Object.fromEntries([...document.querySelectorAll('[data-planned="1"][data-media-id]')].map(card =>
              [card.dataset.mediaId, card.querySelector('[data-action="planning-episodes-auto-card"]')?.innerText ?? null]));
            await __click('[data-media-id="98"] [data-action="planning-episodes-auto-card"]');
            return {buttons, label: PudgeI18n.t('ui.autoDownloadEpisodes')};
            """,
            lang=lang,
            state=state,
            api={"start_planning_episode_download": {"running": True, "media_id": 98, "episodes": []}},
        )
        assert result.value["buttons"] == {"98": result.value["label"], "99": None}
        assert result.calls_of("start_planning_episode_download") == [[98]]
