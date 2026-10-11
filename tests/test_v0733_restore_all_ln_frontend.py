"""The light-novel reader features restored in v0.7.31-v0.7.33 (behaviour; audit 71Б rewrite):
the toolbar groups the reader actions behind the audio controls and shortens its labels in
narrow windows, Cmd+Shift+L exports the listen-together trace, the next word's furigana shows
just before it is spoken.

The old test_v0731 / test_v0732 repair checks are covered here and in
test_v0734 / test_v0735 (see the audit report). Every page.run loads all inline scripts
of index.html, so a script that does not parse fails it (the old `node --check` tests)."""

from __future__ import annotations

from css_support import css_rules
from t71g20_support import LN_PAIRED, PAIRED_VIEW


def test_toolbar_puts_the_audio_tray_before_the_grouped_reader_actions(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            PAIRED_VIEW
            + r"""
            await openBook();
            const toolbar = document.querySelector('#lnReaderShell .ln-reader-toolbar');
            const shown = [...toolbar.children].filter(node => node.checkVisibility());
            const actions = toolbar.querySelector('.ln-reader-actions');
            const back = $('lnReaderClose'), finish = $('lnFinishVolume');
            return {
              order: shown.map(node => node.id || node.className),
              actions: [...actions.children].filter(node => node.checkVisibility()).map(node => node.id),
              back: [back.dataset.wideLabel, back.getAttribute('aria-label')],
              finish: [finish.dataset.wideLabel, finish.dataset.shortLabel],
              texts: {back: t('nav.lightnovels'), finish: [t('ui.finishVolume'), t('ui.finish')]},
            };
            """,
            lang=lang,
            before=LN_PAIRED,
        )
        out = result.value
        assert out["order"] == ["lnReaderClose", "ln-reader-heading", "lnPairedTray", "ln-reader-actions"]
        assert out["actions"] == [
            "lnWordMarksToggle",
            "lnUnknownFuriganaToggle",
            "lnReaderAppearanceToggle",
            "lnFinishVolume",
        ]
        assert out["back"] == [out["texts"]["back"]] * 2
        assert out["finish"] == out["texts"]["finish"]


def test_narrow_windows_show_short_toolbar_labels() -> None:
    # The labels are drawn from the data attributes: "← Light Novels" / "Finish volume" in a
    # wide window, "←" / "Finish" from 1100 px down.
    rules = css_rules("index.html")
    assert rules.get("#lnReaderClose::after", "content") == '" " attr(data-wide-label)'
    assert rules.get("#lnFinishVolume::after", "content") == "attr(data-wide-label)"
    assert rules.get("#lnReaderClose::after", "content", media="(max-width:1100px)") == '""'
    assert (
        rules.get("#lnFinishVolume::after", "content", media="(max-width:1100px)") == "attr(data-short-label)"
    )


def test_cmd_shift_l_exports_the_listen_together_trace(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            PAIRED_VIEW
            + r"""
            await openBook();
            await __key(' ', {code: 'Space'}, 'body');
            await __tick(2500);
            await __key('l', {code: 'KeyL', metaKey: true, shiftKey: true}, 'body');
            await __tick(10);
            await __key('l', {code: 'KeyL', metaKey: true}, 'body');          // without Shift: nothing
            await __tick(10);
            return {toasts: __toasts(), saved: t('ui.traceSaved', {path: '/logs/trace.json'})};
            """,
            lang=lang,
            before=LN_PAIRED,
            api={"light_novel_export_audio_sync_trace": {"path": "/logs/trace.json"}},
        )
        [[payload]] = result.calls_of("light_novel_export_audio_sync_trace")
        assert (payload["book"]["id"], payload["book"]["audiobook_id"], payload["chapter"]["index"]) == (
            6,
            9,
            0,
        )
        assert payload["alignment"]["ready"] is True
        changes = [event for event in payload["events"] if event["event"] == "word_change"]
        assert [event["next_word"]["surface"] for event in changes] == ["ああ", "いいい"]
        # Every event names the word being highlighted at that moment by its text.
        assert payload["events"][-1]["current_word"]["surface"] == "いいい"
        assert result.value["saved"] in result.value["toasts"]
        assert not result.called("export_runtime_debug_bundle")


def test_next_words_furigana_shows_just_before_it_is_spoken(page) -> None:
    result = page.run(
        PAIRED_VIEW
        + r"""
        await openBook();
        const preview = () => [...document.querySelectorAll('#lnReader .ln-paired-furigana-preview')].map(label);
        await __key(' ', {code: 'Space'}, 'body');
        await __tick(1500);
        const early = preview();
        await __tick(430);                       // 0.07 s before いいい starts at 2 s
        const soon = {preview: preview(), current: highlight().words};
        await __tick(200);
        return {early, soon, spoken: {preview: preview(), current: highlight().words}};
        """,
        before=LN_PAIRED,
    )
    out = result.value
    assert out["early"] == []
    assert out["soon"] == {"preview": ["0:いいい"], "current": ["0:ああ"]}
    assert out["spoken"]["current"] == ["0:いいい"] and "0:いいい" not in out["spoken"]["preview"]
