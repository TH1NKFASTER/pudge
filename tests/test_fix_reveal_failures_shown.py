"""Actions that answered {ok: false, error} did nothing visible (review FE-9).

"Show in Finder" of an audiobook whose file was moved, the safe-mode backup and the Irodori
install log now say why nothing opened. The LN audiobook Nyaa row stayed disabled when its
download could not start; it is usable again for another try.
Pages run in the node harness (tests/page_harness.py)."""

from __future__ import annotations

from pathlib import Path

from test_web_app import make_api
from tests.i18n_support import en, ru

AUDIOBOOK = r"""
const book = () => ({id: 5, title: 'Book', duration: 600, position: 30, cover_url: 'cover.jpg', bookmarks: []});
__api.audiobook_state = async () => ({books: [book()]});
__api.audiobook_reveal_source = async () => ({ok: false, error: 'Audiobook source does not exist'});
__api.reveal_safe_mode_backup = async () => ({ok: false, error: 'No migration backup is available'});
__api.reveal_irodori_install_log = async () => ({ok: false, path: '/x/install.log', error: 'The install log does not exist yet'});
__api.start_light_novel_download_audiobook_nyaa = async () => { throw new Error('Nyaa is not reachable'); };
"""


def test_reveal_failures_are_shown_in_the_interface_language(page) -> None:
    for lang in ("en", "ru"):
        out = page.run(
            r"""
            const out = {};
            setPage('audiobooks', true); await __tick(50);
            await __fire('.audiobook-cover[data-audio-cover-id="5"]', 'contextmenu', {clientX: 40, clientY: 50}, 'MouseEvent');
            await __click('#contextMenu [data-audio-context-action="reveal"]'); await __flush();
            out.audiobook = __toasts().at(-1);
            const host = document.createElement('div');
            host.innerHTML = '<button data-action="safe-mode-backup">b</button><button id="openIrodoriInstallLog">l</button>';
            document.body.appendChild(host);
            await __click('[data-action="safe-mode-backup"]'); await __flush();
            out.backup = __toasts().at(-1);
            await __click('#openIrodoriInstallLog'); await __flush();
            out.log = __toasts().at(-1);
            out.expected = [PudgeI18n.message('Audiobook source does not exist'), PudgeI18n.message('No migration backup is available'),
                            PudgeI18n.t('error.irodori.installLogMissing')];
            return out;
            """,
            lang=lang,
            before=AUDIOBOOK,
        ).value
        assert [out["audiobook"], out["backup"], out["log"]] == out["expected"]
        if lang == "ru":
            assert out["expected"] == [
                ru("error.audiobook.sourceMissing"),
                ru("error.backup.noMigrationBackup"),
                ru("error.irodori.installLogMissing"),
            ]


def test_ln_audiobook_nyaa_row_is_usable_again_after_a_failed_start(page) -> None:
    out = page.run(
        r"""
        ui.lnAudiobookNyaaRows = [{title: '[Group] Volume 1', selected_track_count: 0}];
        ui.lnAudiobookNyaaBookId = 5;
        $('modalBackdrop').hidden = false; $('modalBackdrop').classList.add('open');
        $('modalBody').innerHTML = '<button data-ln-audiobook-release="0">Download</button>';
        await __click('[data-ln-audiobook-release="0"]'); await __flush();
        return {disabled: document.querySelector('[data-ln-audiobook-release="0"]').disabled,
                open: $('modalBackdrop').classList.contains('open'), toasts: __toasts(),
                calls: __apiCalls.filter(call => call.name === 'start_light_novel_download_audiobook_nyaa').length};
        """,
        before=AUDIOBOOK,
        allow_errors=True,
    ).value
    assert out["calls"] == 1
    assert out["disabled"] is False and out["open"] is True
    assert any("Nyaa is not reachable" in text for text in out["toasts"])


def test_missing_irodori_log_answers_with_a_reason(tmp_path: Path) -> None:
    api = make_api(tmp_path)
    api._irodori_install_log_path = lambda: tmp_path / "logs" / "irodori-install.log"
    result = api.reveal_irodori_install_log()
    assert result["ok"] is False and result["error"] == en("error.irodori.installLogMissing")
    assert ru("error.irodori.installLogMissing") == "Журнал установки ещё не создан"
