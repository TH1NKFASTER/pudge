"""Provider × mode review action sets: the single source for grade buttons.

Each action has a stable ``id`` (what the UI sends and shortcuts bind to), a
provider-native ``wire`` grade (what the provider transport receives), labels,
a colour ``tone`` and a default key. ``native`` keeps the provider's own grade
scale (Jiten 4, jpdb 5); ``binary`` is a two-button interface:

* Jiten has no native pass/fail, so binary maps Fail → again(1), Pass → good(3).
* jpdb has native ``fail``/``pass`` grades (official API), sent verbatim.

This module never schedules reviews itself and never rewrites history.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any
from .i18n import err, t

REVIEW_MODES = ("native", "binary")
DEFAULT_REVIEW_MODE = "native"


@dataclass(frozen=True, slots=True)
class ReviewAction:
    id: str
    wire: str
    label_en: str
    label_ru: str
    tone: str
    default_key: str


def _action(action_id: str, wire: str, label: str, tone: str, default_key: str) -> ReviewAction:
    """Labels in both languages come from the interface catalog (``web.reviewGrade<Label>``)."""
    key = f"web.reviewGrade{label}"
    return ReviewAction(action_id, wire, t(key, lang="en"), t(key, lang="ru"), tone, default_key)


_ACTIONS: dict[tuple[str, str], tuple[ReviewAction, ...]] = {
    ("jiten", "native"): (
        _action("again", "again", "Again", "again", "1"),
        _action("hard", "hard", "Hard", "hard", "2"),
        _action("good", "good", "Good", "good", "3"),
        _action("easy", "easy", "Easy", "easy", "4"),
    ),
    ("jiten", "binary"): (
        _action("fail", "again", "Fail", "again", "1"),
        _action("pass", "good", "Pass", "good", "2"),
    ),
    ("jpdb", "native"): (
        _action("nothing", "nothing", "Nothing", "again", "1"),
        _action("something", "something", "Something", "something", "2"),
        _action("hard", "hard", "Hard", "hard", "3"),
        _action("okay", "okay", "Okay", "good", "4"),
        _action("easy", "easy", "Easy", "easy", "5"),
    ),
    ("jpdb", "binary"): (
        _action("fail", "fail", "Fail", "again", "1"),
        _action("pass", "pass", "Pass", "good", "2"),
    ),
}

_SHORTCUT_RE = re.compile(r"^(?:(?:Meta|Ctrl|Alt|Shift)\+){0,4}[^\s+]{1,24}$")
# Space confirms the selected grade and Esc/Enter keep their dialog meaning.
_RESERVED_KEYS = {"space", "esc", "escape", "enter"}


class ReviewActionError(ValueError):
    pass


def normalize_provider(provider: str) -> str:
    return "jpdb" if str(provider or "").casefold() == "jpdb" else "jiten"


def normalize_mode(mode: str) -> str:
    value = str(mode or "").casefold()
    return value if value in REVIEW_MODES else DEFAULT_REVIEW_MODE


def actions_for(provider: str, mode: str) -> tuple[ReviewAction, ...]:
    return _ACTIONS[(normalize_provider(provider), normalize_mode(mode))]


def binding_key(provider: str, mode: str, action_id: str) -> str:
    return f"{normalize_provider(provider)}:{normalize_mode(mode)}:{action_id}"


def normalize_shortcuts(value: Any) -> dict[str, str]:
    """Keep only known profile:action keys with a valid shortcut or '' (disabled)."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except json.JSONDecodeError:
            value = {}
    if not isinstance(value, dict):
        return {}
    known = {
        binding_key(provider, mode, action.id)
        for (provider, mode), actions in _ACTIONS.items()
        for action in actions
    }
    out: dict[str, str] = {}
    for key, raw in value.items():
        key = str(key)
        if key not in known:
            continue
        shortcut = str(raw or "").strip()
        if shortcut and (
            not _SHORTCUT_RE.fullmatch(shortcut) or shortcut.rsplit("+", 1)[-1].casefold() in _RESERVED_KEYS
        ):
            continue
        out[key] = shortcut
    return out


def effective_bindings(provider: str, mode: str, shortcuts: dict[str, str]) -> dict[str, str]:
    return {
        action.id: shortcuts.get(binding_key(provider, mode, action.id), action.default_key)
        for action in actions_for(provider, mode)
    }


def binding_conflicts(shortcuts: dict[str, str]) -> list[dict[str, Any]]:
    """Conflicts inside one simultaneously active profile only."""
    conflicts: list[dict[str, Any]] = []
    for provider, mode in _ACTIONS:
        seen: dict[str, str] = {}
        for action_id, shortcut in effective_bindings(provider, mode, shortcuts).items():
            if not shortcut:
                continue
            folded = shortcut.casefold()
            if folded in seen:
                conflicts.append(
                    {
                        "provider": provider,
                        "mode": mode,
                        "shortcut": shortcut,
                        "actions": [seen[folded], action_id],
                    }
                )
            else:
                seen[folded] = action_id
    return conflicts


def action_set_payload(provider: str, mode: str, shortcuts: dict[str, str]) -> dict[str, Any]:
    bindings = effective_bindings(provider, mode, shortcuts)
    return {
        "provider": normalize_provider(provider),
        "mode": normalize_mode(mode),
        "actions": [
            {**asdict(action), "shortcut": bindings[action.id]} for action in actions_for(provider, mode)
        ],
    }


def all_action_sets(shortcuts: dict[str, str]) -> dict[str, Any]:
    return {
        f"{provider}:{mode}": action_set_payload(provider, mode, shortcuts) for provider, mode in _ACTIONS
    }


def resolve_wire_grade(provider: str, mode: str, requested: str) -> str:
    """Map a UI action id to the provider wire grade for the *active* profile.

    Rejects grades that belong to a hidden profile (e.g. ``hard`` while the
    binary mode is active) before anything reaches the network.
    """
    value = str(requested or "").casefold()
    for action in actions_for(provider, mode):
        if value == action.id:
            return action.wire
    raise ReviewActionError(
        err(
            "review.actionUnavailable",
            action=repr(requested),
            provider=normalize_provider(provider),
            mode=normalize_mode(mode),
        )
    )
