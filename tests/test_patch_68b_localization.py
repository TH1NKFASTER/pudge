"""Audit 68Б: all interface text comes from one catalog per language, with plural forms;
backend errors are English and shown in the interface language; logs are English."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import source_policy
from pudge import i18n
from i18n_support import i18n_source

ROOT = Path(__file__).parents[1]
PUDGE = ROOT / "pudge"
WEB = PUDGE / "web"
FIELD = re.compile(r"\{([A-Za-z0-9_]+)\}")
PLURAL = {"en": {"one", "other"}, "ru": {"one", "few", "many", "other"}}


def _fields(value) -> set[str]:
    texts = value.values() if isinstance(value, dict) else [value]
    return {name for text in texts for name in FIELD.findall(str(text))}


# Catalog


def test_catalogs_have_the_same_keys_placeholders_and_plural_forms() -> None:
    en, ru = i18n.catalog("en"), i18n.catalog("ru")
    assert set(en) == set(ru)
    assert len(en) > 2000
    for key, value in en.items():
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+", key), key
        other = ru[key]
        assert isinstance(value, dict) == isinstance(other, dict), key
        if isinstance(value, dict):
            assert set(value) == PLURAL["en"] and set(other) == PLURAL["ru"], key
        assert _fields(value) == _fields(other), key


def test_every_key_used_in_code_exists() -> None:
    en = i18n.catalog("en")
    patterns = [
        re.compile(r"""(?:PudgeI18n\.tn?|\btn?)\(\s*['"`]([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)['"`]"""),
        re.compile(r"""\berr\(\s*['"]([A-Za-z][A-Za-z0-9_.]+)['"]"""),
        re.compile(r"""\btr_key\(\s*['"]([A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+)['"]"""),
        re.compile(r"""data-i18n(?:-[a-z-]+)?=["']([A-Za-z][A-Za-z0-9_.]+)["']"""),
    ]
    # policy: catalog keys are looked up by name; a missing key shows the raw key anywhere.
    files = {
        **source_policy.sources("pudge/**/*.py"),
        **source_policy.sources("pudge/web/*.js"),
        **source_policy.sources("pudge/web/*.html"),
        **source_policy.sources("pudge/mpv_scripts/*.lua"),
    }
    missing = []
    for name, text in files.items():
        path = ROOT / name
        if path.name.startswith("locales."):
            continue
        for index, pattern in enumerate(patterns):
            for match in pattern.finditer(text):
                key = match.group(1)
                if index == 1 and not key.startswith("error."):
                    key = "error." + key
                if key not in en and key != "osd.someKey":
                    missing.append(f"{path.relative_to(ROOT)}: {key}")
    assert missing == []


def test_no_inline_language_pairs_or_russian_text_left_in_code() -> None:
    pair = re.compile(r"ui\.lang===?'ru'\s*\?|\bru\(\)\s*\?|\bisRu\b")
    # The one remaining check picks a date locale, not a text.
    allowed_pairs = {"index.html": 1}
    # policy: interface texts live in the catalogs, never inline in code.
    window = {**source_policy.sources("pudge/web/*.js"), **source_policy.sources("pudge/web/*.html")}
    for name, text in window.items():
        path = ROOT / name
        if path.name.startswith("locales.") or path.name == "i18n.js":
            continue
        count = len(pair.findall(text))
        assert count <= allowed_pairs.get(path.name, 0), (path.name, count)
    cyrillic = re.compile(r"[А-Яа-яЁё]")
    # Deliberate: parsers of Russian input, language names, mapping tables for texts
    # stored by older versions, and audit item ids such as "68Б" in comments.
    allowed = {
        "index.html": 2,
        "manga_reader_v2.js": 2,
        "manager.py": 24,
        "syncing.py": 1,
        "web_app.py": 2,
    }
    audit_id = re.compile(r"\b\d{1,2}[АБВ]\b")
    code = {
        **source_policy.sources("pudge/**/*.py"),
        **window,
        **source_policy.sources("pudge/mpv_scripts/*.lua"),
    }
    for name, text in code.items():
        path = ROOT / name
        if path.name.startswith("locales."):
            continue
        lines = [line for line in text.split("\n") if cyrillic.search(audit_id.sub("", line))]
        assert len(lines) <= allowed.get(path.name, 0), (path.name, lines[:5])


def test_old_localization_modules_and_tk_settings_are_gone() -> None:
    for module in ("pudge.ui_localization", "pudge.settings_ui", "pudge.tk_utils"):
        assert importlib.util.find_spec(module) is None
    assert not (WEB / "ui_localization.js").exists()
    from pudge.cli import build_parser

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--settings"])


# Python API


@pytest.mark.parametrize(
    "count,form",
    [
        (1, "one"),
        (2, "few"),
        (4, "few"),
        (5, "many"),
        (11, "many"),
        (12, "many"),
        (14, "many"),
        (21, "one"),
        (22, "few"),
        (25, "many"),
        (111, "many"),
        (101, "one"),
        (0, "many"),
        (1.5, "other"),
    ],
)
def test_russian_plural_forms(count, form) -> None:
    assert i18n.plural_form("ru", count) == form


def test_t_tn_err_and_message() -> None:
    assert i18n.plural_form("en", 1) == "one" and i18n.plural_form("en", 2) == "other"
    plural = next(
        key
        for key, value in i18n.catalog("ru").items()
        if isinstance(value, dict) and "{count}" in value["few"]
    )
    assert "3" in i18n.tn(plural, 3, lang="ru") and i18n.tn(plural, 3, lang="ru") != i18n.tn(
        plural, 5, lang="ru"
    )
    i18n.set_language("ru")
    try:
        assert i18n.t("nav.settings") == "Настройки"
        assert (
            i18n.err("manager.torrentsDisabled") == "Torrent downloads are disabled in Settings"
        )  # errors stay English
    finally:
        i18n.set_language("en")
    assert i18n.t("nav.settings") == "Settings"
    assert i18n.t("missing.key") == "missing.key"
    english = i18n.err("manager.torrentsDisabled")
    assert i18n.message(english, "ru") == i18n.t("error.manager.torrentsDisabled", lang="ru")
    # A Russian message stored by an older version is shown in English too.
    assert i18n.message(i18n.t("error.manager.torrentsDisabled", lang="ru"), "en") == english
    assert i18n.message("Error: " + english, "ru").startswith("Ошибка")
    assert i18n.message("something else", "ru") == "something else"


def test_load_config_sets_the_process_language(tmp_path) -> None:
    from pudge.config import AppConfig, load_config, write_config

    cfg = AppConfig()
    cfg.ui.language = "ru"
    write_config(cfg, tmp_path / "config.toml")
    load_config(tmp_path / "config.toml")
    assert i18n.language() == "ru"
    i18n.set_language("en")


# Window API (same catalogs, same rules)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_window_api_matches_python() -> None:
    plural = next(
        key
        for key, value in i18n.catalog("ru").items()
        if isinstance(value, dict) and "{count}" in value["few"]
    )
    english = i18n.err("manager.torrentsDisabled")
    script = (
        i18n_source()
        + f"""
const out = {{
  forms: [1, 2, 5, 11, 21, 1.5].map(n => PudgeI18n.pluralForm('ru', n)),
  plural: [1, 3, 5].map(n => PudgeI18n.tn({json.dumps(plural)}, n, {{}}, 'ru')),
  settings: PudgeI18n.t('nav.settings', {{}}, 'ru'),
  message: PudgeI18n.message({json.dumps(english)}, 'ru'),
  legacy: PudgeI18n.message({json.dumps(i18n.t("error.manager.torrentsDisabled", lang="ru"))}, 'en'),
  param: PudgeI18n.t('error.generic', {{error: {json.dumps(english)}}}, 'ru'),
  missing: PudgeI18n.t('missing.key'),
}};
process.stdout.write(JSON.stringify(out));
"""
    )
    result = json.loads(
        subprocess.run(["node", "-"], input=script, capture_output=True, text=True, check=True).stdout
    )
    assert result["forms"] == ["one", "few", "many", "many", "one", "other"]
    assert result["plural"] == [i18n.tn(plural, n, lang="ru") for n in (1, 3, 5)]
    assert result["settings"] == "Настройки"
    assert result["message"] == i18n.t("error.manager.torrentsDisabled", lang="ru")
    assert result["legacy"] == english
    assert result["param"].endswith(i18n.t("error.manager.torrentsDisabled", lang="ru"))
    assert result["missing"] == "missing.key"


def test_every_window_page_loads_the_catalogs_first() -> None:
    # policy: script order of the pages (the page harness runs them in this order too).
    for name in ("index.html", "mpv_assistant.html", "audiobook_float.html"):
        html = source_policy.source(f"pudge/web/{name}")
        positions = [
            html.index(f'src="{script}"') for script in ("locales.en.js", "locales.ru.js", "i18n.js")
        ]
        assert positions == sorted(positions), name
        first_module = min(
            html.index(m.group(0)) for m in re.finditer(r'src="(?!locales\.|i18n\.js)[a-z_0-9]+\.js"', html)
        )
        assert positions[-1] < first_module, name


def test_mpv_scripts_read_the_catalog_file() -> None:
    # policy: both mpv scripts take their texts from the catalog file the CLI names
    # (their texts are tested in the Lua tests; this keeps a new script from hard-coding).
    cli = source_policy.source("pudge/cli.py")
    assert "PUDGE_LOCALE_FILE" in cli
    for name in ("pudge_anilist.lua", "pudge_skip_segments.lua"):
        lua = source_policy.source(f"pudge/mpv_scripts/{name}")
        assert "PUDGE_LOCALE_FILE" in lua and "tr_key(" in lua


# Review fixes


def test_none_params_and_quoted_key_errors() -> None:
    assert i18n.format_text("a {x} b {y}", {"x": None}) == "a  b {y}"
    english = i18n.err("manager.torrentsDisabled")
    assert i18n.message(str(KeyError(english)), "ru") == i18n.t("error.manager.torrentsDisabled", lang="ru")


def test_reading_a_staged_config_keeps_the_process_language(tmp_path) -> None:
    from pudge.config import AppConfig, load_config, write_config

    cfg = AppConfig()
    cfg.ui.language = "ru"
    write_config(cfg, tmp_path / "config.toml")
    load_config(tmp_path / "config.toml", apply_language=False)
    assert i18n.language() == "en"


def test_manual_entries_are_stored_in_english_and_shown_translated() -> None:
    from pudge.consumption_statistics import _manual_title

    stored = i18n.t("web.manualEntry", lang="en")
    i18n.set_language("ru")
    try:
        assert _manual_title(stored) == i18n.t("web.manualEntry", lang="ru")
        assert _manual_title("") == i18n.t("web.manualEntry", lang="ru")
        assert _manual_title("My book") == "My book"
    finally:
        i18n.set_language("en")


def test_iphone_review_gate_message_is_english() -> None:
    from pudge.iphone_media import IPhoneMedia

    media = IPhoneMedia.__new__(IPhoneMedia)
    view = media._gate_view(
        {
            "blocking": True,
            "message": i18n.t("web.reviewNoDueWords", lang="ru"),
            "message_en": i18n.t("web.reviewNoDueWords", lang="en"),
        }
    )
    assert view["message"] == i18n.t("web.reviewNoDueWords", lang="en")
