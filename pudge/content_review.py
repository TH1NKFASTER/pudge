from __future__ import annotations

import hashlib
import json
import secrets
import threading
from pathlib import Path
from typing import Any

from .review_gate import ContentReviewIdentity, ReviewGateStore, _state_key, review_card_key
from .review_episode_due import build_text_due_review_cards
from .review_providers import credential_account_key, ReviewOutcomeUnknown
from .review_actions import resolve_wire_grade


class ContentReviewService:
    """Reviews scoped to an account, immutable source revision and reading part."""

    def __init__(self, api: Any) -> None:
        self.api = api
        self.store = ReviewGateStore(api.manager.db, prefix="content_review:v1")
        self.lock = threading.RLock()
        self.sessions: dict[str, dict[str, Any]] = {}

    def source(self, kind: str, book_id: int, part: int, *, prepare: bool) -> tuple[ContentReviewIdentity, str]:
        if kind == "ln":
            book = self.api.light_novels.book(book_id)
            if not Path(book["file_path"]).is_file():
                raise FileNotFoundError("Light novel source was removed")
            with self.api.light_novels._connection() as conn:
                row = conn.execute("SELECT text,text_hash FROM ln_chapters WHERE book_id=? AND chapter_index=?",
                                   (book_id, part)).fetchone()
            if row is None:
                raise ValueError("Light novel chapter was removed")
            text = str(row["text"])
            revision = str(row["text_hash"])
            identity = ContentReviewIdentity(kind, f"{book_id}:{book['created_at']}:{book['file_path']}", revision, part)
        elif kind == "manga":
            book = dict(self.api.manga._book(book_id))
            if not Path(book["path"]).is_file():
                raise FileNotFoundError("Manga source was removed")
            part = max(0, part // 20 * 20)
            lines = []
            for page in range(part, min(part + 20, int(book["page_count"]))):
                result = self.api.manga.text_regions(book_id, page, cached_only=not prepare)
                if result.get("status") in {"missing", "failed"} or result.get("available") is False:
                    raise ValueError("Recognize the next 20 manga pages before reviewing them")
                lines.extend(str(region.get("text") or "") for region in result.get("regions") or [])
            text = "\n".join(lines)
            revision = hashlib.sha256((str(book.get("source_fingerprint") or "") + text).encode()).hexdigest()
            identity = ContentReviewIdentity(kind, f"{book_id}:{book['created_at']}:{book['path']}", revision, part)
        else:
            raise ValueError("Unsupported content review kind")
        return identity, text

    def begin(self, kind: str, book_id: int, part: int) -> dict[str, Any]:
        config = self.api.config.ui
        if not getattr(config, f"review_gate_{kind}_enabled", False):
            return {"blocking": False, "granted": True, "enabled": False}
        settings = self.api.light_novels.settings()
        if settings.study_backend != "jiten" or not settings.jiten_api_key:
            raise ValueError("Configure Jiten before reviewing content")
        account = credential_account_key("jiten", settings.jiten_api_key)
        identity, text = self.source(kind, int(book_id), int(part), prepare=True)
        with self.lock:
            self.store.clear_unearned_grant(account, identity)
            if self.store.snapshot(account, identity)["granted"]:
                return {"blocking": False, "granted": True, "reason": "already_reviewed"}
        scanned = build_text_due_review_cards(self.api.light_novels, text, identity.revision)
        cards = scanned["cards"]
        if not getattr(config, f"review_gate_{kind}_all_due", False):
            cards = cards[:max(1, min(50, int(getattr(config, f"review_gate_{kind}_count", 5))))]
        issued_key = f"content_review_targets:v1:{account}:{identity.logical_id}"
        with self.lock:
            self._require_exists(kind, int(book_id))
            raw = self.api.manager.db.get_state(issued_key, "")
            if raw and json.loads(raw):
                cards = json.loads(raw)
            elif cards:
                self.api.manager.db.set_state(issued_key, json.dumps(cards, ensure_ascii=False))
        snapshot = self.store.snapshot(account, identity)
        excluded = set(snapshot["confirmed_card_keys"]) | set(snapshot["unknown_card_keys"])
        targets = set(snapshot["confirmed_card_keys"]) | set(snapshot["unknown_card_keys"]) | {review_card_key(c["wordId"], c["readingIndex"]) for c in cards}
        token = secrets.token_urlsafe(24)
        session = {"identity": identity, "kind": kind, "book_id": int(book_id), "part": int(part),
                   "account": account, "required": len(targets), "keys": targets,
                   "cards": [c for c in cards if review_card_key(c["wordId"], c["readingIndex"]) not in excluded]}
        with self.lock:
            # P10: content deleted while cards were scanned must not get
            # a fresh session, grant or unknown-words record.
            self._require_exists(kind, int(book_id))
            self.sessions[token] = session
            # Sessions are only authorization snapshots; progress is durable.
            if len(self.sessions) > 64:
                self.sessions.pop(next(iter(self.sessions)))
            progress = self.store.status(account, identity, required=len(targets))
            if progress.granted and targets:
                self.store.grant(account, identity, required=len(targets))
            owned = [issued_key, _state_key(account, identity, "content_review:v1")]
            if kind == "manga":
                unknown_key = f"manga_unknown_words:{identity.logical_id}"
                self.api.manager.db.set_state(unknown_key,
                    json.dumps(scanned.get("unknown_words", []), ensure_ascii=False))
                owned.append(unknown_key)
            self._remember_owned_keys(kind, int(book_id), owned)
        return {**progress.payload(), "blocking": not progress.granted, "token": token,
                "cards": session["cards"], "unknown_words": scanned.get("unknown_words", []),
                "all_due_episode_words": True, "enabled": True, "checked": True,
                "reason": "no_due_cards" if not targets else "review_required"}

    def _require_exists(self, kind: str, book_id: int) -> None:
        owner = getattr(self.api, "light_novels" if kind == "ln" else "manga", None)
        lookup = getattr(owner, "book" if kind == "ln" else "_book", None)
        if not callable(lookup):
            return
        try:
            lookup(book_id)
        except Exception as exc:
            raise ValueError("Content was removed; reopen reviews") from exc

    @staticmethod
    def _owned_keys_index(kind: str, book_id: int) -> str:
        return f"content_review_book_keys:v1:{kind}:{int(book_id)}"

    def _remember_owned_keys(self, kind: str, book_id: int, keys: list[str]) -> None:
        db = self.api.manager.db
        index = self._owned_keys_index(kind, book_id)
        try:
            current = list(json.loads(db.get_state(index, "") or "[]"))
        except (TypeError, ValueError):
            current = []
        merged = list(dict.fromkeys([*map(str, current), *keys]))
        if merged != current:
            db.set_state(index, json.dumps(merged, ensure_ascii=False))

    @classmethod
    def forget_persisted(cls, db: Any, kind: str, book_ids: list[int]) -> None:
        """Drop issued queues, grants and unknown words owned by deleted content."""
        for book_id in {int(value) for value in book_ids}:
            index = cls._owned_keys_index(kind, book_id)
            try:
                keys = list(json.loads(db.get_state(index, "") or "[]"))
            except (TypeError, ValueError):
                keys = []
            for key in keys:
                db.delete_state(str(key))
            db.delete_state(index)

    def forget(self, kind: str, book_ids: list[int]) -> None:
        ids = {int(value) for value in book_ids}
        with self.lock:
            for token in [t for t, s in self.sessions.items() if s["kind"] == kind and int(s["book_id"]) in ids]:
                self.sessions.pop(token, None)
            self.forget_persisted(self.api.manager.db, kind, list(ids))

    def _session(self, token: str) -> dict[str, Any]:
        session = self.sessions.get(str(token))
        if session is None:
            raise ValueError("Review session expired; reopen reviews")
        settings = self.api.light_novels.settings()
        if credential_account_key("jiten", settings.jiten_api_key) != session["account"]:
            raise ValueError("Jiten account changed; reopen reviews")
        identity, _text = self.source(session["kind"], session["book_id"], session["part"], prepare=False)
        if identity != session["identity"]:
            raise ValueError("Content changed or was removed; reopen reviews")
        return session

    def review(self, token: str, word_id: int, reading_index: int, grade: str, attempt_id: str) -> dict[str, Any]:
        with self.lock:
            s = self._session(token)
            key = review_card_key(word_id, reading_index)
            if key not in s["keys"]:
                raise ValueError("Card was not issued for this content")
            before = self.store.status(s["account"], s["identity"], required=s["required"])
            if key in before.confirmed_card_keys or key in before.unknown_card_keys:
                return {**before.payload(), "outcome": "duplicate"}
            wire_grade = resolve_wire_grade(
                "jiten", getattr(self.api.light_novels.settings(), "review_mode", "native"), grade
            )
            try:
                result = self.api.light_novels.strict_review_submit(word_id, reading_index, wire_grade, attempt_id=attempt_id, expected_account_key=s["account"])
            except ReviewOutcomeUnknown:
                result = {"outcome": "unknown"}
            # Do not resurrect deleted content when a provider reply arrives late.
            self._session(token)
            if result.get("outcome") == "unknown":
                progress = self.store.mark_unknown(s["account"], s["identity"], required=s["required"], card_key=key)
            elif result.get("ok") is False:
                raise ValueError(result.get("message") or "Review failed")
            else:
                progress = self.store.mark_confirmed(s["account"], s["identity"], required=s["required"], card_key=key)
            return {**result, **progress.payload(), "blocking": not progress.granted}

    def undo(self, token: str, word_id: int, reading_index: int) -> dict[str, Any]:
        with self.lock:
            s = self._session(token)
            key = review_card_key(word_id, reading_index)
            progress = self.store.status(s["account"], s["identity"], required=s["required"])
            if key not in progress.confirmed_card_keys:
                raise ValueError("This card has no confirmed review in the session")
            result = self.api.light_novels.strict_review_undo(word_id, reading_index, expected_account_key=s["account"])
            self._session(token)
            if result.get("ok") and result.get("outcome") == "undone":
                progress = self.store.unmark_confirmed(s["account"], s["identity"], required=s["required"], card_key=key)
            return {**result, **progress.payload(), "blocking": not progress.granted}
