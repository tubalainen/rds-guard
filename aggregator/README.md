# RDS Guard Aggregator

Collects traffic announcements and emergency broadcasts from several
[RDS Guard](../README.md) installations, called **satellites**, run by friends
at different places, each with its own RTL-SDR and local P4 station.

![RDS Guard architecture: satellites send events and audio over HTTPS through Cloudflare and nginx to the aggregator's ingest API, which stores them, transcribes the audio and publishes MQTT alerts](../docs/architecture/rds-guard.architecture.svg)

The diagram shows one satellite; any number can report to the same
aggregator. An [interactive version](../docs/architecture/README.md) links
each component to its source code.

Satellites send raw data only: event metadata (station, RadioText,
timestamps), the recorded audio (OGG/Opus) and a status heartbeat. The
aggregator transcribes the audio itself. Satellite owners keep their own
local events, transcriptions, web UI and MQTT. The protocol is in
[docs/uplink-protocol.md](../docs/uplink-protocol.md).

## What it does

- Receives events, audio and status from any number of satellites, each
  with its own token that can be revoked at any time
- Transcribes all audio centrally with Whisper (local or remote)
- Web UI on your LAN: events from all stations with audio and
  transcription, filters per station and per node, and a **Nodes** view
  (online/offline, station, decode rate, version, queue, clock offset)
- Publishes one MQTT alert per event, with the transcription, for all
  stations to one broker (Home Assistant etc.)
- Read API for other programs
- Keeps 90 days of events by default

## Quick start

You need a Linux host with Docker, and a domain on Cloudflare.

1. Get the files:

```bash
git clone https://github.com/tubalainen/rds-guard.git
cd rds-guard/aggregator
cp .env.example .env
```

2. Edit `.env`: set `PUBLIC_URL` to the address satellites will use, for
   example `https://rds.example.com`.

3. Put the ingest API on the internet (see [Cloudflare](#cloudflare)). With
   a Cloudflare Tunnel:

```bash
docker compose --profile tunnel up -d
```

4. Open the web UI at **http://your-host:8080**, go to **Nodes**, add a node
   per friend, and send them the three lines that are shown (see
   [Adding a satellite](#adding-a-satellite)).

To build locally instead of pulling `ghcr.io/tubalainen/rds-guard-aggregator`,
uncomment `build:` in `docker-compose.yml` and run `docker compose up -d --build`.

## Cloudflare

Only one thing is published: nginx, which forwards `/api/v1/ingest/*` to the
aggregator and answers 404 to everything else. **The web UI and admin API
have no login.** Never expose port 8080, and never point Cloudflare at it.

### Option A: Cloudflare Tunnel (recommended)

No open ports on your router, and no public IP address needed.

1. In the Cloudflare dashboard: **Zero Trust → Networks → Tunnels → Create
   a tunnel**, type *Cloudflared*. Copy the tunnel token into `.env` as
   `CLOUDFLARE_TUNNEL_TOKEN=...`.
2. Add a **public hostname** to the tunnel:
   - Hostname: e.g. `rds.example.com`
   - Path: `api/v1/ingest/*` (optional extra lock; nginx already refuses the rest)
   - Service: `HTTP`, URL `nginx:8080`
3. Start with the tunnel profile: `docker compose --profile tunnel up -d`.

### Option B: proxied DNS to your own server

If the host already has a public address and a TLS-terminating reverse
proxy (Caddy, Traefik, nginx), point that proxy at nginx on
`127.0.0.1:8088` (`NGINX_BIND` / `NGINX_PORT` in `.env`) and set the DNS
record to *Proxied* in Cloudflare with SSL mode **Full (strict)**. Allow
connections to the origin only from
[Cloudflare's address ranges](https://www.cloudflare.com/ips/), and add those
ranges as `set_real_ip_from` in `nginx/ingest.conf` so the per-client rate
limit sees real client addresses.

### Bot protection

Satellites are programs, not browsers. If a satellite shows `uplink
offline` with an HTTP 403 from Cloudflare in its log, Cloudflare's bot
protection is blocking it. Turn off **Bot Fight Mode** for the zone, or, on
plans that support it, add a WAF rule that skips bot checks for URI paths
starting with `/api/v1/ingest/`. Satellites never discard data because of
such answers; they keep it queued and retry.

## Adding a satellite

**In the web UI:** Nodes → type a name (e.g. `Kalle, Karlstad`) → *Add
node*. The token is shown once, together with the lines to send.

**Or from the command line:**

```bash
docker compose exec aggregator python3 -m aggregator.nodes add "Kalle, Karlstad"
```

Other commands: `list`, `rotate`, `disable`, `enable`, `remove`. Only a
hash of each token is stored, so a lost token cannot be shown again;
rotate it instead.

### Instructions for the satellite owner

Send your friend the three lines and a link to
[Run as a satellite](../README.md#run-as-a-satellite) in the main README. In
short:

1. Install RDS Guard by following its [Quick start](../README.md#quick-start),
   with the frequency of the local P4 station, and check that it decodes.
2. Add the three lines to `.env`:

```bash
UPLINK_URL=https://rds.example.com
UPLINK_TOKEN=…
NODE_NAME=Kalle, Karlstad
```

3. Restart with `docker compose up -d`.

The satellite's status bar shows `uplink` in green when it is connected, and
the node turns green under **Nodes** here within a minute. No ports need to be
opened at the satellite.

## Configuration

All settings are in `.env`; see [.env.example](.env.example).

| Variable | Default | Description |
|---|---|---|
| `PUBLIC_URL` | | Address satellites use; shown in their setup lines |
| `CLOUDFLARE_TUNNEL_TOKEN` | | Token for the `tunnel` profile |
| `WEB_UI_BIND` / `WEB_UI_PORT` | `0.0.0.0` / `8080` | Where the web UI is published on the host. Use `127.0.0.1` on a server with a public address |
| `NGINX_BIND` / `NGINX_PORT` | `127.0.0.1` / `8088` | Where nginx is published on the host (not needed with the tunnel) |
| `EVENT_RETENTION_DAYS` | `90` | Delete events and audio older than this |
| `TRANSCRIPTION_ENGINE` | `local` | `local`, `remote` or `none` (same as on the satellite) |
| `TRANSCRIPTION_LANGUAGE` | `sv` | Whisper language |
| `TRANSCRIPTION_MODEL` | `small` | Local model size |
| `TRANSCRIPTION_DEVICE` | `cpu` | `cpu` or `cuda` |
| `WHISPER_REMOTE_URL` | | Remote ASR server for `remote` |
| `MQTT_ENABLED` | `false` | Publish to MQTT |
| `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER`, `MQTT_PASSWORD` | | Broker |
| `MQTT_TOPIC_PREFIX` | `rds-aggregator` | Different from the satellites' `rds`, so both can share a broker |
| `ALERT_AUDIO_WAIT_SEC` | `90` | After an event ends, wait this long for its audio |
| `ALERT_TRANSCRIPTION_WAIT_SEC` | `300` | After the audio arrived, wait this long for the transcription |
| `ALERT_MAX_DELAY_MIN` | `60` | Events that ended longer ago (delivered late after an outage) are stored but not alerted |
| `NODE_OFFLINE_SEC` | `120` | A node is offline after this long without a heartbeat |
| `INGEST_RATE_PER_MIN` / `INGEST_BURST` | `120` / `60` | Request budget per node |

The first local transcription downloads the Whisper model into the data
volume.

## MQTT topics

```
rds-aggregator/alert                         # One message per finished event (see below)
rds-aggregator/{pi}/traffic/ta               # TA active true/false (retained)
rds-aggregator/{pi}/{type}/transcription     # Latest transcription per station (retained)
rds-aggregator/nodes/{node}/status           # Node online/offline (retained)
rds-aggregator/aggregator/status             # Aggregator online/offline (retained, last will)
```

`alert` uses the same fields as a satellite's `rds/alert` (`event_type`,
`state`, `station`, `frequency`, `started`, `ended`, `duration_sec`,
`radiotext`, `audio_available`, `transcribed_text`,
`transcription_status`, `event_id`, `timestamp`) plus `node.name`. It is
published once per event, after the transcription is done or after the
wait times above, and never twice, even across restarts.
`{node}` is the node name in lower case, with å/ä/ö turned into a/a/o and
other characters into `-`.

## Read API (LAN)

| Endpoint | Description |
|---|---|
| `GET /api/v1/events` | Events, newest first. Params: `type`, `pi`, `node`, `since`, `active` (`1`/`0`), `limit` (≤ 200), `offset` |
| `GET /api/v1/events/active` | Events in progress |
| `GET /api/v1/events/{event_uid}` | One event |
| `GET /api/v1/audio/{event_uid}.ogg` | Audio |
| `GET /api/v1/stations` | Stations seen, with the nodes that reported them |
| `GET /api/v1/nodes` | Nodes with status |
| `GET /api/v1/status` | Aggregator status |
| `POST /api/v1/nodes` | `{"name": "…"}` → node, token and setup lines |
| `POST /api/v1/nodes/{id}/rotate` | New token |
| `PATCH /api/v1/nodes/{id}` | `{"enabled": true\|false}` |
| `DELETE /api/v1/nodes/{id}` | Remove node and its events |

Write requests must send `Content-Type: application/json` and, from a
browser, come from the same origin.

## Security notes

- Only `/api/v1/ingest/*` is reachable from the internet. Every request
  there needs a node token, and a node can only change its own events.
- Everything a satellite sends is validated and length-limited; audio must
  be OGG, at most 8 MiB, and is stored under a name the aggregator chooses.
- The web UI and admin API have **no login**. Keep port 8080 on your LAN.
- The container runs as an unprivileged user.

## Operations

```bash
docker compose logs -f aggregator                      # logs
docker compose pull && docker compose up -d            # update
docker compose exec aggregator python3 -m aggregator.nodes list
```

Data (database, audio, Whisper model) lives in the `aggregator-data` volume.
