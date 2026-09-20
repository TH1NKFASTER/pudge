from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol


REVIEW_GATE_SCHEMA = 1
_STATE_PREFIX = "review_gate:v1"


class StateStore(Protocol):
    def get_state(self, key: str, default: str = "") -> str: ...
    def set_state(self, key: str, value: str) -> None: ...


@dataclass(frozen=True, slots=True)
class EpisodeReviewIdentity:
    media_id: int
    episode: int

    @property
    def logical_id(self) -> str:
        return f"{int(self.media_id)}:{int(self.episode)}"


@dataclass(frozen=True, slots=True)
class ReviewGateProgress:
    required: int
    confirmed_card_keys: tuple[str, ...]
    unknown_card_keys: tuple[str, ...]
    granted: bool

    @property
    def completed(self) -> int:
        return len(self.confirmed_card_keys)

    @property
    def remaining(self) -> int:
        return max(0, int(self.required) - self.completed)

    def payload(self) -> dict[str, Any]:
        return {
            "required": int(self.required),
            "completed": self.completed,
            "remaining": self.remaining,
            "confirmed_card_keys": list(self.confirmed_card_keys),
            "unknown_card_keys": list(self.unknown_card_keys),
            "granted": bool(self.granted),
        }


def review_card_key(word_id: int, reading_index: int) -> str:
    return f"{int(word_id)}:{int(reading_index)}"


def _state_key(account_key: str, identity: EpisodeReviewIdentity) -> str:
    safe_account = str(account_key or "").strip()
    if not safe_account:
        raise ValueError("review gate requires an account key")
    return f"{_STATE_PREFIX}:{safe_account}:{identity.logical_id}"


class ReviewGateStore:
    """Durable per-account, per-logical-episode review progress.

    A grant is permanent for that logical episode once earned. Changing the
    configured quota later does not revoke an existing grant. Confirmed card
    keys are unique, and ambiguous mutation outcomes are retained separately so
    callers never have to retry the same card blindly.
    """

    def __init__(self, state: StateStore) -> None:
        self._state = state

    def _load_raw(self, account_key: str, identity: EpisodeReviewIdentity) -> dict[str, Any]:
        raw = self._state.get_state(_state_key(account_key, identity), "")
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _keys(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            key = str(item or "").strip()
            if key and key not in seen:
                out.append(key)
                seen.add(key)
        return out

    def status(
        self,
        account_key: str,
        identity: EpisodeReviewIdentity,
        *,
        required: int,
    ) -> ReviewGateProgress:
        data = self._load_raw(account_key, identity)
        confirmed = self._keys(data.get("confirmed"))
        unknown = [key for key in self._keys(data.get("unknown")) if key not in set(confirmed)]
        granted = bool(data.get("granted")) or len(confirmed) >= max(1, int(required))
        return ReviewGateProgress(
            required=max(1, int(required)),
            confirmed_card_keys=tuple(confirmed),
            unknown_card_keys=tuple(unknown),
            granted=granted,
        )

    def _save(
        self,
        account_key: str,
        identity: EpisodeReviewIdentity,
        progress: ReviewGateProgress,
    ) -> ReviewGateProgress:
        payload = {
            "schema": REVIEW_GATE_SCHEMA,
            "confirmed": list(progress.confirmed_card_keys),
            "unknown": list(progress.unknown_card_keys),
            "granted": bool(progress.granted),
        }
        self._state.set_state(
            _state_key(account_key, identity),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )
        return progress

    def mark_confirmed(
        self,
        account_key: str,
        identity: EpisodeReviewIdentity,
        *,
        required: int,
        card_key: str,
    ) -> ReviewGateProgress:
        before = self.status(account_key, identity, required=required)
        if before.granted:
            return before
        confirmed = list(before.confirmed_card_keys)
        key = str(card_key or "").strip()
        if key and key not in confirmed:
            confirmed.append(key)
        unknown = [item for item in before.unknown_card_keys if item != key]
        granted = len(confirmed) >= max(1, int(required))
        return self._save(
            account_key,
            identity,
            ReviewGateProgress(
                required=max(1, int(required)),
                confirmed_card_keys=tuple(confirmed),
                unknown_card_keys=tuple(unknown),
                granted=granted,
            ),
        )

    def mark_unknown(
        self,
        account_key: str,
        identity: EpisodeReviewIdentity,
        *,
        required: int,
        card_key: str,
    ) -> ReviewGateProgress:
        before = self.status(account_key, identity, required=required)
        if before.granted:
            return before
        key = str(card_key or "").strip()
        if not key or key in before.confirmed_card_keys:
            return before
        unknown = list(before.unknown_card_keys)
        if key not in unknown:
            unknown.append(key)
        return self._save(
            account_key,
            identity,
            ReviewGateProgress(
                required=max(1, int(required)),
                confirmed_card_keys=before.confirmed_card_keys,
                unknown_card_keys=tuple(unknown),
                granted=False,
            ),
        )
