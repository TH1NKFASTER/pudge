"""Identify… never gives one episode two files (wave-4 review of SCEN-6).

Baccano without a related OVA entry: "S00E02" is set aside (Action required) because
"S01E02" is episode 2. Identify… could still name it TV episode 2, and since the stored
choice runs before the season-0 checks, every rescan kept both files on (2251, 2).
Now Identify refuses an episode another existing file holds (another number can be picked),
and when a file arrives later that a scan would file on an identified episode, the person's
file keeps it and the new copy is set aside as usual (Delete / Keep)."""

from __future__ import annotations

import json
from pathlib import Path

from pudge import extra_files
from pudge.manager_models import LibraryAnime
from test_web_app import make_api
from tests.i18n_support import en, ru

BACCANO = 2251


def _baccano(tmp_path: Path, *names: str):
    api = make_api(tmp_path)
    api.manager.db.upsert_anime(
        LibraryAnime(
            media_id=BACCANO,
            title="Baccano!",
            status="COMPLETED",
            media_status="FINISHED",
            episodes=16,
            format="TV",
        )
    )
    folder = api.config.library.root_dir / "Baccano!"
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_bytes(b"video")
    api.manager._scan_library_uncached()
    return api, folder


def _holders(api, episode: int) -> list[str]:
    return sorted(row.video_path.name for row in api.manager.db.episode_rows(BACCANO, episode))


def test_identify_refuses_an_episode_another_file_holds(tmp_path: Path) -> None:
    api, folder = _baccano(tmp_path, "Baccano! - S01E02.mkv", "Baccano! - S00E02.mkv")
    special = (folder / "Baccano! - S00E02.mkv").resolve()
    row = api.manager.db.episode_by_path(special)
    assert (row.media_id, row.episode) == (BACCANO, None)  # set aside: Action required
    assert _holders(api, 2) == ["Baccano! - S01E02.mkv"]

    refused = api.identify_episode_file(str(special), BACCANO, 2)
    assert refused == {"ok": False, "error": en("error.identify.episodeTaken", title="Baccano!", episode=2)}
    assert api.manager.db.episode_by_path(special).episode is None
    # A free number is accepted, and rescans keep one file per episode.
    assert api.identify_episode_file(str(special), BACCANO, 14)["ok"] is True
    for _ in range(2):
        api.manager._scan_library_uncached()
        assert _holders(api, 2) == ["Baccano! - S01E02.mkv"]
        assert _holders(api, 14) == ["Baccano! - S00E02.mkv"]
    assert (
        ru("error.identify.episodeTaken", title="Baccano!", episode=2)
        == "Серия 2 «Baccano!» уже есть в другом файле"
    )


def test_a_later_copy_of_an_identified_episode_is_set_aside(tmp_path: Path) -> None:
    api, folder = _baccano(tmp_path, "Baccano! - S00E02.mkv")
    special = (folder / "Baccano! - S00E02.mkv").resolve()
    assert api.identify_episode_file(str(special), BACCANO, 2)["ok"] is True

    regular = folder / "Baccano! - S01E02.mkv"  # arrives later
    regular.write_bytes(b"video")
    regular = regular.resolve()
    for _ in range(3):
        api.manager._scan_library_uncached()
        assert _holders(api, 2) == ["Baccano! - S00E02.mkv"]  # the person's file keeps it
        later = api.manager.db.episode_by_path(regular)
        assert (later.media_id, later.episode) == (BACCANO, None)
        assert api.manager.db.get_state(extra_files.set_aside_state_key(regular), "") == str(BACCANO)
        jobs = {row["video_path"]: dict(row) for row in api.manager.db.subtitle_jobs()}
        assert jobs[str(regular)]["state"] == "needs_action"


def test_the_window_shows_the_refusal_and_lets_another_number_be_picked(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            await openIdentifyFile('/lib/Baccano! - S00E02.mkv', 'Baccano!', null);
            await __input('#identifyEpisode', '2');
            await __click('[data-action="identify-pick"][data-index="0"]'); await __flush();
            const out = {toast: __toasts().at(-1), stillOpen: !!document.getElementById('identifyEpisode'),
                         expected: PudgeI18n.message(window.taken)};
            await __input('#identifyEpisode', '14');
            await __click('[data-action="identify-pick"][data-index="0"]'); await __flush(); await __tick(20);
            out.done = __toasts().at(-1);
            out.doneExpected = PudgeI18n.t('toast.fileIdentified', {title: 'Baccano!'});
            return out;
            """,
            lang=lang,
            before=(
                "window.taken = "
                + json.dumps(en("error.identify.episodeTaken", title="Baccano!", episode=2))
                + ";"
                "__api.identify_episode_search = async () => ({rows: [{media_id: 2251, title: 'Baccano!', format: 'TV',"
                " episodes: 16, in_library: true}], error: ''});"
                "__api.identify_episode_file = async (path, id, episode) => episode === 2"
                " ? {ok: false, error: window.taken}"
                " : {ok: true, media_id: 2251, episode, title: 'Baccano!', state: ui.state};"
                "__api.diagnose_video_file = async () => ({media_id: 2251, title: 'Baccano!', episode: 14, ready: false,"
                " video_path: '/lib/Baccano! - S00E02.mkv', state: 'local', identify_allowed: true, checks: []});"
            ),
        )
        out = result.value
        assert out["toast"] == out["expected"] and out["stillOpen"] is True
        assert out["done"] == out["doneExpected"]
        assert [call[2] for call in result.calls_of("identify_episode_file")] == [2, 14]
        if lang == "ru":
            assert out["toast"] == ru("error.identify.episodeTaken", title="Baccano!", episode=2)
