from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from typing import Any

import httpx

from .branding import APP_SLUG

JITEN_API_BASE = "https://api.jiten.moe/api"
JPDB_API_BASE = "https://jpdb.io/api/v1"
JITEN_SOURCE_REVISION = "216df6378db619fc0b803f5695dc925c0311ee84"


class ReviewProviderError(RuntimeError):
    """A provider rejected a request or returned a deterministic invalid response."""


class ReviewOutcomeUnknown(ReviewProviderError):
    """A mutation may have reached the provider; the caller must not blindly retry it."""

    def __init__(self, provider: str, attempt_id: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider
        self.attempt_id = attempt_id


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    provider: str
    api_base: str
    api_source: str
    source_revision: str
    auth: str
    account_identity: str
    card_id_namespace: str
    card_id_shape: str
    enumerate_existing_reviewable: bool
    previous_review_evidence: bool
    due_state: bool
    grades: tuple[str, ...]
    idempotency: str
    review_history: str
    reconciliation: str
    mutation_retry: str
    strict_gate_supported: bool
    strict_gate_reason: str

    def as_dict(self, *, configured: bool, account_key: str) -> dict[str, Any]:
        payload = asdict(self)
        payload["grades"] = list(self.grades)
        payload["configured"] = bool(configured)
        payload["account_key"] = account_key
        return payload


JITEN_CAPABILITIES = ProviderCapabilities(
    provider="jiten",
    api_base=JITEN_API_BASE,
    api_source="Sirush/Jiten",
    source_revision=JITEN_SOURCE_REVISION,
    auth="ApiKey",
    account_identity="credential_fingerprint (remote user id is not exposed by the reviewed contract)",
    card_id_namespace="jiten",
    card_id_shape="(wordId, readingIndex)",
    enumerate_existing_reviewable=True,
    previous_review_evidence=True,
    due_state=True,
    grades=("again", "hard", "good", "easy"),
    idempotency="ClientRequestId accepted (max 64); server cache is written after commit",
    review_history="GET /srs/review-history/{wordId}/{readingIndex}",
    reconciliation="history can prove that reviews exist, but does not echo ClientRequestId",
    mutation_retry="single-send; timeout/network/5xx => outcome_unknown",
    strict_gate_supported=True,
    strict_gate_reason=(
        "Pudge uses study-batch with extraNewCards=0, requires prior review history, revalidates the card "
        "immediately before submit, and never counts or blindly retries ambiguous outcomes"
    ),
)

JPDB_CAPABILITIES = ProviderCapabilities(
    provider="jpdb",
    api_base=JPDB_API_BASE,
    api_source="JPDB v1 endpoints corroborated by maintained clients; no reviewed official exactly-once contract",
    source_revision="unverified",
    auth="Bearer",
    account_identity="credential_fingerprint",
    card_id_namespace="jpdb",
    card_id_shape="(vid, sid)",
    enumerate_existing_reviewable=False,
    previous_review_evidence=False,
    due_state=False,
    grades=("fail", "hard", "okay", "easy"),
    idempotency="not proven",
    review_history="not proven for attempt reconciliation",
    reconciliation="unsupported",
    mutation_retry="single-send; timeout/network/5xx => outcome_unknown",
    strict_gate_supported=False,
    strict_gate_reason=(
        "native review enumeration, previous-review evidence and exactly-once/reconciliation semantics are not proven"
    ),
)


def credential_account_key(provider: str, token: str) -> str:
    value = str(token or "")
    if not value:
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"{provider.casefold()}:{digest}"


def provider_capabilities(provider: str, token: str = "") -> dict[str, Any]:
    name = str(provider or "jiten").casefold()
    caps = JPDB_CAPABILITIES if name == "jpdb" else JITEN_CAPABILITIES
    return caps.as_dict(
        configured=bool(token),
        account_key=credential_account_key(caps.provider, token),
    )


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        payload = None
    if isinstance(payload, dict):
        detail = payload.get("error_message") or payload.get("detail") or payload.get("message")
        if detail:
            return str(detail)
    return ""


def _json_or_error(response: httpx.Response, provider: str) -> Any:
    try:
        return response.json() if response.content else {}
    except ValueError as exc:
        raise ReviewProviderError(f"{provider} returned invalid JSON") from exc


class JitenReviewProvider:
    def __init__(self, token: str) -> None:
        self.token = str(token or "")
        if not self.token:
            raise ReviewProviderError("Jiten API token is not configured")

    @property
    def account_key(self) -> str:
        return credential_account_key("jiten", self.token)

    @staticmethod
    def capabilities(token: str = "") -> dict[str, Any]:
        return provider_capabilities("jiten", token)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"ApiKey {self.token}",
            "User-Agent": APP_SLUG,
        }

    def list_review_candidates(
        self,
        *,
        limit: int = 20,
        session_id: str = "",
        extra_reviews: int | None = None,
    ) -> dict[str, Any]:
        # Jiten's StudyController allows up to 500 extra reviews. The strict gate
        # may deliberately warm more than one episode worth of cards, but never
        # asks for new cards.
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 500)), "extraNewCards": 0}
        if extra_reviews is not None:
            params["extraReviews"] = max(0, min(int(extra_reviews), 500))
        if session_id:
            params["sessionId"] = session_id
        response = httpx.get(
            f"{JITEN_API_BASE}/srs/study-batch",
            headers=self._headers(),
            params=params,
            timeout=30,
        )
        if response.status_code >= 400:
            detail = _error_detail(response)
            raise ReviewProviderError(
                f"Jiten HTTP {response.status_code}{': ' + detail if detail else ''}"
            )
        payload = _json_or_error(response, "Jiten")
        if not isinstance(payload, dict):
            raise ReviewProviderError("Jiten study-batch returned an invalid response")
        cards = [
            card
            for card in (payload.get("cards") or [])
            if isinstance(card, dict) and not bool(card.get("isNewCard"))
        ]
        return {
            "provider": "jiten",
            "account_key": self.account_key,
            "session_id": str(payload.get("sessionId") or session_id or ""),
            "cards": cards,
            "reviews_remaining": int(payload.get("reviewsRemaining") or 0),
        }

    def list_strict_review_candidates(
        self,
        *,
        required: int,
        exclude_keys: set[str] | None = None,
        trusted_previous_keys: set[str] | None = None,
    ) -> dict[str, Any]:
        wanted = max(1, min(int(required), 500))
        excluded = set(exclude_keys or set())
        trusted = set(trusted_previous_keys or set())

        # Jiten clamps study-batch `limit` to the user's configured BatchSize
        # (commonly 8), even when extraReviews is much larger. The batch itself
        # is slightly randomized, so collect a few batches into one local pool.
        # This stays read-only, never asks for new cards, and stops after two
        # no-growth rounds so a small/stable provider set cannot create a request
        # storm. The WebApp keeps the resulting pool between refreshes.
        session_id = ""
        raw_by_key: dict[str, tuple[int, dict[str, Any], int, int]] = {}
        order = 0
        no_growth_rounds = 0
        max_rounds = min(12, max(2, (wanted + 7) // 8 + 2))
        last_reviews_remaining = 0
        provider_available_estimate: int | None = None
        rounds_used = 0
        for _round in range(max_rounds):
            rounds_used += 1
            before = len(raw_by_key)
            batch = self.list_review_candidates(
                limit=500,
                session_id=session_id,
                extra_reviews=wanted,
            )
            session_id = str(batch.get("session_id") or session_id or "")
            last_reviews_remaining = int(batch.get("reviews_remaining") or 0)
            batch_cards = [card for card in (batch.get("cards") or []) if isinstance(card, dict)]
            if provider_available_estimate is None:
                # extraNewCards=0 means this is the provider's due-review set.
                # Jiten reports how many reviews remain after the returned batch,
                # so this gives us the useful `min(provider available, X*Y)` cap
                # even though the returned batch itself is clamped to BatchSize.
                provider_available_estimate = len(batch_cards) + last_reviews_remaining
            effective_wanted = min(wanted, max(0, provider_available_estimate))
            for card in batch_cards:
                try:
                    word_id = int(card.get("wordId"))
                    reading_index = int(card.get("readingIndex"))
                except (TypeError, ValueError):
                    continue
                key = f"{word_id}:{reading_index}"
                if key in excluded or key in raw_by_key:
                    continue
                raw_by_key[key] = (order, card, word_id, reading_index)
                order += 1
                if len(raw_by_key) >= effective_wanted:
                    break
            if len(raw_by_key) >= effective_wanted:
                break
            if not batch_cards:
                break
            if len(raw_by_key) == before:
                no_growth_rounds += 1
                if no_growth_rounds >= 2:
                    break
            else:
                no_growth_rounds = 0

        # review-history is independent per card. A key already proven during this
        # process lifetime can skip the repeated read: strict_review_submit() still
        # revalidates immediately before every mutation, so stale history can never
        # create a new card. Preserve first-seen study-batch ordering.
        pending: list[tuple[int, dict[str, Any], int, int, str]] = []
        eligible_by_position: dict[int, dict[str, Any]] = {}
        for key, (position, card, word_id, reading_index) in raw_by_key.items():
            if key in trusted:
                scoped = dict(card)
                scoped["pudgeCardKey"] = key
                scoped["pudgePreviouslyReviewed"] = True
                scoped["pudgeProvider"] = "jiten"
                eligible_by_position[position] = scoped
                continue
            pending.append((position, card, word_id, reading_index, key))

        validation_errors: list[ReviewProviderError] = []
        # Background warming must not monopolize the Jiten connection while the
        # user opens an ordinary study card. Three concurrent history lookups are
        # enough to hide latency without causing the random UI stalls seen with 6.
        max_workers = min(3, max(1, len(pending)))
        if pending:
            with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="pudge-jiten-review") as pool:
                futures = {
                    pool.submit(self.lookup_state, word_id, reading_index):
                    (position, card, key)
                    for position, card, word_id, reading_index, key in pending
                }
                for future in as_completed(futures):
                    position, card, key = futures[future]
                    try:
                        state = future.result()
                    except ReviewProviderError as exc:
                        validation_errors.append(exc)
                        continue
                    if not state.get("exists") or not state.get("previously_reviewed"):
                        continue
                    # Never warm terminal cards. study-batch normally filters these,
                    # and this explicit check keeps the invariant local to Pudge.
                    if int(state.get("state") if state.get("state") is not None else -1) in {0, 4, 5, 6}:
                        continue
                    scoped = dict(card)
                    scoped["pudgeCardKey"] = key
                    scoped["pudgePreviouslyReviewed"] = True
                    scoped["pudgeProvider"] = "jiten"
                    eligible_by_position[position] = scoped

        if not eligible_by_position and validation_errors:
            raise validation_errors[0]
        eligible = [eligible_by_position[pos] for pos in sorted(eligible_by_position)][:wanted]
        return {
            "provider": "jiten",
            "account_key": self.account_key,
            "session_id": session_id,
            "cards": eligible,
            "requested": wanted,
            "available": len(eligible),
            "reviews_remaining": last_reviews_remaining,
            "provider_available_estimate": int(provider_available_estimate or 0),
            "batch_rounds": rounds_used,
        }

    def validate_strict_review_candidate(self, word_id: int, reading_index: int) -> dict[str, Any]:
        state = self.lookup_state(int(word_id), int(reading_index))
        if not state.get("exists"):
            raise ReviewProviderError("Jiten card no longer exists; refusing to create a new card")
        if not state.get("previously_reviewed"):
            raise ReviewProviderError("Jiten card has no previous review history; new cards are forbidden by the gate")
        if int(state.get("state") if state.get("state") is not None else -1) in {0, 4, 5, 6}:
            raise ReviewProviderError("Jiten card is not currently reviewable; refusing strict gate review")
        return state

    def lookup_state(self, word_id: int, reading_index: int) -> dict[str, Any]:
        response = httpx.get(
            f"{JITEN_API_BASE}/srs/review-history/{int(word_id)}/{int(reading_index)}",
            headers=self._headers(),
            timeout=30,
        )
        if response.status_code >= 400:
            detail = _error_detail(response)
            raise ReviewProviderError(
                f"Jiten HTTP {response.status_code}{': ' + detail if detail else ''}"
            )
        payload = _json_or_error(response, "Jiten")
        if not isinstance(payload, dict):
            raise ReviewProviderError("Jiten review-history returned an invalid response")
        reviews = payload.get("reviews") or []
        card = payload.get("card") if isinstance(payload.get("card"), dict) else None
        return {
            "provider": "jiten",
            "account_key": self.account_key,
            "card_id": {"word_id": int(word_id), "reading_index": int(reading_index)},
            "exists": card is not None,
            "previously_reviewed": bool(reviews),
            "review_count": len(reviews) if isinstance(reviews, list) else 0,
            "state": card.get("state") if card else None,
            "due": card.get("due") if card else None,
            "last_review": card.get("lastReview") if card else None,
        }

    def submit_review(
        self,
        word_id: int,
        reading_index: int,
        grade: str,
        *,
        attempt_id: str,
        session_id: str = "",
        review_duration_ms: int | None = None,
    ) -> dict[str, Any]:
        attempt = str(attempt_id or "").strip()
        if not attempt or len(attempt) > 64:
            raise ReviewProviderError("Jiten review requires a stable attempt_id of at most 64 characters")
        rating = {"again": 1, "hard": 2, "good": 3, "easy": 4}.get(str(grade).casefold())
        if rating is None:
            raise ReviewProviderError("Unsupported Jiten review grade")
        body: dict[str, Any] = {
            "wordId": int(word_id),
            "readingIndex": int(reading_index),
            "rating": rating,
            "clientRequestId": attempt,
        }
        if session_id:
            body["sessionId"] = str(session_id)
        if review_duration_ms is not None:
            body["reviewDuration"] = max(0, min(int(review_duration_ms), 60_000))
        try:
            response = httpx.post(
                f"{JITEN_API_BASE}/srs/review",
                headers=self._headers(),
                json=body,
                timeout=30,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ReviewOutcomeUnknown("jiten", attempt, f"Jiten review outcome is unknown: {exc}") from exc
        if response.status_code == 408 or response.status_code >= 500:
            raise ReviewOutcomeUnknown(
                "jiten", attempt, f"Jiten review outcome is unknown after HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            detail = _error_detail(response)
            raise ReviewProviderError(
                f"Jiten HTTP {response.status_code}{': ' + detail if detail else ''}"
            )
        try:
            result = _json_or_error(response, "Jiten")
        except ReviewProviderError as exc:
            raise ReviewOutcomeUnknown("jiten", attempt, str(exc)) from exc
        if isinstance(result, dict) and result.get("error_message"):
            raise ReviewProviderError(str(result["error_message"]))
        return {
            "ok": True,
            "outcome": "confirmed",
            "provider": "jiten",
            "account_key": self.account_key,
            "attempt_id": attempt,
            "result": result,
        }

    def reconcile_attempt(self, word_id: int, reading_index: int, attempt_id: str) -> dict[str, Any]:
        state = self.lookup_state(word_id, reading_index)
        return {
            "provider": "jiten",
            "attempt_id": str(attempt_id or ""),
            "status": "indeterminate",
            "reason": "Jiten review history does not expose ClientRequestId",
            "state": state,
        }


class JpdbReviewProvider:
    def __init__(self, token: str) -> None:
        self.token = str(token or "")
        if not self.token:
            raise ReviewProviderError("JPDB API token is not configured")

    @property
    def account_key(self) -> str:
        return credential_account_key("jpdb", self.token)

    @staticmethod
    def capabilities(token: str = "") -> dict[str, Any]:
        return provider_capabilities("jpdb", token)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": APP_SLUG,
        }

    def submit_review(
        self,
        vid: int,
        sid: int,
        grade: str,
        *,
        attempt_id: str,
    ) -> dict[str, Any]:
        attempt = str(attempt_id or "").strip()
        if not attempt:
            raise ReviewProviderError("JPDB review requires a local attempt_id")
        mapped = {
            "again": "fail",
            "hard": "hard",
            "good": "okay",
            "easy": "easy",
            "fail": "fail",
            "okay": "okay",
        }.get(str(grade).casefold())
        if mapped is None:
            raise ReviewProviderError("Unsupported JPDB review grade")
        try:
            response = httpx.post(
                f"{JPDB_API_BASE}/review",
                headers=self._headers(),
                json={"vid": int(vid), "sid": int(sid), "grade": mapped},
                timeout=30,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ReviewOutcomeUnknown("jpdb", attempt, f"JPDB review outcome is unknown: {exc}") from exc
        if response.status_code == 408 or response.status_code >= 500:
            raise ReviewOutcomeUnknown(
                "jpdb", attempt, f"JPDB review outcome is unknown after HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            detail = _error_detail(response)
            raise ReviewProviderError(
                f"JPDB HTTP {response.status_code}{': ' + detail if detail else ''}"
            )
        try:
            result = _json_or_error(response, "JPDB")
        except ReviewProviderError as exc:
            raise ReviewOutcomeUnknown("jpdb", attempt, str(exc)) from exc
        return {
            "ok": True,
            "outcome": "confirmed",
            "provider": "jpdb",
            "account_key": self.account_key,
            "attempt_id": attempt,
            "result": result,
        }

    def reconcile_attempt(self, _vid: int, _sid: int, attempt_id: str) -> dict[str, Any]:
        return {
            "provider": "jpdb",
            "attempt_id": str(attempt_id or ""),
            "status": "unsupported",
            "reason": "No verified JPDB attempt-id/review-history reconciliation contract",
        }
