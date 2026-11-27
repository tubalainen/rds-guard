# RDS Guard uplink protocol, version 2

This is how a **satellite** (an RDS Guard installation with
`AGGREGATOR_URL`, or `UPLINK_URL`, set) reports to an **aggregator**. The satellite makes outbound
HTTPS requests only, so it works from behind NAT. Nothing on the satellite
needs to be reachable from the internet.

The satellite sends raw data only:

- **events**: traffic announcements and emergency broadcasts, with their RDS
  context and timestamps
- **audio**: the recording of each event, as OGG/Opus
- **heartbeat**: station and pipeline status every 30 seconds

Version 2 (v1.0.0) adds **joining**: a satellite without a configured token
makes its own and asks to join, and the aggregator's owner approves it. It
also adds the `receiver` of an event. An aggregator answers every request
that carries another protocol version with 426, so satellites and
aggregators are updated together.

Transcriptions are never sent. The aggregator transcribes the audio itself.

## Common rules

| | |
|---|---|
| Base path | `{AGGREGATOR_URL}/api/v1/ingest` |
| Authentication | `Authorization: Bearer <node token>` on every request |
| Protocol header | `X-RDS-Guard-Protocol: 2` on every request **and every aggregator response, errors included** |
| User agent | `rds-guard/<version>` |
| Timestamps | UTC, ISO 8601 with `Z` (`2026-11-25T10:00:00Z`), or `null` |
| Redirects | Not followed |

The aggregator always includes the protocol header in its responses. A
satellite treats any response *without* it as coming from something other
than the aggregator, such as a reverse proxy, a Cloudflare error page or a
wrong URL. It then keeps the item queued and retries; it never discards data
because of such a response.

### Response status codes

| Status | Meaning | Satellite action |
|---|---|---|
| 2xx | Accepted | Remove item from queue |
| 401 | Missing or unknown token | Keep queue, pause 5 min, show "bad token" |
| 403 | Node disabled by the aggregator's owner | As 401 |
| 404 | Unknown event (audio upload, delete) | Delete: success. Audio: drop item |
| 408, 425, 429, 5xx | Transient | Keep queue, retry with backoff 5 s → 5 min |
| 426 | Protocol header missing or version unsupported | Keep queue, pause 5 min, show "update needed" |
| Other 4xx (409, 413, 415, 422 …) | Item refused for good | Drop item, log the response body |

Error bodies are JSON: `{"error": "<code>", "detail": "<text>"}`.

### Delivery and ordering

The satellite records every relevant event change in a local outbox in the
same database transaction as the change. Two workers send it: one for event
snapshots and deletes, one for audio, each oldest first, each retrying its
head until it gets an answer. A new broadcast is therefore reported while an
earlier recording is still uploading. A retry wait is cut short when
something new is queued. Consequences:

- An event snapshot is always sent before that event's audio: an audio item
  waits while a snapshot or delete of its event is queued before it.
- A queued item older than `UPLINK_MAX_AGE_HOURS` (default 168) is dropped.
- Nothing is sent until the satellite has joined; events are queued meanwhile.
- Several queued changes to the same event are collapsed into one snapshot,
  read from the database at send time.

All requests are idempotent and safe to repeat.

## `POST /join`

Sent by a satellite that has no configured token (`UPLINK_TOKEN` empty).
Once, it makes a token of its own (at least 32 characters) and a `join_id`
(a lowercase UUID) and keeps both. It then sends this request with
`Authorization: Bearer <its own token>`, every 15 seconds until approved.
The aggregator stores only a hash of the token; approving the request makes
that hash the node's token. Nothing secret is sent back, so an answer lost
on the way only means asking again.

Body (JSON, at most 64 KiB):

| Field | Type | Constraint |
|---|---|---|
| `join_id` | string | lowercase UUID, the same on every request |
| `node_name` | string or null | ≤ 64 chars, a suggestion for the node's name |
| `version` | string or null | ≤ 64 chars |
| `suggested_location` | string or null | ≤ 80 chars, "Ort, Län" |
| `stations` | array | ≤ 8 items: `frequency`, `pi`, `ps` (shown to the owner) |
| `join_key` | string | Optional: approves at once when it equals the aggregator's `JOIN_KEY` |

Responses:

- `202 {"status": "pending", "verify": "K7Q-3PD"}`: waiting for the owner.
  `verify` is derived from `join_id` (SHA-256, the first six bytes mapped to
  `ABCDEFGHJKLMNPQRSTUVWXYZ23456789`, as `XXX-XXX`); the satellite logs the
  same code, so the owner approves the right one.
- `200 {"status": "approved", "node": "<name>"}`: the token is a node's
  token now, enabled. Also the answer to every later join with that token.
- `403 {"error": "rejected"}`: the owner rejected the request, or
  `join_key` does not match (deliberately the same answer), or the node is
  disabled (`node_disabled`).
- `409`: the `join_id` was first sent with another token.
- `401`: no token, or one shorter than 32 characters.
- `422`: validation failed.
- `429`: too many requests from this address or in total
  (`JOIN_RATE_PER_MIN`), or too many satellites waiting (`JOIN_MAX_PENDING`).

Waiting and rejected requests that are not repeated for `JOIN_PENDING_HOURS`
(default 24) are forgotten. A node removed by the owner answers 401 to its
satellite, which then asks to join again.

## `PUT /events/{event_uid}`

Creates or replaces the aggregator's copy of one event. `event_uid` is a
lowercase UUID generated by the satellite.

Body (JSON, at most 64 KiB):

| Field | Type | Constraint |
|---|---|---|
| `rev` | integer | ≥ 0. Increases with every change on the satellite |
| `type` | string | `traffic` or `emergency` |
| `state` | string | `start`, `update`, `active` or `end` |
| `pi` | string | 1–16 chars, e.g. `0x9E04` |
| `station_ps` | string or null | ≤ 64 chars |
| `long_ps` | string or null | ≤ 64 chars |
| `frequency` | string or null | ≤ 16 chars, e.g. `103.3M` |
| `prog_type` | string or null | ≤ 64 chars |
| `radiotext` | array of strings | ≤ 50 items, each ≤ 128 chars |
| `started_at` | timestamp or null | |
| `ended_at` | timestamp or null | |
| `duration_sec` | integer or null | 0–86400 |
| `sent_at` | timestamp | Satellite clock when sent |
| `receiver` | string or null | ≤ 32 chars, which of the satellite's receivers (dongles) heard it: its `name` from `receivers.yml`, or its serial number |

Unknown fields are ignored.

Each `event_uid` is one node's **reception**. The aggregator folds receptions
of the same broadcast into one event: same `type` and `pi`, starting within
`MATCH_WINDOW_SEC` (30) of it or while it is going on, on the aggregator's
clock (each node's measured clock offset applied). That event keeps the
`event_uid` of its first reception. A `409` is returned when the `event_uid`
belongs to another node.

Responses:

- `200 {"status": "stored"}`: the snapshot was saved.
- `200 {"status": "stale"}`: the aggregator already has this `rev` or a newer
  one, so nothing changed.
- `409`: the `event_uid` belongs to another node.
- `413`: the body is larger than 64 KiB.
- `422`: validation failed.

## `PUT /events/{event_uid}/audio`

Uploads the recording of an event. Body: raw OGG/Opus bytes,
`Content-Type: audio/ogg`, at most 8 MiB. Uploading again replaces the file.

Responses:

- `200 {"status": "stored"}`
- `404`: no such event for this node.
- `413`: the file is too large.
- `415`: the body is not OGG, i.e. does not start with `OggS`.

## `DELETE /events/{event_uid}`

Sent when the satellite discards an event as too short (under 10 seconds),
because the aggregator may already have received its start. Local clean-up,
such as the retention purge or "clear all events", is **not** forwarded.

Responses:

- `200 {"status": "deleted"}`
- `404`: unknown event, which the satellite treats as success.

## `POST /heartbeat`

Sent every `UPLINK_HEARTBEAT_SEC` seconds (default 30), and three seconds
after start-up.

Body (JSON, at most 64 KiB):

| Field | Type | Constraint |
|---|---|---|
| `node_name` | string | ≤ 64 chars, free text set by the satellite owner |
| `version` | string | ≤ 64 chars |
| `sent_at` | timestamp | |
| `uptime_sec` | integer or null | |
| `receivers` | array | ≤ 8 items, one per dongle (see below) |
| `pipeline` | object | `{"state": string, "error": string or null}`, each ≤ 256 chars: the worst of the receivers |
| `stations` | array | ≤ 8 items: `frequency`, `pi`, `ps`, `long_ps`, `prog_type`, `tp`, `ta`, `groups_per_sec`, `groups_total`, all receivers together |
| `queue_depth` | integer or null | Items waiting in the satellite's outbox |
| `suggested_location` | string or null | ≤ 80 chars, the satellite's `NODE_LOCATION` as "Ort, Län" (e.g. `Karlstad, Värmlands län`). The aggregator places the node's messages there unless its owner set another place in Admin. Optional (`location` is read too) |

Each item of `receivers`:

| Field | Type | Constraint |
|---|---|---|
| `id` | string | ≤ 32 chars, the `receiver` its events carry |
| `serial` | string | ≤ 32 chars, the dongle's serial number |
| `name` | string or null | ≤ 32 chars, from `receivers.yml` |
| `state` | string | ≤ 32 chars: `not_started`, `starting`, `running`, `paused` (calibrating), `error`, `stopped` |
| `error` | string or null | ≤ 256 chars |
| `gain`, `ppm` | number or null | In use now |
| `gain_mode`, `ppm_mode` | string | `auto` (calibrated) or `manual` (fixed in `receivers.yml`) |
| `calibrated_at` | timestamp or null | Last calibration |
| `calibration` | string or null | ≤ 64 chars: what the calibration is doing, or why it failed |
| `quality` | object | ≤ 8 entries, frequency → RDS groups per second at calibration |
| `stations` | array | ≤ 4 items, as `stations` above |

`pipeline` and `stations` repeat what `receivers` holds, for node cards that
read the flat shape. Unknown fields are ignored and the optional ones may be
missing.

Response: `200 {"server_time": "<timestamp>"}`. The aggregator compares
`sent_at` with its own clock to show each node's clock offset.

The answer may also carry `commands`, an array of what the node's owner
asked for in Admin. Each is sent once and then forgotten by the aggregator;
the satellite runs each `id` at most once and ignores types it does not
know.

| `type` | Fields | What the satellite does |
|---|---|---|
| `recalibrate` | `id` (integer), `receiver` (its `id` or `serial`) | Calibrates that receiver's gain and ppm as soon as no broadcast is going on there; the result shows in a later heartbeat |
