# RDS Guard

FM radio traffic and emergency monitoring for Sweden. Decodes RDS data from an RTL-SDR dongle, captures traffic announcements and emergency broadcasts, transcribes the captured audio with ElevenLabs Scribe (falling back to a local, Swedish-tuned Whisper model), sends push notifications over MQTT by rules you set in the web UI, and serves a web archive of everything it has heard.

Built for Sveriges Radio P4, Sweden's primary traffic announcement carrier and emergency broadcast channel (VMA).

<img width="1706" height="1392" alt="image" src="https://github.com/user-attachments/assets/f13e254d-0a31-4c74-b32b-f32b9b7c42bd" />

## What it does

- Tunes to an FM frequency using an RTL-SDR USB dongle
- Decodes all RDS (Radio Data System) data in real time
- Detects traffic announcements, emergency broadcasts, and EON cross-network alerts
- Records broadcast audio during traffic and emergency events, removes the FM pre-emphasis and converts to OGG/WAV via ffmpeg
- Transcribes recorded audio with ElevenLabs Scribe, or locally with the bundled Swedish KB-Whisper model (used automatically as fallback, or without an API key)
- Stores events in a local SQLite database with 30-day retention
- Serves a web archive (in Swedish) with a calendar, search in transcriptions, audio playback and the push rules
- Sends ready-made push messages to MQTT (`rds/notify/<rule>`) for the events that match your rules, e.g. VMA always and traffic that mentions your roads on weekday mornings
- Optionally forwards events and decoded data to an MQTT broker for Home Assistant or other automation
- Optionally reports to a central [aggregator](aggregator/README.md) that collects events from several installations (see [Run as a satellite](#run-as-a-satellite))

## Architecture

### System overview

![RDS Guard architecture: a satellite decodes P4 with an RTL-SDR, stores events and audio locally and, optionally, sends them over HTTPS through a reverse proxy to the aggregator, which stores, transcribes and publishes alerts](docs/architecture/rds-guard.architecture.svg)

Every installation is a complete RDS Guard on its own (**standalone**). With
`UPLINK_URL` set it also becomes a **satellite** and sends a copy of each event
and its audio to an **aggregator**, a separate container that collects events
from many satellites, transcribes them centrally and publishes MQTT alerts. Only
the aggregator's ingest API is on the internet, behind an HTTPS reverse proxy or
a Cloudflare Tunnel.
See [Run as a satellite](#run-as-a-satellite) and
[aggregator/README.md](aggregator/README.md).

An [interactive version](docs/architecture/rds-guard.architecture.html) of the
diagram (download and open in a browser) links every component to the code it
describes; see [docs/architecture](docs/architecture/README.md).

### Inside one installation

Single Docker container. One process handles the entire pipeline:

```
┌──────────────────────────────────────────────────────────────┐
│  rds-guard container                                         │
│                                                              │
│  Single-station (FM_FREQUENCY):                              │
│  rtl_fm ──→ AudioTee ──→ redsea ──→ rds_guard.py            │
│                │                          │                  │
│  Multi-station (FM_FREQUENCIES):          │                  │
│  rtl_sdr ──→ Channelizer ─┬─ pipe ──→ redsea[0] ──→        │
│                            ├─ pipe ──→ redsea[1] ──→ rds_guard.py
│                            └─ ...                     │      │
│                                                  ┌────┴────┐ │
│          AudioRecorder                           │  Rules  │ │
│            (ffmpeg)                              │  Engine │ │
│                │                                 └──┬───┬──┘ │
│                ▼                            ┌───────┘   └──┐ │
│          Transcriber                        ▼              ▼ │
│     (ElevenLabs Scribe,                 SQLite           MQTT │
│      fallback KB-Whisper)          /data/events.db  (optional)│
│                                         │                    │
│          /data/audio/                   ▼                    │
│           *.ogg / *.wav     Web server (aiohttp)             │
│                               ├── GET  /              → UI   │
│                               ├── GET  /api/events    → API  │
│                               ├── GET  /api/audio/:f  → play │
│                               ├── GET  /api/status    → info │
│                               └── WS   /ws/console    → live │
│                                   │                          │
│  Uplink (optional, UPLINK_URL) ──→ HTTPS ──→ aggregator      │
│                                   │                          │
├───────────────────────────────────┼──────────────────────────┤
│                               port 8022                      │
└───────────────────────────────────┼──────────────────────────┘
                                    ▼
                                 Browser
```

**Single-station pipeline:** `rtl_fm` demodulates the FM signal, `AudioTee` splits the PCM stream — forwarding to `redsea` for RDS decoding while simultaneously feeding the `AudioRecorder` during active events.

**Multi-station pipeline:** `rtl_sdr` captures raw wideband IQ, `Channelizer` (numpy DSP) extracts and demodulates each station to its own 171 kHz PCM pipe, and N independent `redsea` + `AudioRecorder` instances run in parallel. `rds_guard.py` processes the decoded JSON, triggers recordings, and dispatches transcriptions.

**Rules engine** evaluates each decoded group against hardcoded rules defined by the RDS standard:

| Trigger | Event type | Severity |
|---------|------------|----------|
| TA flag goes true | `traffic` | `warning` |
| TA flag goes false (end) | `traffic` | `info` |
| RadioText change during active TA | updates existing event | |
| PTY changes to Alarm | `emergency` | `critical` |
| EON linked station TA (group 14A) | `eon_traffic` | `info` |

Events are written to SQLite and optionally published to MQTT. Traffic announcements and emergency broadcasts are tracked through their full lifecycle (start, RadioText updates, end with duration). Audio is automatically recorded during `traffic` and `emergency` events, then transcribed (ElevenLabs Scribe, or local KB-Whisper as fallback).

## Requirements

- Linux host (Raspberry Pi, NAS, server)
- RTL-SDR USB dongle (RTL2832U-based)
- Docker and Docker Compose
- FM antenna with line of sight to a P4 transmitter

### Host preparation

Blacklist the default DVB kernel driver so the RTL-SDR is available to the container:

```bash
echo "blacklist dvb_usb_rtl28xxu" | sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf
sudo modprobe -r dvb_usb_rtl28xxu
```

## Quick start

1. Get the two files you need. There is no need to clone the repository:
   `docker-compose.yml` uses the ready-made image
   `ghcr.io/tubalainen/rds-guard:latest` (amd64 + arm64), which Docker pulls
   for you.

```bash
mkdir rds-guard && cd rds-guard
curl -fsSLo docker-compose.yml https://raw.githubusercontent.com/tubalainen/rds-guard/master/docker-compose.yml
curl -fsSLo .env https://raw.githubusercontent.com/tubalainen/rds-guard/master/.env.example
```

2. Edit `.env` and at minimum set your local P4 frequency:

```bash
FM_FREQUENCY=103.3M        # Your local P4 frequency
```

For the best transcriptions, add an [ElevenLabs](https://elevenlabs.io) API
key (about 2–3 öre per traffic announcement). Without one, audio is
transcribed locally with the bundled Swedish KB-Whisper model:

```bash
ELEVENLABS_API_KEY=your-key
```

If you want MQTT forwarding, enable it and configure the broker:

```bash
MQTT_ENABLED=true              # Enable MQTT (default: false)
MQTT_HOST=192.168.1.100        # Your MQTT broker IP
MQTT_PORT=1883
```

3. Pull the image and start it:

```bash
docker compose pull
docker compose up -d
```

To update later, run `docker compose pull && docker compose up -d` in the
same folder. Your `.env` and `rds-data` are kept.

4. Open the web UI at **http://your-host:8022**

5. View logs:

```bash
docker compose logs -f rds-guard
```

> **Building locally:** Only if you want to build the image yourself instead of pulling it, you need the whole repository: `git clone https://github.com/tubalainen/rds-guard.git`, then in `rds-guard` copy `.env.example` to `.env`, edit `docker-compose.yml` (comment out the `image:` line and uncomment `build: .`) and run `docker compose up -d --build`.

6. **Optional:** connect the installation to an aggregator. See
   [Run as a satellite](#run-as-a-satellite).

That's a complete **standalone** installation: everything stays on this host.

## Run as a satellite

A satellite is an ordinary RDS Guard installation that also reports to a
central **aggregator**, which collects traffic announcements and emergency
broadcasts from many places. Nothing changes locally: you keep your own events,
audio, transcriptions, web UI and MQTT. The uplink only adds a copy that is
sent to the aggregator.

```
 Your RDS Guard (satellite)                         Aggregator (someone else's server)
 RTL-SDR → decode → events, audio, UI, MQTT
                │
                └── uplink.py ── outbound HTTPS ──→  https://rds.example.com
                    (queued locally while offline)
```

### Before you start

1. Get the standalone installation working first ([Quick start](#quick-start)):
   the *Debug* page of the web UI (link in the footer) shows your station and a decode rate above 5 grp/s.
2. Ask the aggregator's owner for three values: the aggregator address, a
   node token, and the name to use for your node. They create the token in the
   aggregator's web UI (*Noder → Lägg till nod*) and get the lines to send you.

No ports need to be opened on your router: the satellite only makes outbound
HTTPS requests.

### Connect

1. Add the lines you received to `.env`:

```bash
UPLINK_URL=https://rds.example.com    # Aggregator address
UPLINK_TOKEN=paste-your-token-here    # Node token (keep it secret)
NODE_NAME=Kalle, Karlstad             # Shown on the aggregator
```

2. Restart the container so it reads the new settings:

```bash
docker compose up -d
```

### Check that it works

Within a minute:

- The footer of the web UI shows **Uplink ansluten**.
- The log shows `Mode: satellite` at start-up and then `Uplink connected`:

```bash
docker compose logs rds-guard | grep -i -E "mode|uplink"
```

- The status API reports the uplink state and how many items are queued:

```bash
curl -s http://localhost:8022/api/status | python3 -m json.tool | grep -A6 '"uplink"'
```

  `"state": "connected"` with `"queue_depth": 0` means everything has been delivered.

- On the aggregator, your node turns green under **Nodes**.

| Status bar | Meaning | What to do |
|---|---|---|
| `uplink` (green) | Connected | Nothing |
| `uplink offline` | Aggregator unreachable; data is queued | Check your internet connection and `UPLINK_URL`; hover over the status for the last error |
| `uplink: bad token` | Token wrong, rotated or node disabled | Ask the aggregator's owner for a new token |
| `uplink: update needed` | Your RDS Guard version is not supported by the aggregator | Update: `docker compose pull && docker compose up -d` |
| *(no uplink shown)* | `UPLINK_URL` is empty | Standalone mode; see [Connect](#connect) |

### What is sent

- Each traffic announcement and emergency broadcast: station (PI, PS),
  frequency, programme type, RadioText, start and end time.
- The recorded audio of each event (OGG/Opus, about 0.7 MB per 2 minutes).
- A status report every 30 seconds: station, decode rate, pipeline health,
  software version and queue length.

Transcriptions are **not** sent; the aggregator transcribes the audio itself.
Nothing else leaves your network: no MQTT data, no web UI, no raw RDS stream.

If the aggregator or your internet connection is down, everything is queued
locally and sent once the connection is back, for up to
`UPLINK_MAX_AGE_HOURS` (72 hours by default). The protocol is described in
[docs/uplink-protocol.md](docs/uplink-protocol.md).

### Disconnect

Remove or comment out `UPLINK_URL` in `.env` and run `docker compose up -d`.
The installation is standalone again; local data is not affected.

### Run the aggregator yourself

To collect events from friends' satellites, run the separate aggregator
container: see [aggregator/README.md](aggregator/README.md). It is a single
container that you put behind your own HTTPS reverse proxy (for example
Nginx Proxy Manager with Cloudflare) or a Cloudflare Tunnel; its README has
the settings for both and a way to
[check that it works](aggregator/README.md#check-that-it-works).

## Configuration

All settings are in the `.env` file:

### RTL-SDR

| Variable | Default | Description |
|----------|---------|-------------|
| `FM_FREQUENCY` | `103.5M` | FM frequency to tune (single-station mode) |
| `FM_FREQUENCIES` | | Comma-separated 2–4 frequencies for multi-station mode, e.g. `103.5M,102.9M` (see [Multi-station monitoring](#multi-station-monitoring)) |
| `RTL_GAIN` | `8` | Tuner gain in dB (0-50) |
| `PPM_CORRECTION` | `0` | Frequency correction for your dongle |
| `RTL_DEVICE_INDEX` | `0` | Device index if multiple dongles |
| `RTL_DEVICE_SERIAL` | | Select dongle by serial number instead of index |

### MQTT (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `MQTT_ENABLED` | `false` | Enable MQTT publishing (`true`/`false`) |
| `MQTT_HOST` | | Broker hostname/IP |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_USER` | | Username (optional) |
| `MQTT_PASSWORD` | | Password (optional) |
| `MQTT_TOPIC_PREFIX` | `rds` | Base topic prefix |
| `MQTT_CLIENT_ID` | `rds-guard` | MQTT client ID |
| `MQTT_QOS` | `1` | Default QoS level |
| `MQTT_RETAIN_STATE` | `true` | Retain state topics |

### Publishing control

| Variable | Default | Description |
|----------|---------|-------------|
| `PUBLISH_MODE` | `essential` | `essential` = alert topic carries only traffic announcements and emergencies. `all` = every decoded RDS field + EON on alert topic. |
| `PUBLISH_RAW` | `false` | Publish raw RDS groups to `system/raw` (high volume) |
| `STATUS_INTERVAL` | `30` | Seconds between status messages |

### Web UI

| Variable | Default | Description |
|----------|---------|-------------|
| `WEB_UI_PORT` | `8022` | HTTP port for the web UI and API |
| `EVENT_RETENTION_DAYS` | `30` | Auto-delete events older than this |
| `WEB_UI_URL` | | Address the web UI is reached on, e.g. `https://rds.example.se`. When set, push messages link to the event and its audio |
| `NOTIFY_TIMEZONE` | `Europe/Stockholm` | Time zone for the days and times in push rules |
| `ADMIN_PASSWORD` | *(empty)* | Password for the **Admin** page (push rules). Empty switches Admin off and the rules cannot be changed |

### Audio recording

Audio is always recorded during traffic and emergency events. Recordings are stored as OGG (Opus) by default.

| Variable | Default | Description |
|----------|---------|-------------|
| `AUDIO_DIR` | `/data/audio` | Directory for recorded audio files |
| `RECORD_EVENT_TYPES` | `traffic,emergency` | Comma-separated event types to record |
| `AUDIO_FORMAT` | `ogg` | Output format: `ogg` (Opus) or `wav` |
| `MAX_RECORDING_SEC` | `600` | Maximum recording duration (safety cutoff) |

### Transcription

Recorded audio is transcribed for searchable text and MQTT alerts. By default ElevenLabs Scribe does the work and the local KB-Whisper model takes over when the API fails; without `ELEVENLABS_API_KEY` everything is transcribed locally.

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSCRIPTION_ENGINE` | `elevenlabs` | `elevenlabs` = ElevenLabs Scribe API with local fallback (runs as `local` without an API key), `local` = built-in faster-whisper, `remote` = external ASR server, `none` = disable |
| `TRANSCRIPTION_LANGUAGE` | `sv` | Language code for Whisper (e.g. `sv`, `en`, `de`) |
| `TRANSCRIPTION_MODEL` | *(auto)* | Empty = the bundled KB-Whisper small *strict* model for Swedish, `small` for other languages. Any size (`tiny` … `large-v3`, `large-v3-turbo`) or a Hugging Face CTranslate2 repo such as `KBLab/kb-whisper-medium` (local only) |
| `TRANSCRIPTION_DEVICE` | `cpu` | Compute device: `cpu` or `cuda` (local only) |
| `TRANSCRIPTION_PROMPT` | | Optional vocabulary hint passed to Whisper, e.g. `Trafikverket, E4, E18, Essingeleden, olycka, kö, körfält` |
| `TRANSCRIPTION_BEAM_SIZE` | `5` | Beam search width; `1` is roughly twice as fast on CPU at a small accuracy cost (local only) |
| `TRANSCRIPTION_CPU_THREADS` | `0` | CPU threads for inference, `0` = automatic (local only) |
| `WHISPER_REMOTE_URL` | | URL of remote Whisper ASR server, e.g. `http://whisper:9000` (remote only) |
| `WHISPER_REMOTE_TIMEOUT` | `120` | HTTP timeout in seconds for remote ASR requests |
| `ELEVENLABS_API_KEY` | | ElevenLabs API key (elevenlabs only) |
| `ELEVENLABS_MODEL` | `scribe_v3` | `scribe_v3` (writes numbers as digits) or `scribe_v2` |
| `ELEVENLABS_KEYTERMS` | | Comma-separated words to listen for, e.g. `E18, Bergviksmotet, Våxnäsmotet` (max 50 characters / 5 words each; costs 20 % extra) |
| `ELEVENLABS_TIMEOUT` | `60` | HTTP timeout in seconds |
| `TRANSCRIPTION_FALLBACK` | `local` | When the ElevenLabs call fails: `local` = transcribe with faster-whisper instead, `none` = mark as failed |

> **Swedish model:** for `TRANSCRIPTION_LANGUAGE=sv` the default is [KB-Whisper](https://huggingface.co/KBLab/kb-whisper-small) small from the National Library of Sweden, in its verbatim *strict* style, which the Docker image converts and bundles at build time (`/opt/models`) because KBLab only publishes the default style for faster-whisper. KB-Whisper is trained on 50 000+ hours of Swedish. `kb-whisper-small` has about 6–7 % word error rate on Swedish benchmarks versus 20–26 % for OpenAI's `small` (and beats OpenAI's `large-v3`) at the same CPU cost. On a stronger host, `KBLab/kb-whisper-medium` or `KBLab/kb-whisper-large` is more accurate still. Other models are downloaded once and cached under `/data/models`.

> **ElevenLabs:** on P4 traffic clips Scribe was more complete than local KB-Whisper (fewer dropped place names and details, phone and road numbers right) and took 2–5 s per clip. Audio is sent to ElevenLabs (US processing by default). With the default fallback, a failed call is transcribed locally, so alerts still get text.

**Check that ElevenLabs works:** at startup the container checks the key against ElevenLabs (an account lookup, no credits used) and logs the result. `docker compose logs rds-guard | grep -i elevenlabs` shows one of:

```
ElevenLabs: connected and authenticated (plan starter, 20 264 of 90 000 credits used) — transcribing with scribe_v3, fallback local
ElevenLabs: authentication failed (HTTP 401: invalid_api_key) — check ELEVENLABS_API_KEY; clips will be transcribed locally (…)
ElevenLabs: could not reach api.elevenlabs.io (…) — clips will be transcribed locally (…) until it answers
ElevenLabs: no ELEVENLABS_API_KEY set — transcribing locally instead
```

Each transcription is logged with the engine that served it, e.g. `Transcription complete for event 42 via ElevenLabs scribe_v3 (333 chars, 3.1s)` or `… via local … (fallback)`. A warning appears when 90 % of the period's credits are used.

> **Local vs Remote:** Local transcription works out of the box but uses significant CPU/RAM (especially larger models). For Raspberry Pi or low-power hosts, use `TRANSCRIPTION_ENGINE=remote` with a [whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice) container running on a more powerful machine, or set `TRANSCRIPTION_ENGINE=none` to disable transcription while still recording audio.

### Uplink to an aggregator (optional)

| Variable | Default | Description |
|----------|---------|-------------|
| `UPLINK_URL` | | Aggregator address. Empty = standalone (see [Run as a satellite](#run-as-a-satellite)) |
| `UPLINK_TOKEN` | | Node token from the aggregator's owner |
| `NODE_NAME` | | Your name or place, shown on the aggregator |
| `UPLINK_HEARTBEAT_SEC` | `30` | Seconds between status reports |
| `UPLINK_MAX_AGE_HOURS` | `72` | Unsent items older than this are dropped |

## Web UI

The web UI is in Swedish and works on phones and desktops. It is built
around the history of what the receiver has heard:

- **Arkiv** — a calendar over the stored days (colour = number of
  messages, red ring = VMA) and the messages of the chosen day, each with
  audio, transcription and whether a push was sent. Figures at the top sum
  up the period: number of traffic messages, VMA, the most common time of
  day and pushes sent.
- **Sök** — search in RadioText and transcriptions, filtered by type,
  period and station. A search can be turned into a push rule with one
  click (*Bevaka*).
- **Admin** — behind the password in `ADMIN_PASSWORD`. Its first section is
  **Aviseringar**: the push rules (see [Push notifications](#push-notifications)),
  the MQTT connection and the latest messages sent. A login lasts 12 hours,
  or until the container restarts or the password changes. Without
  `ADMIN_PASSWORD` the Admin page is switched off; rules already saved keep
  sending.
- **Händelse** — one event: audio, full transcription, RadioText, pushes
  and details. Push messages link here when `WEB_UI_URL` is set.

The audio player is [wavesurfer.js](https://wavesurfer.xyz/) 7 (BSD-3,
bundled in `static/shared/vendor/`, no CDN): a waveform you can click in,
playback speed and download.

**Debug** (link in the footer) shows the receiver status (PI, PTY, TP/TA,
RadioText, decode rate, pipeline, uplink) and the live stream of all
decoded RDS groups over WebSocket, with pause and filters.

## Push notifications

Every finished event is checked against the rules you set under
**Admin → Aviseringar**. For each rule that matches, a ready-to-show message is
published to `rds/notify/<topic of the rule>`, which Home Assistant,
Node-RED or similar can send on to a phone.

A rule reads like a sentence: *when* a traffic message and/or VMA is sent
on *these stations*, *and* the text mentions *any of these words*, *on
these days* and *between these times*, *send* to this topic with this
priority. Words are searched in RadioText and the transcription. Every
change is tried against the stored history right away ("the rule would
have sent 31 pushes in the last 30 days"), and *Skicka test* sends a
made-up message.

- Traffic messages are checked when the broadcast has ended and the
  transcription is ready (or has timed out), so keywords also match what
  was said.
- VMA is pushed as soon as the broadcast starts.
- A fresh install starts with two rules: **VMA** (`rds/notify/vma`,
  critical) and **Trafik** (`rds/notify/trafik`, all traffic messages).
- Rules are stored in the database (`/data/events.db`); the broker itself
  is still set in `.env`. Without MQTT nothing is sent, but rules can still
  be made and tried.

The message:

```json
{
  "title": "Trafik: Kö Essingeleden norrut",
  "message": "Två bilar har kört ihop vid Stora Essingen. Ett körfält är avstängt.",
  "priority": "normal",
  "event_type": "traffic",
  "rule": {"id": 3, "name": "Pendling"},
  "matched_keywords": ["Essingeleden"],
  "station": {"pi": "0xE203", "ps": "P4 Sthlm"},
  "frequency": "103.3M",
  "started": "2026-11-24T16:12:04",
  "duration_sec": 64,
  "event_id": 1234,
  "url": "https://rds.example.se/#/event/1234",
  "audio_url": "https://rds.example.se/api/audio/1234.ogg",
  "test": false,
  "timestamp": "2026-11-24T16:13:20Z"
}
```

`url` and `audio_url` are `null` without `WEB_UI_URL`. Home Assistant
example:

```yaml
automation:
  - alias: "RDS Guard push"
    trigger:
      - platform: mqtt
        topic: "rds/notify/#"
    action:
      - service: notify.mobile_app_your_phone
        data:
          title: "{{ trigger.payload_json.title }}"
          message: "{{ trigger.payload_json.message }}"
          data:
            url: "{{ trigger.payload_json.url }}"
```

## REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/events` | GET | Query events. Params: `type`, `since`, `q` (words in RadioText/transcription), `from`/`to` (UTC, on the start time), `pi`, `limit`, `offset` |
| `/api/events/{id}` | GET | One event |
| `/api/events/active` | GET | In-progress announcements only |
| `/api/calendar` | GET | Light rows for the whole retention period (calendar and figures) |
| `/api/status` | GET | Decoder status (uptime, station info, decode rate) |
| `/api/audio/{filename}` | GET | Stream recorded audio file (OGG/WAV) |
| `/api/events` | DELETE | Clear all events |
| `/api/admin/session` | GET | Whether Admin is on and you are logged in |
| `/api/admin/login` / `/api/admin/logout` | POST | Log in (`{"password": "…"}`) or out; sets a session cookie |
| `/api/notify` | GET | Push rules and the MQTT connection (needs Admin login, as do all `/api/notify*`) |
| `/api/notify/rules` | POST | Create a rule |
| `/api/notify/rules/{id}` | PUT / DELETE | Change or remove a rule |
| `/api/notify/preview` | POST | Try a rule (unsaved) on the stored history |
| `/api/notify/test` | POST | Send a test message for a rule |
| `/api/notify/log` | GET | Latest push messages |
| `/ws/console` | WS | Live stream of all decoded RDS messages |

Event responses include `audio_url`, `transcription` and `notify` (the push
messages sent for the event). Writes to `/api/notify` and `/api/admin` must
be JSON and, from a browser, same-origin.

## MQTT topics

When MQTT is enabled, decoded data is published to structured topics:

```
rds/{pi}/traffic/ta                # Traffic Announcement active (bool)
rds/{pi}/traffic/tp                # Traffic Programme flag
rds/{pi}/programme/rt              # RadioText (64-char free text)
rds/{pi}/station/pty               # Programme Type
rds/{pi}/eon/{other_pi}/ta         # Linked station TA via EON
rds/{pi}/{type}/transcription      # Transcription text (retained)
rds/alert                          # Traffic & emergency events (see below)
rds/notify/{topic}                 # Push messages from your rules (see Push notifications)
rds/system/status                  # Bridge health (periodic)
```

### Alert topic (`rds/alert`)

A single MQTT message is published to `rds/alert` when an event is fully complete. The alert is held until audio transcription finishes (up to 2 minutes), so subscribers receive the transcribed text in the same message. If transcription fails or times out, the alert is sent without it.

Each alert message includes:

| Field | Description |
|-------|-------------|
| `event_type` | `traffic_announcement`, `emergency_broadcast`, or `eon_traffic` (all mode only) |
| `state` | `end` |
| `transcribed_text` | Speech-to-text of the broadcast audio, or `null` if transcription failed/timed out |
| `transcription_status` | `done`, `error`, `timeout`, or `none` (no audio) |
| `station` | Station context (PI code, PS name, etc.) |
| `duration_sec` | Event duration in seconds |
| `radiotext` | RadioText messages collected during the event |
| `audio_available` | Whether recorded audio is available for playback |
| `timestamp` | ISO 8601 timestamp |

**Publish mode behavior:**

- **`essential`** (default) — Only traffic announcements and emergency broadcasts are published to `rds/alert`. EON (Enhanced Other Networks) events and other decoded RDS data are excluded.
- **`all`** — Everything published to `rds/alert`, including EON linked station traffic events (`event_type: "eon_traffic"`). Additional per-field topics are also published for PS, AF, clock, RT+, Long PS, ODA, BLER, and more.

The `rds/{pi}/{type}/transcription` topic is retained so new subscribers receive the latest transcription immediately.

## Home Assistant

The MQTT output works directly with Home Assistant. Example configuration:

```yaml
mqtt:
  binary_sensor:
    - name: "P4 Traffic Announcement"
      state_topic: "rds/0x9E04/traffic/ta"
      value_template: "{{ value_json.active }}"
      payload_on: "true"
      payload_off: "false"
      device_class: problem

  sensor:
    - name: "P4 RadioText"
      state_topic: "rds/0x9E04/programme/rt"
      value_template: "{{ value_json.radiotext }}"
      icon: mdi:message-text
```

Example automation for traffic announcement notifications:

```yaml
automation:
  - alias: "Traffic Announcement on P4"
    trigger:
      - platform: mqtt
        topic: "rds/+/traffic/ta"
    condition:
      - condition: template
        value_template: "{{ trigger.payload_json.active == true }}"
    action:
      - service: notify.mobile_app
        data:
          title: "Traffic announcement on P4"
          message: "A traffic announcement is being broadcast"
```

Send a notification with transcription when a traffic announcement ends:

```yaml
automation:
  - alias: "Traffic Alert with Transcription"
    trigger:
      - platform: mqtt
        topic: "rds/alert"
    condition:
      - condition: template
        value_template: "{{ trigger.payload_json.event_type == 'traffic_announcement' }}"
    action:
      - service: notify.mobile_app
        data:
          title: "Traffic announcement on P4"
          message: >
            {% if trigger.payload_json.transcribed_text %}
              {{ trigger.payload_json.transcribed_text }}
            {% else %}
              Traffic announcement ({{ trigger.payload_json.duration_sec }}s, no transcription)
            {% endif %}
```

## P4 regional frequencies

P4 is Sweden's primary traffic and emergency broadcast network with 25 regional stations. Set `FM_FREQUENCY` to your local station:

| Station | City | Frequency |
|---------|------|-----------|
| P4 Stockholm | Stockholm | 103.3 MHz |
| P4 Goteborg | Goteborg | 101.9 MHz |
| P4 Malmohus | Malmo | 102.0 MHz |
| P4 Uppland | Uppsala | 107.3 MHz |
| P4 Vastmanland | Vasteras | 100.5 MHz |
| P4 Orebro | Orebro | 102.8 MHz |
| P4 Ostergotland | Norrkoping | 94.8 MHz |
| P4 Sormland | Eskilstuna | 98.3 MHz |
| P4 Jonkoping | Jonkoping | 100.8 MHz |
| P4 Halland | Halmstad | 97.3 MHz |
| P4 Norrbotten | Lulea | 96.9 MHz |
| P4 Vasterbotten | Umea | 103.6 MHz |
| P4 Jamtland | Ostersund | 100.4 MHz |
| P4 Vasternorrland | Sundsvall | 102.8 MHz |
| P4 Gavleborg | Gavle | 102.0 MHz |
| P4 Dalarna | Falun | 100.2 MHz |
| P4 Varmland | Karlstad | 103.5 MHz |
| P4 Vast | Uddevalla | 103.3 MHz |
| P4 Sjuharad | Boras | 102.9 MHz |
| P4 Skaraborg | Skovde | 100.3 MHz |
| P4 Kronoberg | Vaxjo | 100.2 MHz |
| P4 Kalmar | Kalmar | 95.6 MHz |
| P4 Blekinge | Ronneby | 87.8 MHz |
| P4 Kristianstad | Kristianstad | 101.4 MHz |
| P4 Gotland | Visby | 102.8 MHz |

Frequencies may vary by relay transmitter. The decoded AF (Alternative Frequencies) data reveals all available frequencies for your station.

## File structure

```
rds-guard/
├── Dockerfile
├── docker-compose.yml
├── .env
├── config.py
├── requirements.txt
├── entrypoint.sh
├── rds_guard.py          # Supervisor: rules engine, MQTT, WebSocket hub
├── pipeline.py           # Subprocess manager for rtl_fm + redsea (+ multi-station path)
├── channelizer.py        # Wideband IQ → N×PCM (numpy DSP, multi-station only)
├── audio_tee.py          # PCM stream splitter (rtl_fm → redsea + recorder)
├── audio_recorder.py     # Recording lifecycle, ffmpeg conversion
├── transcriber.py        # Speech-to-text: ElevenLabs Scribe, local faster-whisper or remote ASR
├── tools/convert_kb_whisper.py # Build step: converts KB-Whisper strict for faster-whisper
├── status.py             # Status snapshot (/api/status + uplink heartbeat)
├── uplink.py             # Satellite → aggregator sender (outbox + heartbeat)
├── docs/
│   ├── architecture/     # Archify architecture diagram (SVG, HTML, JSON source)
│   └── uplink-protocol.md # Satellite ↔ aggregator protocol
├── aggregator/           # Aggregator service (separate container, see its README)
├── tests/                # pytest suite (pip install -r requirements-dev.txt; pytest)
├── event_store.py        # SQLite wrapper
├── web_server.py         # aiohttp REST API + WebSocket + static serving
├── notify_rules.py       # Push rules: validation, matching, message (shared with aggregator)
├── notifier.py           # Push dispatch on the satellite
└── static/
    ├── index.html
    ├── shared/           # Web UI shared with the aggregator
    │   ├── app.css
    │   ├── rdsui.js      # Arkiv, Sök, Händelse, Admin (Aviseringar), audio player
    │   └── vendor/       # wavesurfer.js
    ├── css/
    │   └── debug.css     # Debug view
    └── js/
        ├── satellite.js  # Status, Debug view
        └── console.js    # WebSocket console, pause/filter
```

## Operations

### Update and restart

Pull the latest image and restart (preserves event history):

```bash
docker compose down && docker compose pull && docker compose up -d && docker compose logs -f
```

Clean restart from scratch (deletes the event database, audio and all other
data in `./rds-data`). The data is in a bind-mounted folder, so
`docker compose down -v` does not remove it; delete the folder instead. The
container writes its files as root, so this needs `sudo`:

```bash
docker compose down && sudo rm -rf ./rds-data && docker compose pull && docker compose up -d && docker compose logs -f
```

If building locally (a clone of the repository, with `build: .` in
docker-compose.yml), run `git pull` first, then:

```bash
docker compose down && docker compose build --no-cache && docker compose up -d && docker compose logs -f
```

### Upgrading from v0.1.3 to v0.2.x

v0.2.x is a drop-in upgrade for a standalone install. Satellite mode is
optional and **off by default**: if you don't set `UPLINK_URL`, RDS Guard runs
exactly as before, and nothing is sent anywhere. You don't need to change
`.env` or `docker-compose.yml`, and your existing events, audio and
transcriptions are kept.

1. **Back up your data (recommended):**
   ```bash
   docker compose down
   cp -a rds-data rds-data.backup-0.1.3
   ```
2. **Pull the new image and start again:**
   ```bash
   docker compose pull
   docker compose up -d
   ```
   If you build locally (`build: .`), run `git pull` and then
   `docker compose up -d --build` instead.
3. **Check the log** with `docker compose logs -f rds-guard`. You should see
   `Mode: standalone (no uplink)`. The web UI footer shows the new version.

On first start the database in `rds-data/events.db` is migrated
automatically. A new `event_uid` column and an empty `outbox` table are added,
and existing rows are not changed. The outbox is only used in satellite mode.

**Pinning a version:** `ghcr.io/tubalainen/rds-guard:latest` follows
`master`. To stay on a release, set the image to
`ghcr.io/tubalainen/rds-guard:0.4.2` (or `:0.4` to get 0.4.x patch releases).

**Rolling back:** set the image to `ghcr.io/tubalainen/rds-guard:0.1.3`, and
restore `rds-data.backup-0.1.3` if you want the database exactly as it was.
v0.1.3 ignores the added column and table, so it also runs on the migrated
database.

**Joining an aggregator** is a separate, optional step after the upgrade. See
[Run as a satellite](#run-as-a-satellite). Only events recorded after the
uplink is turned on are sent. Older events and transcriptions are not.

### Transcription changes in v0.2.2

- **ElevenLabs is the default engine.** Add `ELEVENLABS_API_KEY` to `.env` to
  use it. Without a key nothing changes in how it works: audio is transcribed
  locally. To keep local only, set `TRANSCRIPTION_ENGINE=local`.
- **New local model.** With `TRANSCRIPTION_LANGUAGE=sv` and
  `TRANSCRIPTION_MODEL` empty, the bundled KB-Whisper small *strict* model is
  used (it is in the image, nothing to download). If your `.env` still has
  `TRANSCRIPTION_MODEL=small`, you stay on OpenAI's small model: clear the
  line to switch.
- New recordings have the FM pre-emphasis removed, so they sound less sharp.
- Other models are now cached in `rds-data/models`; a model you set yourself
  is downloaded once more after the upgrade.

### Releasing

Change `RELEASE_VERSION` to the new version (e.g. `v0.3.0`) in a pull request
and merge it, or run Actions → **Release** → *Run workflow* with a version.
Either way the `Release` workflow tags `master`, publishes a GitHub release
with generated release notes, and builds both images for that version.

To add hand-written notes, such as upgrade steps, put them in
`docs/release-notes/<version>.md` (e.g. `docs/release-notes/v0.3.0.md`) in the
same pull request. They appear above the generated notes. A notes file added
later for a release that already exists is put at the top of that release's
text once, when it reaches `master`.

### View logs

```bash
docker compose logs -f rds-guard
```

### Check decoder status

The status API returns pipeline health, station info, decode rate, and uptime:

```bash
curl http://localhost:8022/api/status
```

### Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Header says *Inte startad* or *Fel i mottagaren* | Pipeline hasn't started or RTL-SDR not found | Check logs for `[rtl_fm]` errors. Verify dongle is plugged in and driver is blacklisted |
| `usb_open error` in logs | Kernel DVB driver still loaded | Run `sudo modprobe -r dvb_usb_rtl28xxu` on the host |
| `No RTL-SDR devices found` | Dongle not passed to container | Verify `privileged: true` in docker-compose.yml or add the device explicitly |
| Pipeline status shows `error` | rtl_fm or redsea crashed | Check logs for `[rtl_fm]` / `[redsea]` error lines. Rebuild with `--no-cache` |
| Events show as "In progress" after restart | Normal — stale events from the previous run | They are automatically closed on startup. Do a clean rebuild with `-v` to clear old data |
| MQTT not publishing | MQTT disabled or broker unreachable | Set `MQTT_ENABLED=true` in `.env` and verify broker IP/port. Check logs for MQTT connection errors |
| Low decode rate (< 5 grp/s) | Weak signal or wrong frequency | Try increasing `RTL_GAIN`. Verify `FM_FREQUENCY` matches a nearby P4 transmitter |
| Container keeps restarting; log shows `UPLINK_URL is set but UPLINK_TOKEN is empty` | Satellite settings incomplete | Add `UPLINK_TOKEN` from the aggregator's owner, or remove `UPLINK_URL` to run standalone |
| Status bar shows `uplink offline` | Aggregator unreachable, or a proxy/Cloudflare answered instead of it | Hover over the uplink status, or check `docker compose logs rds-guard \| grep -i uplink`. Data stays queued and is sent later |
| Status bar shows `uplink: bad token` | Token wrong, rotated or node disabled | Ask the aggregator's owner for a new token and update `UPLINK_TOKEN` |
| `queue_depth` in `/api/status` keeps growing | Uplink cannot deliver | See the two rows above; items older than `UPLINK_MAX_AGE_HOURS` are dropped |

## Multi-station monitoring

A single RTL-SDR dongle covers up to ~2.4 MHz of instantaneous bandwidth, which is enough to capture 4 FM stations at once (each channel is 200 kHz wide, and 4 channels spaced within 2 MHz fit comfortably).

Set `FM_FREQUENCIES` to a comma-separated list of 2–4 stations to enable simultaneous monitoring:

```bash
FM_FREQUENCIES=103.5M,102.9M,101.3M
```

### How it works

When `FM_FREQUENCIES` is set with 2 or more values, the pipeline switches to a wideband capture mode:

1. `rtl_sdr` captures raw IQ at 2 394 000 samples/sec centred between all target frequencies
2. A Python channelizer thread (numpy DSP) extracts each station, demodulates FM, and writes 171 kHz PCM to a dedicated pipe per station
3. One `redsea` process and one audio recorder run independently per station

Station names are decoded live from the RDS Programme Service (PS) field — no configuration needed.

### Constraints

- **Maximum 4 stations** — hardware and CPU limit
- **All frequencies must be within 2.0 MHz of each other** — this is the usable bandwidth at the chosen sample rate. Startup will abort with a clear error if this constraint is violated.
- Adequate on any x86_64 host and Raspberry Pi 4. Not tested on Raspberry Pi 3.

### API changes in multi-station mode

`GET /api/status` returns a `stations[]` array instead of a single `station` object:

```json
{
  "pipeline": { "state": "running", ... },
  "stations": [
    { "frequency": "103.5M", "pi": "C404", "ps": "P4 Värmland", "groups_per_sec": 11.3 },
    { "frequency": "102.9M", "pi": "C502", "ps": "P4 Sjuharad", "groups_per_sec": 11.1 }
  ]
}
```

Events in the database and web UI are always tagged with `frequency` and `station_ps`, so the archive works identically regardless of mode.

### Backward compatibility

- `FM_FREQUENCIES` unset → single-station mode using `FM_FREQUENCY` (unchanged)
- `FM_FREQUENCIES` with exactly 1 value → also single-station mode
- `FM_FREQUENCIES` with 2–4 values → wideband multi-station mode

## Multiple RTL-SDR dongles

If you have more than one RTL-SDR plugged in, you can select by serial number:

```bash
RTL_DEVICE_SERIAL=00000002
```

The pipeline manager resolves the serial to a device index automatically using `rtl_test`.

## License

MIT
