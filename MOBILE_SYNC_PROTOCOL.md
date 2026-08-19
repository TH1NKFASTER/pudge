# Pudge Companion Sync Protocol v1

This protocol is the backend boundary for future iOS, iPadOS, web, and other
companion clients. Clients never read Pudge's SQLite tables directly.

## Design

- Every library item is exposed as an opaque `entity_id` UUID.
- Progress changes are append-only events with an integer cursor.
- `sync_snapshots` stores the currently accepted position for fast startup.
- Local Pudge progress is detected lazily before library/change responses.
- Incoming events are idempotent by `event_id`.
- An older offline event remains in history but does not replace a newer
  snapshot.
- Access tokens are returned once and only their SHA-256 hashes are stored.

Supported entities:

- `anime_episode`: episode plus playback/duration milliseconds
- `manga`: page index and page count
- `light_novel`: chapter, canonical character offset, chapter hash, and fraction
- `audiobook`: playback/duration milliseconds and speed

LN and audiobook entities can have a `read_with_audio` relation. Alignment data
remains a separate media artifact and can later be downloaded by a companion
client without changing the progress protocol.

## Pairing

The desktop process calls `companion_start_pairing()` through the local
pywebview bridge. It returns a short-lived `pairing_token` intended for a QR
code. A client completes pairing with:

```http
POST /api/v1/pair/complete
Content-Type: application/json

{
  "pairing_token": "...",
  "name": "Maksim's iPad",
  "platform": "ipados"
}
```

The response contains `device_id` and `access_token`. Later requests use:

```http
Authorization: Bearer <access_token>
```

## Endpoints

- `GET /api/v1/health`
- `POST /api/v1/pair/complete`
- `GET /api/v1/library`
- `GET /api/v1/sync/changes?cursor=0&limit=200`
- `POST /api/v1/sync/events`

Example progress event:

```json
{
  "events": [
    {
      "event_id": "device-generated-uuid",
      "entity_id": "server-issued-entity-uuid",
      "type": "progress.updated",
      "occurred_at": 1787060000.0,
      "payload": {
        "position": {
          "chapter_index": 3,
          "character_offset": 18422,
          "chapter_length": 25100,
          "chapter_hash": "...",
          "fraction": 0.7339
        },
        "status": "in_progress"
      }
    }
  ]
}
```

## Network exposure

The companion server is disabled by default. Configuration:

```toml
[companion]
enabled = false
bind_host = "127.0.0.1"
port = 47821
pairing_ttl_seconds = 300.0
max_events_per_request = 500
```

Use `0.0.0.0` only on a trusted LAN. Bonjour discovery, TLS, remote relay,
media streaming, and downloadable offline bundles are intentionally outside
v1; they can be added without changing entity IDs or progress events.
