# Pudge Companion sync protocol v1

This is a developer-facing protocol note, so it is intentionally more technical
than the user guide. The companion API lets a phone, tablet, or web client use
Pudge progress and library data without opening the SQLite database. The Mac
remains the source of truth for local files and completed anime episodes.

## Progress model

- Each library item has an opaque `entity_id`.
- Progress changes are append-only events with an integer cursor.
- Repeating an `event_id` is safe; the server applies it only once.
- Older offline progress cannot replace newer progress.
- A completed anime episode cannot be reopened by a stale mobile `in_progress` event. Resetting it requires an explicit desktop progress reset.
- Pairing tokens are short-lived. Access tokens are returned once, and only their hashes are stored.

Supported item kinds:

- `anime_episode`: episode number, playback position, and duration;
- `manga`: page index and page count;
- `light_novel`: chapter, character offset, chapter identity, and fraction;
- `audiobook`: playback position, duration, and speed.

A Light Novel and audiobook may share a `read_with_audio` relation. Audio/text
alignment remains a separate media artifact and does not change the progress
event format.

## Pair a device

The desktop creates a short-lived pairing token suitable for a QR code. The
device completes pairing with:

```http
POST /api/v1/pair/complete
Content-Type: application/json

{
  "pairing_token": "...",
  "name": "My iPad",
  "platform": "ipados"
}
```

The response contains `device_id` and `access_token`. Later requests send:

```http
Authorization: Bearer <access_token>
```

## Sync endpoints

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
      "base_revision": 4,
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

Clients should reload the library after a conflict. The bundled companion page
also refreshes when it returns to the foreground and every 15 seconds while
visible.

## Network access

The companion server is disabled by default:

```toml
[companion]
enabled = false
bind_host = "127.0.0.1"
port = 47821
pairing_ttl_seconds = 300.0
max_events_per_request = 500
```

Use `0.0.0.0` only on a trusted local network. Protocol v1 has no remote relay.
Anime streaming uses the same authenticated local companion service but is
separate from the progress-event format described here.
