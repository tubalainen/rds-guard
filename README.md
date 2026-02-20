# RDS Guard

FM radio traffic and emergency monitoring for Sweden. Decodes RDS data from an RTL-SDR dongle, captures traffic announcements and emergency broadcasts, and serves a live web dashboard.

Built for Sveriges Radio P4, Sweden's primary traffic announcement carrier and emergency broadcast channel (VMA).

## What it does

- Tunes to an FM frequency using an RTL-SDR USB dongle
- Decodes all RDS (Radio Data System) data in real time
- Detects traffic announcements, emergency broadcasts, and EON cross-network alerts
- Records broadcast audio during traffic and emergency events, converts to OGG/WAV via ffmpeg
- Transcribes recorded audio using Whisper (local or remote) for searchable text
- Stores events in a local SQLite database with 30-day retention
- Serves a dark-themed web dashboard with live event feed, audio playback, and transcriptions
- Optionally forwards events and decoded data to an MQTT broker for Home Assistant or other automation

## Architecture

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
│       (faster-whisper                   SQLite           MQTT │
│        or remote ASR)              /data/events.db  (optional)│
│                                         │                    │
│          /data/audio/                   ▼                    │
│           *.ogg / *.wav     Web server (aiohttp)             │
│                               ├── GET  /              → UI   │
│                               ├── GET  /api/events    → API  │
│                               ├── GET  /api/audio/:f  → play │
│                               ├── GET  /api/status    → info │
│                               └── WS   /ws/console    → live │
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

Events are written to SQLite and optionally published to MQTT. Traffic announcements and emergency broadcasts are tracked through their full lifecycle (start, RadioText updates, end with duration). Audio is automatically recorded during `traffic` and `emergency` events, then transcribed via Whisper.

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

1. Clone the repository and enter the directory:

```bash
git clone https://github.com/tubalainen/rds-guard.git
cd rds-guard
```

2. Copy the example config and set your FM frequency:

```bash
cp .env.example .env
```

Edit `.env` and at minimum set your local P4 frequency:

```bash
FM_FREQUENCY=103.3M        # Your local P4 frequency
```

If you want MQTT forwarding, enable it and configure the broker:

```bash
MQTT_ENABLED=true              # Enable MQTT (default: false)
MQTT_HOST=192.168.1.100        # Your MQTT broker IP
MQTT_PORT=1883
```

3. Pull and start:

```bash
docker compose up -d
```

This pulls the pre-built image from `ghcr.io/tubalainen/rds-guard:latest` (amd64 + arm64).

4. Open the web UI at **http://your-host:8022**

5. View logs:

```bash
docker compose logs -f rds-guard
```

> **Building locally:** To build from source instead of pulling the pre-built image, edit `docker-compose.yml` — comment out the `image:` line and uncomment `build: .`, then run `docker compose up -d --build`.

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

### Audio recording

Audio is always recorded during traffic and emergency events. Recordings are stored as OGG (Opus) by default.

| Variable | Default | Description |
|----------|---------|-------------|
| `AUDIO_DIR` | `/data/audio` | Directory for recorded audio files |
| `RECORD_EVENT_TYPES` | `traffic,emergency` | Comma-separated event types to record |
| `AUDIO_FORMAT` | `ogg` | Output format: `ogg` (Opus) or `wav` |
| `MAX_RECORDING_SEC` | `600` | Maximum recording duration (safety cutoff) |

### Transcription

Recorded audio is transcribed using Whisper for searchable text. Three engine modes are available:

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSCRIPTION_ENGINE` | `local` | `local` = built-in faster-whisper, `remote` = external ASR server, `none` = disable |
| `TRANSCRIPTION_LANGUAGE` | `sv` | Language code for Whisper (e.g. `sv`, `en`, `de`) |
| `TRANSCRIPTION_MODEL` | `small` | Whisper model size: `tiny`, `base`, `small`, `medium`, `large-v3` (local only) |
| `TRANSCRIPTION_DEVICE` | `cpu` | Compute device: `cpu` or `cuda` (local only) |
| `WHISPER_REMOTE_URL` | | URL of remote Whisper ASR server, e.g. `http://whisper:9000` (remote only) |
| `WHISPER_REMOTE_TIMEOUT` | `120` | HTTP timeout in seconds for remote ASR requests |

> **Local vs Remote:** Local transcription works out of the box but uses significant CPU/RAM (especially larger models). For Raspberry Pi or low-power hosts, use `TRANSCRIPTION_ENGINE=remote` with a [whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice) container running on a more powerful machine, or set `TRANSCRIPTION_ENGINE=none` to disable transcription while still recording audio.

## Web UI

The dashboard has two views:

**Events** — Traffic announcements, emergency broadcasts, and other alerts from the database. Active announcements pulse red. Filter by event type. Polls every 10 seconds. Events with recorded audio show an inline audio player. Completed transcriptions are displayed below each event card.

**Console** — Live stream of all decoded RDS groups via WebSocket. Pause/resume, text filter, 500-message buffer. Useful for debugging and seeing raw data flow.

The status bar at the bottom shows two rows of live data:
- **Top row:** Station name, PI code, frequency, programme type (PTY), TP/TA flags, pipeline health indicator, decode rate, and uptime
- **Bottom row:** Current RadioText and now-playing info (artist/title from RT+)

## REST API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/events` | GET | Query events. Params: `type`, `since`, `limit`, `offset` |
| `/api/events/active` | GET | In-progress announcements only |
| `/api/status` | GET | Decoder status (uptime, station info, decode rate) |
| `/api/audio/{filename}` | GET | Stream recorded audio file (OGG/WAV) |
| `/api/events` | DELETE | Clear all events |
| `/ws/console` | WS | Live stream of all decoded RDS messages |

Event responses include `audio_url` and `transcription` fields when available.

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
├── transcriber.py        # Whisper STT (local faster-whisper or remote ASR)
├── event_store.py        # SQLite wrapper
├── web_server.py         # aiohttp REST API + WebSocket + static serving
└── static/
    ├── index.html
    ├── css/
    │   └── style.css
    └── js/
        ├── app.js        # Tab routing, status bar
        ├── events.js     # Event cards, filters, audio player, transcriptions
        └── console.js    # WebSocket console, pause/filter
```

## Operations

### Update and restart

Pull the latest image and restart (preserves event history):

```bash
docker compose down && docker compose pull && docker compose up -d && docker compose logs -f
```

Clean restart from scratch (wipes the event database):

```bash
docker compose down -v && docker compose pull && docker compose up -d && docker compose logs -f
```

If building locally (with `build: .` in docker-compose.yml):

```bash
docker compose down && docker compose build --no-cache && docker compose up -d && docker compose logs -f
```

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
| Web UI loads but shows `---` everywhere | Pipeline hasn't started or RTL-SDR not found | Check logs for `[rtl_fm]` errors. Verify dongle is plugged in and driver is blacklisted |
| `usb_open error` in logs | Kernel DVB driver still loaded | Run `sudo modprobe -r dvb_usb_rtl28xxu` on the host |
| `No RTL-SDR devices found` | Dongle not passed to container | Verify `privileged: true` in docker-compose.yml or add the device explicitly |
| Pipeline status shows `error` | rtl_fm or redsea crashed | Check logs for `[rtl_fm]` / `[redsea]` error lines. Rebuild with `--no-cache` |
| Events show as "In progress" after restart | Normal — stale events from the previous run | They are automatically closed on startup. Do a clean rebuild with `-v` to clear old data |
| MQTT not publishing | MQTT disabled or broker unreachable | Set `MQTT_ENABLED=true` in `.env` and verify broker IP/port. Check logs for MQTT connection errors |
| Low decode rate (< 5 grp/s) | Weak signal or wrong frequency | Try increasing `RTL_GAIN`. Verify `FM_FREQUENCY` matches a nearby P4 transmitter |

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

Events in the database and web UI are always tagged with `frequency` and `station_ps`, so the Events tab works identically regardless of mode.

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
