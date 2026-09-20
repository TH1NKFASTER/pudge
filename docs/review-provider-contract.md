# Review provider contract (R5)

R5 enables the mandatory pre-episode review gate for **Jiten only**. Provider-native identity/state remains authoritative; Pudge never infers account review state from LN/manga text and never creates a new card to satisfy the gate.

## Gate invariants

- Gate identity is `(provider account, AniList media_id, logical episode)`.
- A completed grant is permanent for that logical episode; reopening Pudge or increasing the configured quota does not revoke it.
- Only unique, confirmed existing-card reviews count. `Again` counts once for that card just like any other confirmed grade.
- Closing the gate cancels only the pending launch. Already confirmed progress is retained.
- An existing grant is checked locally and requires no provider request.
- The normal `play()` path only performs a local gate check. Provider/network work happens outside the play lock.
- New cards are forbidden. A candidate must have previous provider review history and is revalidated immediately before submit.
- A timeout/network error/HTTP 5xx after mutation send is `outcome_unknown`. It does not count and Pudge never blindly retries that card.
- Candidate IDs are provider-native. Jiten `(wordId, readingIndex)` is never treated as JPDB `(vid, sid)`.

## Jiten

Source reviewed: `Sirush/Jiten` at `216df6378db619fc0b803f5695dc925c0311ee84`.

- Base: `https://api.jiten.moe/api`
- Auth: `Authorization: ApiKey ...`
- Native card ID: `(wordId, readingIndex)`
- Candidate enumeration: `GET /srs/study-batch?extraNewCards=0`
- Batch response exposes `sessionId`, `isNewCard`, state and due data.
- Prior-review evidence: `GET /srs/review-history/{wordId}/{readingIndex}`; a non-empty `reviews` list proves the card has previously been reviewed.
- Review: `POST /srs/review`, ratings `1..4` (Again/Hard/Good/Easy).
- Idempotency input: `ClientRequestId` (max 64 chars), plus optional `SessionId`.
- Limitation: `/srs/review` can create a missing card. Therefore Pudge rechecks `exists && previously_reviewed` immediately before every gate mutation.
- Limitation: review history does not echo `ClientRequestId`, and Jiten stores its idempotency cache after the DB commit. Pudge therefore does not claim transactional exactly-once semantics.
- Pudge transport policy: one mutation send per attempt ID. Timeout/network/HTTP 5xx becomes `outcome_unknown`; no blind retry and no quota credit.
- Strict gate: **enabled in R5** under the rules above.

## JPDB

Transport paths corroborated by maintained clients/Jiten import code:

- Base: `https://jpdb.io/api/v1`
- Auth: `Authorization: Bearer ...`
- Native card ID: `(vid, sid)`
- Known read endpoints include `list-user-decks`, `deck/list-vocabulary`, `lookup-vocabulary`; review clients use `/review`.
- Previous-review evidence, attempt-id idempotency and per-attempt reconciliation were not proven from an authoritative contract during R4/R5.
- Pudge policy: JPDB review is allowed only when the caller has an explicit `id_namespace=jpdb`; ambiguous mutation failures are never retried automatically.
- Strict pre-episode gate: **disabled** until those missing capabilities are proven.

## Account identity

Pudge does not expose provider secrets in the review contract. Until a provider exposes a stable account ID through a reviewed endpoint, the gate uses a local `provider:sha256(token)[:20]` credential fingerprint as its account namespace. The token itself is never returned to the UI or written into gate state.

## UI mutation contract

Every review click owns one local `attempt_id`. While it is pending, grade buttons are disabled. A second click cannot create another mutation. Only the same active gate generation may consume the confirmed result. `outcome_unknown` stays uncounted and is excluded from automatic retry.
