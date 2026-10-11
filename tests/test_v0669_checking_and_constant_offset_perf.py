from pathlib import Path

from pudge import syncing


def _write_srt(path: Path, starts: list[float]) -> None:
    rows = []
    for index, start in enumerate(starts, start=1):
        end = start + 1.1 + (index % 3) * 0.17

        def stamp(value: float) -> str:
            milliseconds = int(round(value * 1000))
            hours, milliseconds = divmod(milliseconds, 3_600_000)
            minutes, milliseconds = divmod(milliseconds, 60_000)
            seconds, milliseconds = divmod(milliseconds, 1000)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

        rows.append(f"{index}\n{stamp(start)} --> {stamp(end)}\n字幕 {index}\n")
    path.write_text("\n".join(rows), encoding="utf-8")


# The harness clock starts at 1790856000 (2026-10-01 12:00 UTC).
FUTURE_JOB = {
    "video": "Show - 03.mkv",
    "video_path": "/v/Show - 03.mkv",
    "state": "pending",
    "priority": 200,
    "next_check": 1790856000 + 3600,
    "attempts": 1,
}


def test_future_priority_subtitle_jobs_do_not_keep_checking_ui_active(page) -> None:
    # A manual subtitle retry scheduled in an hour: the window does not say "Checking…" and
    # keeps Refresh usable meanwhile; it polls when the retry is due, not every second.
    for lang in ("en", "ru"):
        result = page.run(
            r"""
            const polls = () => __apiCalls.filter(c => c.name === 'poll_downloads_and_subtitles').map(c => c.at);
            await __tick(30000);
            const waiting = {polls: polls(), status: __text('#status'), button: {disabled: $('refreshAll').disabled, text: __text('#refreshAll')}};
            await __tick(3600000 - 30000 + 2500);
            return {waiting, due: {polls: polls(), status: __text('#status')},
                    texts: {checking: t('status.subtitleCheckingBackground'), refresh: t('action.refresh')}};
            """,
            lang=lang,
            state={"subtitle_jobs": [FUTURE_JOB]},
            before="__api.poll_downloads_and_subtitles = async () => ({stats: {}});",
        )
        out, texts = result.value, result.value["texts"]
        assert out["waiting"] == {
            "polls": [],
            "status": "",
            "button": {"disabled": False, "text": texts["refresh"]},
        }
        assert out["due"] == {"polls": [3600000, 3601000, 3602000], "status": texts["checking"]}
    # A running download keeps the window polling meanwhile; the retry still does not count
    # as a subtitle check in progress.
    download = {"name": "Other 04", "torrent_hash": "abc", "state": "downloading", "progress": 0.5}
    busy = page.run(
        r"""
        await __tick(60000);
        return {polls: __apiCalls.filter(c => c.name === 'poll_downloads_and_subtitles').length, status: __text('#status'),
                disabled: $('refreshAll').disabled};
        """,
        state={"subtitle_jobs": [FUTURE_JOB], "downloads": [download]},
        before="__api.poll_downloads_and_subtitles = async () => ({stats: {}});",
    )
    assert busy.value["polls"] >= 1 and busy.value["status"] == "" and busy.value["disabled"] is False


def test_a_new_due_subtitle_job_restarts_the_stopped_foreground_polling(page) -> None:
    # Nothing to do: polling stopped after two empty checks. The library changes (seen by the
    # state version check) and brings a due subtitle job: checking resumes at once.
    result = page.run(
        r"""
        const polls = () => __apiCalls.filter(c => c.name === 'poll_downloads_and_subtitles').map(c => c.at);
        await __tick(30000);
        const idle = polls();
        window.__versions = {ready: '1', ui: '2'};
        await __tick(5000);
        return {idle, after: polls().filter(at => at > 30000), status: __text('#status'), checking: t('status.subtitleCheckingBackground')};
        """,
        before=r"""
        window.__versions = {ready: '1', ui: '1'};
        const base = __api.get_state;
        __api.ui_state_versions = async () => window.__versions;
        __api.get_state = __api.get_state_fast = async () => ({...(await base()), ui_state_version: window.__versions.ui,
          subtitle_jobs: window.__versions.ui === '2' ? [{video: 'Show - 03.mkv', video_path: '/v/Show - 03.mkv', state: 'pending',
                                                          priority: 200, next_check: Date.now() / 1000, attempts: 0}] : []});
        __api.poll_downloads_and_subtitles = async () => ({stats: {}});
        """,
    )
    out = result.value
    assert out["idle"] == [1000, 16000]
    assert out["after"] == [32000, 33000, 34000, 35000] and out["status"] == out["checking"]


def test_constant_offset_search_is_bounded_and_keeps_small_global_shift(tmp_path: Path, monkeypatch):
    source = tmp_path / "source.srt"
    reference = tmp_path / "reference.srt"
    gaps = [1.7, 4.9, 2.6, 7.3, 1.1, 5.8, 3.4, 9.2, 2.1, 6.7]
    starts: list[float] = []
    current = 8.0
    for index in range(90):
        current += gaps[index % len(gaps)] + (index % 4) * 0.07
        starts.append(current)
    _write_srt(source, starts)
    _write_srt(reference, [value + 4.4 for value in starts])

    original = syncing._activity_correlation_for_shift
    calls = {"count": 0}

    def counted(*args, **kwargs):
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(syncing, "_activity_correlation_for_shift", counted)
    results = syncing.estimate_constant_subtitle_offsets(
        source, reference, max_offset_seconds=30.0, maximum_results=6
    )

    assert any(
        item.get("available") and abs(float(item.get("offset_seconds") or 0.0) - 4.4) <= 0.15
        for item in results
    )
    assert calls["count"] <= 140
    assert max(int(item.get("candidate_evaluations") or 0) for item in results) <= 140
