"""The nine word states Pudge colours words by, grouped and prioritised like Jiten.

Jiten keeps several KnownStates per word (New=0, Young=1, Mature=2, Blacklisted=3,
Due=4, Mastered=5, Redundant=6, Suspended=7). One colour is picked like Jiten's
``wordStateColourKey`` with its groups split, except that Due comes first (the user's
choice: a word due for review shows as due whatever else it is): due, blacklisted,
suspended, redundant, mastered, mature, young, then new. "not_in_deck" is a New word that is in none
of the user's study decks (only when the provider said so; otherwise plain "new").
"""

from __future__ import annotations

import re
from typing import Any

WORD_STATES = (
    "new",
    "not_in_deck",
    "young",
    "mature",
    "mastered",
    "due",
    "blacklisted",
    "suspended",
    "redundant",
)
# Jiten's priority (wordStateColourKey) with blacklisted/suspended and mastered/mature split,
# Due moved first: a due word always shows as due.
WORD_STATE_PRIORITY = ("due", "blacklisted", "suspended", "redundant", "mastered", "mature", "young")
# jpdb and older Pudge names.
STATE_ALIASES = {
    "learning": "young",
    "failed": "due",
    "known": "mature",
    "never-forget": "mastered",
    "never_forget": "mastered",
    "neverforget": "mastered",
    "not-in-deck": "not_in_deck",
}
# Every state has a real colour. Jiten's DEFAULT_WORD_STATE_COLOURS, except that mature and
# mastered (Jiten: the ordinary text colour) are green and teal.
DEFAULT_WORD_COLORS = {
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
# Jiten profile colour key -> Pudge states it sets.
JITEN_PROFILE_COLOR_KEYS = {
    "new": ("new", "not_in_deck"),
    "young": ("young",),
    "due": ("due",),
    "mature": ("mature", "mastered"),
    "redundant": ("redundant",),
    "ignored": ("blacklisted", "suspended"),
}
# Coarse groups for statistics (Jiten colours mature like mastered: known).
WORD_STATE_GROUPS = {
    "new": "new",
    "not_in_deck": "new",
    "young": "learning",
    "mature": "known",
    "due": "learning",
    "mastered": "known",
    "redundant": "known",
    "blacklisted": "blacklisted",
    "suspended": "blacklisted",
}
_HEX = re.compile(r"#[0-9a-f]{6}")


def canonical_state(value: Any) -> str:
    name = str(value or "").strip().casefold()
    return STATE_ALIASES.get(name, name)


def normalized_word_state(states: Any, *, in_deck: bool | None = None) -> str:
    """One of WORD_STATES for a word's raw states; in_deck=None means unknown."""
    values = states if isinstance(states, (list, tuple, set)) else []
    lowered = {canonical_state(value) for value in values}
    for name in WORD_STATE_PRIORITY:
        if name in lowered:
            return name
    return "not_in_deck" if in_deck is False else "new"


def word_state_group(normalized: Any) -> str:
    """new / learning / known / blacklisted, or "unknown" (also for unknown names)."""
    return WORD_STATE_GROUPS.get(canonical_state(normalized), "unknown")


def word_color(value: Any, fallback: str) -> str:
    """A saved colour "#rrggbb"; anything else (also "" from older saves) is ``fallback``."""
    candidate = str(value or "").strip().lower()
    return candidate if _HEX.fullmatch(candidate) else fallback


def jiten_profile_colors(payload: Any) -> dict[str, Any]:
    """Word colours of the first display profile of Jiten's display-profiles answer.

    Returns {"name", "count", "colors": {state: "#rrggbb"}}; missing, null (Jiten's
    ordinary text colour) or invalid colours take Pudge's defaults.
    Raises ValueError("no_profiles") / ValueError("invalid").
    """
    if not isinstance(payload, dict):
        raise ValueError("invalid")
    profiles = payload.get("profiles", payload.get("Profiles"))
    if not isinstance(profiles, list):
        raise ValueError("invalid")
    profiles = [item for item in profiles if isinstance(item, dict)]
    if not profiles:
        raise ValueError("no_profiles")
    first = profiles[0]
    values = first.get("values", first.get("Values"))
    values = values if isinstance(values, dict) else {}
    stored = values.get("stateColours")
    stored = stored if isinstance(stored, dict) else {}
    colors: dict[str, str] = {}
    for key, states in JITEN_PROFILE_COLOR_KEYS.items():
        for state in states:
            colors[state] = word_color(stored.get(key), DEFAULT_WORD_COLORS[state])
    return {
        "name": str(first.get("name", first.get("Name")) or ""),
        "count": len(profiles),
        "colors": colors,
    }
