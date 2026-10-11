"""Settings render their form controls (behaviour; audit 71Б rewrite).

v0.6.20: the input()/checkbox() helpers were defined after renderSettings() and the
Settings page came out empty."""

from __future__ import annotations


def test_settings_render_checkbox_and_input_controls_with_saved_values(page) -> None:
    for lang in ("en", "ru"):
        result = page.run(
            """
            const control = id => {
              const el = document.getElementById(id);
              const label = el?.closest('label') || document.querySelector(`label[for="${id}"]`);
              return el && {type: el.type, checked: el.checked, value: el.value, label: label?.innerText.trim()};
            };
            return {escape: control('s_escape_fullscreen'), jimaku: control('s_jimaku'),
                    labels: {escape: t('settings.escapeFullscreen'), jimaku: t('settings.jimakuKey')}};
            """,
            lang=lang,
            state={
                "settings": {
                    "version": "0.6.29",
                    "escape_exits_fullscreen": True,
                    "jimaku_api_key": "jimaku-key",
                }
            },
        )
        out = result.value
        assert out["escape"] == {
            "type": "checkbox",
            "checked": True,
            "value": "on",
            "label": out["labels"]["escape"],
        }
        assert out["jimaku"] == {
            "type": "password",
            "checked": False,
            "value": "jimaku-key",
            "label": out["labels"]["jimaku"],
        }
        assert not [line for line in result.console if "ui.render.fail" in line]
