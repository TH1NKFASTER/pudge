"""Word states exactly like Jiten: nine states, Jiten's colour priority, colours from the
user's Jiten display profile on request, and a palette of the other states' colours."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from pudge.config import AppConfig
from pudge.light_novels import LightNovelService
from tests.css_support import css_rules

JITEN_DEFAULTS = {
    "new": "#f43f5e",
    "not_in_deck": "#f43f5e",
    "young": "#f59e0b",
    "mature": "#4ade80",
    "mastered": "#2dd4bf",
    "due": "#f97316",
    "blacklisted": "#9ca3af",
    "suspended": "#9ca3af",
    "redundant": "#0ea5e9",
}
STATES = list(JITEN_DEFAULTS)


def _service(tmp_path: Path) -> LightNovelService:
    config = AppConfig()
    config.library.root_dir = tmp_path / "library"
    config.library.database_path = tmp_path / "pudge.sqlite3"
    config.paths.cache_dir = tmp_path / "cache"
    config.library.root_dir.mkdir(parents=True)
    config.paths.cache_dir.mkdir(parents=True)
    return LightNovelService(config)


# --- one state per word, Jiten's priority ------------------------------------------------

PRIORITY_CASES = [
    ([], "new"),
    (["new"], "new"),
    (["young"], "young"),
    (["young", "due"], "due"),
    # Due comes before everything else (the user's choice, unlike Jiten).
    (["mature", "due"], "due"),
    (["mastered", "due"], "due"),
    (["blacklisted", "due"], "due"),
    (["suspended", "redundant", "due"], "due"),
    (["mature", "mastered"], "mastered"),
    (["redundant", "mastered"], "redundant"),
    (["suspended", "redundant", "mature"], "suspended"),
    (["blacklisted", "suspended", "mastered"], "blacklisted"),
    # jpdb / older Pudge names
    (["learning"], "young"),
    (["known"], "mature"),
    (["never-forget"], "mastered"),
    (["failed"], "due"),
]


@pytest.mark.parametrize(("states", "expected"), PRIORITY_CASES)
def test_python_normalizer_uses_jiten_priority_with_groups_split(states, expected) -> None:
    assert LightNovelService._normalized_state(states) == expected


def test_not_in_deck_only_for_new_words_in_no_study_deck() -> None:
    normalize = LightNovelService._normalized_state
    assert normalize([], []) == "not_in_deck"
    assert normalize(["new"], []) == "not_in_deck"
    assert normalize(["new"], [5]) == "new"
    assert normalize(["young"], []) == "young"
    assert normalize(["new"], None) == "new"  # membership unknown: plain New


def test_live_jiten_batch_carries_deck_membership_into_the_state(tmp_path: Path, monkeypatch) -> None:
    service = _service(tmp_path)
    service.save_settings({"jiten_api_key": "key"})
    answers = [
        {"result": [[0], [0], [1, 4], [2]], "decks": [[], [7], [], []]},
        {"result": [[0]]},  # an answer without deck membership
    ]
    monkeypatch.setattr(service, "_jiten_request", lambda action, payload=None: answers.pop(0))
    rows = service.jiten_live_states([(1, 0), (2, 0), (3, 0), (4, 0)])
    assert [row["normalizedState"] for row in rows] == ["not_in_deck", "new", "due", "mature"]
    assert [row["studyDeckIds"] for row in rows] == [[], [7], [], []]
    [unknown] = service.jiten_live_states([(5, 0)])
    assert unknown["normalizedState"] == "new" and "studyDeckIds" not in unknown
    # The reader payload built from the cached live states keeps the distinction.
    parsed = {
        "paragraphs": ["猫犬"],
        "vocabulary": [{"wordId": 1, "readingIndex": 0, "knownState": []}, {"wordId": 9, "readingIndex": 0}],
        "tokens": [[{"wordId": 1, "readingIndex": 0, "start": 0, "end": 1}]],
    }
    tokens, vocabulary = service._provider_scoped_study_payload(parsed, "jiten")
    assert [item["normalizedState"] for item in vocabulary] == ["not_in_deck", "new"]
    assert tokens[0][0]["card"]["normalizedState"] == "not_in_deck"


# --- settings: nine colours, each a real colour, older settings -------------------------


def test_new_settings_default_to_jitens_colours(tmp_path: Path) -> None:
    payload = _service(tmp_path).settings_payload()
    assert payload["word_color_theme"] == "jiten"
    assert {state: payload[f"word_color_{state}"] for state in STATES} == JITEN_DEFAULTS
    assert "word_color_learning" not in payload and "word_color_known" not in payload


def _legacy_rows(service: LightNovelService, rows: dict[str, str]) -> None:
    with service._connection() as conn:
        for key, value in rows.items():
            conn.execute("INSERT OR REPLACE INTO ln_settings(key,value) VALUES(?,?)", (key, value))


def test_older_five_colour_settings_seed_the_nine_states(tmp_path: Path) -> None:
    service = _service(tmp_path)
    managed = "/* Pudge colors */\n:root {\n  --pudge-study-new: #f3f6fb;\n}\n/* /Pudge colors */"
    _legacy_rows(
        service,
        {
            "word_color_theme": "balanced",
            "word_color_new": "#f3f6fb",  # the old default
            "word_color_learning": "#f4bd63",  # the old default
            "word_color_due": "#ff7d8c",
            "word_color_known": "#123456",  # chosen by the user
            "word_color_blacklisted": "#654321",  # chosen by the user
            "custom_css": ".ln-reader{letter-spacing:1px}\n\n" + managed,
        },
    )
    payload = service.settings_payload()
    colours = {state: payload[f"word_color_{state}"] for state in STATES}
    assert colours == {
        **JITEN_DEFAULTS,
        "mature": "#123456",
        "mastered": "#123456",
        "blacklisted": "#654321",
        "suspended": "#654321",
    }
    # The old default theme becomes Jiten's; the managed block of old colours goes.
    assert payload["word_color_theme"] == "jiten"
    assert payload["custom_css"] == ".ln-reader{letter-spacing:1px}"
    # Saving once stores the nine states; the old keys no longer matter.
    service.save_settings({"word_color_mature": "#00aa00"})
    _legacy_rows(service, {"word_color_known": "#abcdef"})
    again = service.settings_payload()
    assert again["word_color_mature"] == "#00aa00" and again["word_color_mastered"] == "#123456"


def test_hex_values_are_kept_empty_becomes_the_default_and_junk_is_refused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    saved = service.save_settings(
        {"word_color_young": "#ABCDEF", "word_color_new": "", "word_color_due": None}
    )
    assert (saved["word_color_young"], saved["word_color_new"], saved["word_color_due"]) == (
        "#abcdef",
        "#f43f5e",
        "#f97316",
    )
    refused = service.save_settings({"word_color_young": "url(x)", "word_color_mature": "#12345g"})
    assert refused["word_color_young"] == "#abcdef" and refused["word_color_mature"] == "#4ade80"


# --- Jiten display profile ---------------------------------------------------------------


class _Response:
    def __init__(self, status: int, body: Any) -> None:
        self.status_code = status
        self._body = body
        self.content = b"x"

    def json(self) -> Any:
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


def _profile_service(tmp_path: Path, monkeypatch, answer, *, key: str = "secret") -> tuple[Any, list, list]:
    service = _service(tmp_path)
    if key:
        service.save_settings({"jiten_api_key": key})
    calls: list[dict[str, Any]] = []
    logs: list[str] = []
    service.logger = SimpleNamespace(info=lambda message, *args: logs.append(message % args))

    def fake_get(url, headers=None, **kwargs):
        calls.append({"url": url, "headers": headers})
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr("pudge.light_novels.httpx.get", fake_get)
    return service, calls, logs


def test_profile_colours_come_from_the_first_profile_with_jiten_mapping(tmp_path: Path, monkeypatch) -> None:
    body = {
        "profiles": [
            {
                "id": "a",
                "name": "Desk",
                "values": {
                    "colourWordsByState": True,
                    "stateColours": {
                        "new": "#111111",
                        "young": "#222222",
                        "due": None,
                        "mature": "#444444",
                        "redundant": "#555555",
                        "ignored": "#666666",
                    },
                },
                "statColumns": None,
                "updatedAt": 1,
            },
            {"id": "b", "name": "Phone", "values": {"stateColours": {"new": "#999999"}}},
        ],
        "maxProfiles": 10,
    }
    service, calls, logs = _profile_service(tmp_path, monkeypatch, _Response(200, body))
    before = service.settings_payload()
    result = service.jiten_profile_word_colors()
    assert calls == [
        {
            "url": "https://api.jiten.moe/api/user/settings/display-profiles",
            "headers": {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": "ApiKey secret",
                "User-Agent": calls[0]["headers"]["User-Agent"],
            },
        }
    ]
    assert result == {
        "ok": True,
        "profileName": "Desk",
        "profileCount": 2,
        "colors": {
            "new": "#111111",
            "not_in_deck": "#111111",
            "young": "#222222",
            "mature": "#444444",
            "mastered": "#444444",
            "due": "#f97316",  # Jiten's null (text colour): Pudge's default
            "blacklisted": "#666666",
            "suspended": "#666666",
            "redundant": "#555555",
        },
    }
    # Nothing is saved by the request itself: the window fills the editable rows.
    assert service.settings_payload() == before
    assert any(line.startswith("EVENT jiten.profile_colors profiles=2 used='Desk'") for line in logs)


def test_profile_without_saved_colours_gives_jitens_defaults(tmp_path: Path, monkeypatch) -> None:
    body = {"profiles": [{"id": "a", "name": "Default", "values": {"stateColours": {"young": "#010203"}}}]}
    service, _calls, _logs = _profile_service(tmp_path, monkeypatch, _Response(200, body))
    result = service.jiten_profile_word_colors()
    assert result["colors"] == {**JITEN_DEFAULTS, "young": "#010203"}
    assert result["profileCount"] == 1


@pytest.mark.parametrize(
    ("answer", "key", "expected"),
    [
        (
            _Response(200, {"profiles": [], "maxProfiles": 10}),
            "secret",
            {"ok": False, "reason": "no_profiles"},
        ),
        (_Response(401, {"error": "nope"}), "secret", {"ok": False, "reason": "http", "status": 401}),
        (_Response(200, ValueError("not json")), "secret", {"ok": False, "reason": "invalid"}),
        (httpx.ConnectError("down"), "secret", {"ok": False, "reason": "offline"}),
        (_Response(200, {}), "", {"ok": False, "reason": "no_key"}),
    ],
)
def test_profile_errors_are_reported_and_logged(tmp_path: Path, monkeypatch, answer, key, expected) -> None:
    service, calls, logs = _profile_service(tmp_path, monkeypatch, answer, key=key)
    assert service.jiten_profile_word_colors() == expected
    assert len(calls) == (1 if key else 0)
    assert any(line.startswith(f"FAIL jiten.profile_colors reason={expected['reason']}") for line in logs)


def test_profile_request_is_refused_by_the_test_network(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.save_settings({"jiten_api_key": "secret"})
    assert service.jiten_profile_word_colors() == {"ok": False, "reason": "offline"}


# --- iPhone ------------------------------------------------------------------------------


def test_iphone_gets_nine_colours_and_the_word_state_of_each_token() -> None:
    from pudge.iphone_media import IPhoneMedia, state_code, token_flags

    settings = SimpleNamespace(**{f"word_color_{state}": color for state, color in JITEN_DEFAULTS.items()})
    media = IPhoneMedia.__new__(IPhoneMedia)
    media._reader_settings = lambda: settings
    colors = media.reader_settings()["colors"]
    assert {state: colors[state] for state in STATES} == JITEN_DEFAULTS
    assert (colors["learning"], colors["known"]) == ("#f59e0b", "#2dd4bf")  # older iPhone apps
    word = lambda card: token_flags(card, set(), set()) >> 7  # noqa: E731
    assert word({"states": ["mature", "due"]}) == STATES.index("due") + 1
    assert word({"states": ["mature", "mastered"]}) == STATES.index("mastered") + 1
    assert word({"states": ["new"], "normalizedState": "not_in_deck"}) == STATES.index("not_in_deck") + 1
    assert word({"states": [], "normalizedState": "unknown"}) == 0
    assert [state_code({"normalizedState": name}) for name in STATES] == [0, 0, 1, 3, 3, 2, 4, 4, 3]


# --- the window ----------------------------------------------------------------------------

JS_CASES = [
    *[({"states": states}, expected) for states, expected in PRIORITY_CASES],
    ({"states": ["new"], "studyDeckIds": []}, "not_in_deck"),
    ({"states": ["new"], "studyDeckIds": [3]}, "new"),
    ({"normalizedState": "not_in_deck", "states": ["new"]}, "not_in_deck"),
    # an older cache's bucket names: the raw states decide, else the Jiten name
    ({"normalizedState": "learning", "states": ["mature"]}, "mature"),
    ({"normalizedState": "known"}, "mature"),
    ({"normalizedState": "learning"}, "young"),
]


def test_window_normalizer_matches_the_backend(page) -> None:
    cards = [card for card, _ in JS_CASES]
    result = page.run(
        f"return {json.dumps(cards)}.map(card => PudgeReadingTools.study.normalizeState(card));"
    )
    assert result.value == [expected for _, expected in JS_CASES]


def test_empty_state_colour_paints_words_in_the_default_colour(page) -> None:
    result = page.run(
        r"""
        PudgeReadingTools.study.applyAppearance({word_color_mature: '', word_color_young: '#123456', word_color_mastered: null});
        const style = document.documentElement.style;
        const box = document.createElement('div'); document.body.appendChild(box);
        box.innerHTML = PudgeReadingTools.study.renderParsedText({paragraphs: ['猫犬'], tokens: [[
          {start: 0, end: 1, wordId: 1, readingIndex: 0, card: {states: ['mature', 'mastered']}},
          {start: 1, end: 2, wordId: 2, readingIndex: 0, card: {states: ['young']}}]], vocabulary: []}, {backend: 'jiten'});
        return {mature: style.getPropertyValue('--pudge-study-mature'), mastered: style.getPropertyValue('--pudge-study-mastered'),
                young: style.getPropertyValue('--pudge-study-young'), suspended: style.getPropertyValue('--pudge-study-suspended'),
                classes: [...box.querySelectorAll('[data-pudge-study-token]')].map(n => [...n.classList].filter(c => c.startsWith('state-')))};
        """
    )
    assert result.value == {
        "mature": "#4ade80",
        "mastered": "#2dd4bf",
        "young": "#123456",
        "suspended": "#9ca3af",
        "classes": [["state-mastered"], ["state-young"]],
    }
    for sheet, word in (("reading_tools.css", ".pudge-study-word"), ("index.html", ".ln-word")):
        rules = css_rules(sheet)
        for state in STATES:
            fallback = JITEN_DEFAULTS[state]
            assert rules.get(f"{word}.state-{state}", "color") == f"var(--pudge-study-{state},{fallback})", (
                sheet,
                state,
            )


def test_reader_word_carries_one_colour_class_only(page) -> None:
    # A Mature+Due word keeps its raw state classes except the other colour states'.
    result = page.run(
        r"""
        return [lnRawStateClasses(['mature', 'due', 'locked'], 'mature'), lnRawStateClasses(['new'], 'not_in_deck')];
        """
    )
    assert result.value == [["state-mature", "state-locked"], []]


SETTINGS = {
    "version": 1,
    "settings": {
        "jiten_api_key": "key",
        "word_color_theme": "jiten",
        **{f"word_color_{s}": c for s, c in JITEN_DEFAULTS.items()},
    },
    "books": [],
}
ROWS = r"""
populateLnReaderAppearance(ui.lnState.settings);
const ids = LN_WORD_COLOR_STATES.map(([, id]) => id);
const rows = () => ids.map(id => ({label: document.getElementById(`${id}Name`).textContent, value: lnWordColorValue(id),
  controls: [...$(id).closest('.ln-word-color-picker').querySelectorAll('input')].map(i => i.type), disabled: $(id).disabled}));
const palettes = () => ids.map(id => { const list = document.getElementById($(id).getAttribute('list'));
  return list ? [...list.querySelectorAll('option')].map(o => o.getAttribute('value')) : null; });
const saves = () => __apiCalls.filter(c => c.name === 'light_novel_save_settings').map(c => c.args[0]);
"""


@pytest.mark.parametrize(
    ("lang", "labels"),
    [
        (
            "en",
            [
                "New",
                "Not in deck",
                "Young",
                "Mature",
                "Mastered",
                "Due",
                "Blacklisted",
                "Suspended",
                "Redundant",
            ],
        ),
        (
            "ru",
            [
                "Новые",
                "Нет в колоде",
                "Молодые",
                "Зрелые",
                "Освоенные",
                "К повторению",
                "В чёрном списке",
                "Приостановленные",
                "Избыточные",
            ],
        ),
    ],
)
def test_reader_settings_show_nine_rows_with_a_colour_each(page, lang, labels) -> None:
    result = page.run(
        ROWS
        + r"""
        const out = {start: rows(), button: $('lnrWordJitenProfile').textContent};
        await __input('#lnrWordMature', '#abcdef'); await __tick(300);
        await __input('#lnrWordYoung', '#00ff00'); await __tick(300);
        out.after = rows(); out.saved = saves().at(-1); out.theme = $('lnrWordTheme').value;
        return out;
        """,
        lang=lang,
        api={"light_novel_state": SETTINGS},
    )
    out = result.value
    assert [row["label"] for row in out["start"]] == labels
    assert [row["value"] for row in out["start"]] == list(JITEN_DEFAULTS.values())
    # Each row is just a colour picker (no "Text color" checkbox), all enabled.
    assert [(row["controls"], row["disabled"]) for row in out["start"]] == [(["color"], False)] * 9
    assert out["button"] == (
        "Load colors from Jiten profile" if lang == "en" else "Загрузить цвета из профиля Jiten"
    )
    after = {row["label"]: row for row in out["after"]}
    assert after[labels[3]]["value"] == "#abcdef" and after[labels[2]]["value"] == "#00ff00"
    assert out["theme"] == "custom"
    assert out["saved"]["word_color_mature"] == "#abcdef" and out["saved"]["word_color_young"] == "#00ff00"
    assert out["saved"]["word_color_theme"] == "custom"


def test_each_picker_offers_the_other_states_unique_colours(page) -> None:
    result = page.run(
        ROWS
        + r"""
        const out = {lists: ids.map(id => $(id).getAttribute('list')), start: palettes()};
        await __input('#lnrWordRedundant', '#f59e0b'); await __tick(50);  // same as young
        await __input('#lnrWordMastered', '#00ff00'); await __tick(50);
        out.after = palettes();
        return out;
        """,
        api={"light_novel_state": SETTINGS},
    )
    out = result.value
    assert all(out["lists"]) and len(set(out["lists"])) == 9
    others = lambda values, index: sorted({v for k, v in enumerate(values) if k != index and v})  # noqa: E731
    start = list(JITEN_DEFAULTS.values())
    assert [sorted(p) for p in out["start"]] == [others(start, k) for k in range(9)]
    after = [*start[:4], "#00ff00", *start[5:8], "#f59e0b"]
    assert [sorted(p) for p in out["after"]] == [others(after, k) for k in range(9)]
    # Unique: young and redundant share a colour, it is offered once.
    assert out["after"][0].count("#f59e0b") == 1


PROFILE = {
    "ok": True,
    "profileName": "Desk",
    "profileCount": 2,
    "colors": {
        **JITEN_DEFAULTS,
        "new": "#111111",
        "not_in_deck": "#111111",
        "mature": "#444444",
        "mastered": "#444444",
    },
}


def test_load_from_jiten_profile_fills_the_rows_and_saves_them_editable(page) -> None:
    result = page.run(
        ROWS
        + r"""
        __apiCalls.length = 0;
        await __click('#lnrWordJitenProfile'); await __tick(300);
        const out = {rows: rows(), note: __text('#lnrWordJitenProfileNote'), theme: $('lnrWordTheme').value,
                     saved: saves(), calls: __apiCalls.filter(c => c.name === 'light_novel_jiten_profile_colors').length};
        await __input('#lnrWordNew', '#222222'); await __tick(300);   // still editable afterwards
        out.edited = saves().at(-1).word_color_new;
        return out;
        """,
        api={"light_novel_state": SETTINGS, "light_novel_jiten_profile_colors": PROFILE},
    )
    out = result.value
    assert out["calls"] == 1
    assert [row["value"] for row in out["rows"]] == [PROFILE["colors"][s] for s in STATES]
    assert out["theme"] == "custom"
    assert len(out["saved"]) == 1
    assert {k: v for k, v in out["saved"][0].items() if k.startswith("word_color_")} == {
        "word_color_theme": "custom",
        **{f"word_color_{s}": PROFILE["colors"][s] for s in STATES},
    }
    assert (
        out["note"]
        == "Colors loaded from the Jiten profile “Desk” (the first of 2). You can still change each one."
    )
    assert out["edited"] == "#222222"


@pytest.mark.parametrize(
    ("answer", "note"),
    [
        ({"ok": False, "reason": "no_key"}, "Add your Jiten API key first."),
        ({"ok": False, "reason": "offline"}, "Jiten is unreachable. Check the connection and try again."),
        ({"ok": False, "reason": "http", "status": 401}, "Jiten answered HTTP 401; colors were not changed."),
        (
            {"ok": False, "reason": "no_profiles"},
            "Your Jiten account has no display profiles yet; colors were not changed.",
        ),
        (
            {**PROFILE, "profileCount": 1},
            "Colors loaded from your Jiten profile. You can still change each one.",
        ),
    ],
)
def test_load_from_jiten_profile_reports_the_result(page, answer, note) -> None:
    result = page.run(
        ROWS
        + r"""
        __apiCalls.length = 0;
        await __click('#lnrWordJitenProfile'); await __tick(300);
        return {note: __text('#lnrWordJitenProfileNote'), values: rows().map(row => row.value), saves: saves().length};
        """,
        api={"light_novel_state": SETTINGS, "light_novel_jiten_profile_colors": answer},
    )
    assert result.value["note"] == note
    if not answer["ok"]:
        assert result.value["values"] == list(JITEN_DEFAULTS.values()) and result.value["saves"] == 0


def test_nothing_is_fetched_from_jiten_without_the_button(page) -> None:
    result = page.run(ROWS + "await __tick(500); return true;", api={"light_novel_state": SETTINGS})
    assert not result.called("light_novel_jiten_profile_colors")
