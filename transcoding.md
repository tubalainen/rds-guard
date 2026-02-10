# Voice Recording & Transcription Plan

## Objective

When a Traffic Announcement (TA), Emergency Broadcast, or similar RDS event
occurs, **record the FM audio**, **transcribe it to text**, and make both the
transcription and the audio clip available through all output channels.

---

## 1. Architecture Overview

### 1.1 Output Architecture

Every recordable RDS event produces three artefacts: the **event metadata**
(RDS data, timestamps, RadioText), the **audio recording** (OGG/WAV), and the
**transcription** (speech-to-text). These flow into the system's output
channels as follows:

```
                        ┌─────────────────────────────────┐
                        │  RDS Event (TA / Emergency)     │
                        │  + Audio Recording               │
                        │  + Transcription                 │
                        └────────────┬────────────────────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              ▼                      ▼                      ▼
     ┌─── SQLite + Web UI ───┐  ┌── MQTT ──────────┐  ┌── Audio Files ──┐
     │  Always on             │  │  Optional         │  │  Always on      │
     │  Not configurable      │  │  If enabled:      │  │  /data/audio/   │
     │                        │  │  alerts ALWAYS    │  │                 │
     │  • Event metadata      │  │  include the      │  │  • OGG for web  │
     │  • Transcription text  │  │  transcription    │  │    playback     │
     │  • Audio playback      │  │  text             │  │  • WAV for STT  │
     │    (via web player)    │  │                   │  │    input        │
     └────────────────────────┘  └───────────────────┘  └─────────────────┘
```

**Design principles:**
- **Recording + playback is always on.** Every recordable event (traffic
  announcement, emergency broadcast) gets its audio saved. No toggle.
- **Transcription is on by default.** Local CPU Whisper runs out of the box.
  Can be switched to a remote GPU server for better performance, or disabled
  with `TRANSCRIPTION_ENGINE=none`.
- **MQTT is all-or-nothing for alerts.** If MQTT is enabled, alert payloads
  always include the transcription text (when available). There is no option
  to receive alerts without transcription — it is part of the event data.
- **SQLite + Web UI always shows everything.** Event metadata, transcription
  text, and audio playback are always present in the web interface.

### 1.2 What the User Configures

| Concern | Config needed? | Default |
|---------|---------------|---------|
| Audio recording | None — always on | Enabled |
| Audio playback in web UI | None — always on | Enabled |
| Transcription engine | Optional | `local` (CPU Whisper) |
| Remote Whisper server | Only if `TRANSCRIPTION_ENGINE=remote` | — |
| MQTT | Same as today (`MQTT_ENABLED`, `MQTT_HOST`, etc.) | Disabled |
| Which event types to record | Optional | `traffic,emergency` |

### 1.3 Current Pipeline

```
RTL-SDR → rtl_fm (stdout=PCM @ 171 kHz) → redsea (stdin) → JSON → Python
```

`rtl_fm -M fm` outputs **demodulated FM baseband audio** as raw signed-16-bit
little-endian PCM at 171 kHz. The `redsea` decoder reads this stream to extract
RDS data from the 57 kHz subcarrier. The audible voice content (0–15 kHz) is
present in the same stream but is currently discarded after RDS extraction.

### 1.4 Proposed Pipeline

```
RTL-SDR → rtl_fm (stdout=PCM @ 171 kHz)
                    │
                    ▼
            ┌── AudioTee (Python) ──┐
            │                       │
            ▼                       ▼
     redsea (stdin)         AudioRecorder
     JSON → Python        (always active)
            │                       │
            ▼                       ▼
     RulesEngine ──trigger──▶ start/stop
                                    │
                                    ▼
                            PCM buffer → WAV + OGG
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
              SQLite + Web UI   Transcriber      Audio files
              (event row +      (async)          /data/audio/
               audio_path)         │
                                   │
                      ┌────────────┴────────────┐
                      ▼                         ▼
               engine = "local"          engine = "remote"
               faster-whisper            HTTP POST → /asr
               (CPU, default)            (GPU server, LAN)
                      │                         │
                      └────────────┬────────────┘
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
              SQLite + Web UI   MQTT pub      WebSocket
              (update row       (if enabled)  broadcast
               with text)
```

**Key change**: Instead of connecting `rtl_fm.stdout` directly to
`redsea.stdin`, Python sits in the middle as a **tee**. Every chunk read from
`rtl_fm` is forwarded to `redsea` AND, when a recordable event is active
(TA or emergency), simultaneously written to an audio buffer. Recording is
always enabled — there is no toggle to disable it.

---

## 2. Component Design

### 2.1 Audio Tee — `audio_tee.py` (new module)

Replaces the direct `rtl_fm.stdout → redsea.stdin` pipe in `pipeline.py`.

**Responsibilities:**
- Read raw PCM chunks from `rtl_fm.stdout` in a tight loop
- Forward every chunk to `redsea.stdin` (writer thread)
- When recording is active, also push chunks to `AudioRecorder`
- Minimal overhead when not recording (just a boolean check per chunk)

**Design:**

```python
class AudioTee:
    def __init__(self, rtl_stdout, redsea_stdin, recorder):
        self._src = rtl_stdout        # rtl_fm stdout (binary stream)
        self._dst = redsea_stdin       # redsea stdin (binary stream)
        self._recorder = recorder      # AudioRecorder instance
        self._chunk_size = 8192        # ~24 ms at 171 kHz 16-bit mono

    def run(self):
        """Main loop — runs in the pipeline thread."""
        try:
            while True:
                chunk = self._src.read(self._chunk_size)
                if not chunk:
                    break  # EOF — rtl_fm died
                self._dst.write(chunk)
                self._dst.flush()
                if self._recorder.is_recording:
                    self._recorder.feed(chunk)
        finally:
            self._dst.close()
```

**Changes to `pipeline.py`:**
- Open `redsea` with `stdin=subprocess.PIPE` instead of `stdin=rtl_proc.stdout`
- Do NOT close `rtl_proc.stdout` in the parent (Python reads it)
- Instantiate `AudioTee` and run it in the pipeline thread
- `redsea.stdout` readline loop remains unchanged (separate thread)

**Threading model:**

```
Pipeline thread: AudioTee.run()  ← reads rtl_fm, writes redsea + recorder
New thread:      redsea stdout readline → on_line_callback (JSON processing)
```

### 2.2 Audio Recorder — `audio_recorder.py` (new module)

Manages recording lifecycle tied to RDS events.

**Responsibilities:**
- Accept start/stop signals from the rules engine
- Buffer raw PCM chunks during recording
- On stop: write WAV file, enqueue transcription job
- Handle edge cases (very short recordings, very long recordings)

**Design:**

```python
class AudioRecorder:
    """Records FM audio during active RDS events.

    Thread-safe: feed() is called from the AudioTee thread,
    start/stop are called from the pipeline callback thread.
    """

    # Raw PCM parameters (must match rtl_fm output)
    SAMPLE_RATE = 171_000   # Hz (from rtl_fm -s 171k)
    SAMPLE_WIDTH = 2        # bytes (signed 16-bit)
    CHANNELS = 1            # mono

    # Recording limits
    MIN_DURATION_SEC = 2    # discard very short recordings
    MAX_DURATION_SEC = 600  # 10 min hard cap (safety limit)

    def __init__(self, audio_dir, transcriber, on_complete):
        self._audio_dir = Path(audio_dir)
        self._transcriber = transcriber
        self._on_complete = on_complete  # callback(event_id, audio_path)
        self._lock = threading.Lock()
        self._recording = False
        self._buffer = None
        self._event_id = None
        self._started_at = None

    @property
    def is_recording(self):
        return self._recording

    def start(self, event_id):
        """Begin recording for the given event."""
        with self._lock:
            self._recording = True
            self._buffer = io.BytesIO()
            self._event_id = event_id
            self._started_at = time.time()

    def feed(self, chunk):
        """Append a PCM chunk (called from AudioTee thread)."""
        with self._lock:
            if not self._recording or self._buffer is None:
                return
            # Safety: enforce max duration
            elapsed = time.time() - self._started_at
            if elapsed > self.MAX_DURATION_SEC:
                self._finalize()
                return
            self._buffer.write(chunk)

    def stop(self):
        """Stop recording and finalize."""
        with self._lock:
            if not self._recording:
                return
            self._finalize()

    def _finalize(self):
        """Write WAV, enqueue transcription. Must hold self._lock."""
        self._recording = False
        raw_pcm = self._buffer.getvalue()
        self._buffer = None
        event_id = self._event_id
        self._event_id = None
        elapsed = time.time() - self._started_at

        if elapsed < self.MIN_DURATION_SEC or len(raw_pcm) == 0:
            log.info("Recording too short (%.1fs), discarding", elapsed)
            return

        # Save in background thread to avoid blocking the tee
        threading.Thread(
            target=self._save_and_transcribe,
            args=(event_id, raw_pcm, elapsed),
            daemon=True,
        ).start()

    def _save_and_transcribe(self, event_id, raw_pcm, duration):
        """Downsample, save WAV, run transcription."""
        # 1. Downsample 171 kHz → 16 kHz (for smaller files + STT input)
        # 2. Write WAV to /data/audio/{event_id}.wav
        # 3. Encode OGG/Opus for web playback: /data/audio/{event_id}.ogg
        # 4. Queue transcription
        # 5. Call on_complete callback with results
```

**Audio processing pipeline** (within `_save_and_transcribe`):

```
Raw PCM (171 kHz, 16-bit, mono)
    │
    ▼  scipy.signal.resample_poly  OR  audioop.ratecv
Resampled PCM (16 kHz, 16-bit, mono)
    │
    ├──▶ WAV file  (for archival / fallback playback)
    │
    ▼  ffmpeg  OR  opusenc  OR  soundfile+pyogg
OGG/Opus file  (for web playback — much smaller)
    │
    ▼
Transcription input (16 kHz WAV)
```

**File naming convention:**
```
/data/audio/{event_id}.wav     ← 16 kHz archive copy
/data/audio/{event_id}.ogg     ← Opus-encoded for web playback
```

### 2.3 Transcription Engine — `transcriber.py` (new module)

Converts recorded audio to text using a speech-to-text engine. Supports two
modes: **local** (built-in `faster-whisper`) and **remote** (external Whisper
ASR server with GPU acceleration).

#### 2.3.1 Mode A: Local — `faster-whisper` (default)

Rationale:
- Uses CTranslate2 — 4x faster than original Whisper, lower memory
- Runs on CPU (no GPU needed — suitable for Raspberry Pi / NUC)
- Excellent Swedish language support (Whisper was trained on Swedish data)
- Quantized models (int8) reduce memory to ~1 GB for `small` model
- Apache 2.0 license
- Zero network dependencies — fully self-contained

#### 2.3.2 Mode B: Remote — Whisper ASR Webservice (`/asr` endpoint)

For deployments where the RDS Guard host lacks CPU power (e.g., Raspberry Pi)
or where GPU acceleration is available on another machine, transcription can
be offloaded to a **remote Whisper ASR server** running on a separate Docker
host — typically a machine with an NVIDIA GPU.

The remote server exposes the standard Whisper ASR Webservice `/asr` HTTP
endpoint (see [onerahmet/openai-whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice)).
RDS Guard sends audio files via multipart POST and receives transcription text
in the response.

**Why remote?**
- GPU acceleration: `large-v3` model transcribes 2 minutes of audio in ~2-3
  seconds on an NVIDIA GPU vs. ~120+ seconds on a Pi 4 CPU
- Larger models: The `large-v3` model is too heavy for most SBCs but trivial
  on a GPU host, producing near-perfect Swedish transcription
- Shared resource: One GPU Whisper server can serve multiple RDS Guard
  instances (or other services)
- Keeps the RDS Guard container lightweight — no ML dependencies needed

**`/asr` API contract** (Whisper ASR Webservice):

```
POST /asr?encode=true&task=transcribe&language=sv&output=json
Content-Type: multipart/form-data

audio_file=@recording.wav
```

Response:
```json
{
  "text": "Trafikmeddelande från P4 Stockholm..."
}
```

#### 2.3.3 Engine Comparison

| Engine | Where it runs | Pros | Cons |
|--------|--------------|------|------|
| `local` (default) | RDS Guard container | Self-contained, no network | CPU-bound, limited model size |
| `remote` | Separate Docker host | GPU-accelerated, large models | Network dependency, extra infra |

#### 2.3.4 Design — Unified Transcriber with Backend Abstraction

```python
class Transcriber:
    """Speech-to-text engine with async job queue.

    Supports two backends selected by TRANSCRIPTION_ENGINE config:
      - "local"  → built-in faster-whisper (CPU)
      - "remote" → external Whisper ASR server via /asr HTTP endpoint
    """

    def __init__(self, engine, language, model_size=None, device=None,
                 remote_url=None, remote_timeout=120):
        self._engine = engine          # "local" or "remote"
        self._language = language
        self._model_size = model_size  # local only
        self._device = device          # local only
        self._remote_url = remote_url  # remote only (e.g. "http://gpu-host:9000")
        self._remote_timeout = remote_timeout
        self._queue = queue.Queue()
        self._model = None             # lazy-loaded for local engine

    def _load_local_model(self):
        """Load the faster-whisper model (one-time, ~10-30s on first call)."""
        from faster_whisper import WhisperModel
        self._model = WhisperModel(
            self._model_size,
            device=self._device,
            compute_type="int8" if self._device == "cpu" else "float16",
        )

    def enqueue(self, audio_path, event_id, callback):
        """Add a transcription job to the queue."""
        self._queue.put((audio_path, event_id, callback))

    def run(self):
        """Worker loop — runs in a dedicated thread."""
        while True:
            audio_path, event_id, callback = self._queue.get()
            try:
                if self._engine == "remote":
                    text = self._transcribe_remote(audio_path)
                else:
                    if self._model is None:
                        self._load_local_model()
                    text = self._transcribe_local(audio_path)
                callback(event_id, text, None)
            except Exception as e:
                log.error("Transcription failed for event %s: %s", event_id, e)
                callback(event_id, None, e)

    def _transcribe_local(self, audio_path):
        """Run transcription locally via faster-whisper."""
        segments, info = self._model.transcribe(
            str(audio_path),
            language=self._language,
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        texts = [seg.text.strip() for seg in segments if seg.text.strip()]
        return " ".join(texts)

    def _transcribe_remote(self, audio_path):
        """Send audio to remote Whisper ASR server via /asr endpoint."""
        import requests

        url = f"{self._remote_url.rstrip('/')}/asr"
        params = {
            "encode": "true",
            "task": "transcribe",
            "language": self._language,
            "output": "json",
        }

        with open(audio_path, "rb") as f:
            files = {"audio_file": (audio_path.name, f, "audio/wav")}
            resp = requests.post(
                url,
                params=params,
                files=files,
                timeout=self._remote_timeout,
            )

        resp.raise_for_status()
        result = resp.json()
        return result.get("text", "").strip()
```

#### 2.3.5 Model Selection Guidance

**Local mode** (`TRANSCRIPTION_ENGINE=local`):

| Model | Size | RAM | Accuracy | Speed (Pi 4) | Speed (x86) |
|-------|------|-----|----------|---------------|--------------|
| `tiny` | 75 MB | ~400 MB | Fair | ~2x real-time | ~10x |
| `base` | 150 MB | ~500 MB | Good | ~1x real-time | ~6x |
| `small` | 500 MB | ~1 GB | Very good | ~0.3x | ~3x |
| `medium` | 1.5 GB | ~2.5 GB | Excellent | Too slow | ~1x |

Recommendation: `small` for x86/NUC, `base` or `tiny` for Raspberry Pi 4.

**Remote mode** (`TRANSCRIPTION_ENGINE=remote`):

The model is selected on the **remote server**, not in RDS Guard. This allows
using `large-v3` (best accuracy) without impacting the RDS Guard host:

| Model | VRAM | Accuracy | Speed (RTX 3060) | Speed (RTX 4090) |
|-------|------|----------|-------------------|-------------------|
| `small` | ~2 GB | Very good | ~15x real-time | ~30x |
| `medium` | ~5 GB | Excellent | ~8x real-time | ~20x |
| `large-v3` | ~10 GB | Near-perfect | ~5x real-time | ~15x |

Recommendation: `large-v3` on GPU for best Swedish transcription quality.

### 2.4 Configuration — `config.py` additions

Recording and web playback are always on — no enable/disable toggle. The only
user-facing configuration is which transcription engine to use and, if remote,
where to find it.

```python
# --- Audio Recording (always on) ---
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")
RECORD_EVENT_TYPES = os.environ.get("RECORD_EVENT_TYPES", "traffic,emergency")
AUDIO_FORMAT = os.environ.get("AUDIO_FORMAT", "ogg")
MAX_RECORDING_SEC = _int(os.environ.get("MAX_RECORDING_SEC"), 600)

# --- Transcription ---
# "local"  = built-in faster-whisper on CPU (default, works out of the box)
# "remote" = external Whisper ASR server via HTTP /asr endpoint
# "none"   = disable transcription (audio is still recorded and playable)
TRANSCRIPTION_ENGINE = os.environ.get("TRANSCRIPTION_ENGINE", "local")
TRANSCRIPTION_LANGUAGE = os.environ.get("TRANSCRIPTION_LANGUAGE", "sv")

# Local engine settings (only used when TRANSCRIPTION_ENGINE=local)
TRANSCRIPTION_MODEL = os.environ.get("TRANSCRIPTION_MODEL", "small")
TRANSCRIPTION_DEVICE = os.environ.get("TRANSCRIPTION_DEVICE", "cpu")

# Remote engine settings (only used when TRANSCRIPTION_ENGINE=remote)
WHISPER_REMOTE_URL = os.environ.get("WHISPER_REMOTE_URL", "")
WHISPER_REMOTE_TIMEOUT = _int(os.environ.get("WHISPER_REMOTE_TIMEOUT"), 120)
```

**Note on MQTT**: Transcription text is always included in MQTT alert payloads
when MQTT is enabled. There is no separate toggle — if you receive alerts, you
receive transcriptions. This matches the principle that transcription is simply
part of the event data, not a separate opt-in feature.

### 2.5 Database Schema Changes — `event_store.py`

**New columns on `events` table:**

```sql
ALTER TABLE events ADD COLUMN audio_path TEXT;
ALTER TABLE events ADD COLUMN transcription TEXT;
ALTER TABLE events ADD COLUMN transcription_status TEXT;
-- transcription_status: null | "recording" | "transcribing" | "done" | "error"
```

**Migration strategy**: Use `ALTER TABLE ADD COLUMN IF NOT EXISTS` in
`init_db()`. SQLite added `IF NOT EXISTS` for columns in version 3.35.0
(2021-03-12). For older versions, catch the "duplicate column" error.

**New functions:**

```python
def update_event_audio(event_id, audio_path):
    """Set the audio file path for an event."""

def update_event_transcription(event_id, transcription, status="done"):
    """Set the transcription text and status for an event."""

def update_event_transcription_status(event_id, status):
    """Update just the transcription status (recording/transcribing/error)."""
```

**Retention purge update**: When deleting old events, also delete their
associated audio files from disk:

```python
def purge_old_events(days):
    # 1. Query audio_path for events about to be deleted
    # 2. Delete the rows
    # 3. Delete the audio files from disk
```

### 2.6 MQTT Changes — `rds_guard.py`

If MQTT is enabled, alert payloads **always** include transcription data — there
is no separate opt-in. Transcription is part of the event, just like RadioText.

Transcription is **asynchronous** — it completes seconds to minutes after the
TA ends. This means the `end` alert cannot contain the transcription text. The
MQTT design accounts for this timing gap with a separate `transcribed` state
published later.

**Topics overview (essential mode):**

```
rds/alert                          ← existing; all event lifecycle messages
rds/{pi}/traffic/transcription     ← NEW; dedicated topic for transcription results
```

#### Stage 1: TA Start (T+0s) — `rds/alert`

Existing payload structure, with one new field added:

```json
{
  "type": "traffic",
  "state": "start",
  "station": {
    "pi": "0x9E04",
    "ps": "P4 Stockholm"
  },
  "frequency": "103.3M",
  "prog_type": "News",
  "event_id": 42,
  "timestamp": "2024-02-10T15:30:00"
}
```

| Field | Status | Notes |
|-------|--------|-------|
| `event_id` | **NEW** | Database row ID, used to correlate later transcription messages |
| All others | Existing | Unchanged from current code |

#### Stage 2: RadioText Update (T+5s) — `rds/alert`

Unchanged from current code. No recording/transcription fields — this is a
real-time RDS data update:

```json
{
  "type": "traffic",
  "state": "update",
  "station": {
    "pi": "0x9E04",
    "ps": "P4 Stockholm"
  },
  "frequency": "103.3M",
  "radiotext": "Olycka E4 norrgående vid Rotebro",
  "all_radiotext": [
    "Olycka E4 norrgående vid Rotebro"
  ],
  "started": "2024-02-10T15:30:00",
  "timestamp": "2024-02-10T15:30:05"
}
```

#### Stage 3: TA End (T+135s) — `rds/alert`

Published **immediately** when TA flag goes false. Transcription is not yet
available — the audio has just stopped recording and is being processed:

```json
{
  "type": "traffic",
  "state": "end",
  "station": {
    "pi": "0x9E04",
    "ps": "P4 Stockholm"
  },
  "frequency": "103.3M",
  "started": "2024-02-10T15:30:00",
  "ended": "2024-02-10T15:32:15",
  "duration_sec": 135,
  "radiotext": [
    "Olycka E4 norrgående vid Rotebro",
    "Två fält blockerade"
  ],
  "prog_type": "News",
  "audio_available": true,
  "transcription_status": "transcribing",
  "event_id": 42,
  "timestamp": "2024-02-10T15:32:15"
}
```

| Field | Status | Notes |
|-------|--------|-------|
| `audio_available` | **NEW** | `true` — recording is always saved for recordable event types |
| `transcription_status` | **NEW** | `"transcribing"` (in progress) or `"none"` (if `TRANSCRIPTION_ENGINE=none`) |
| `event_id` | **NEW** | Same ID from the start event, for correlation |
| All others | Existing | Unchanged from current code |

**Important**: No `transcription` field is present here — the text is not yet
available. MQTT consumers that need the transcription must listen for the
Stage 4 message.

#### Stage 4: Transcription Complete (T+145s) — two publishes

When the transcriber finishes (typically 2-30s after TA end, depending on
engine and audio length), **two** messages are published:

**4a. `rds/alert` — lifecycle completion:**

```json
{
  "type": "traffic",
  "state": "transcribed",
  "event_id": 42,
  "station": {
    "pi": "0x9E04",
    "ps": "P4 Stockholm"
  },
  "frequency": "103.3M",
  "started": "2024-02-10T15:30:00",
  "ended": "2024-02-10T15:32:15",
  "duration_sec": 135,
  "radiotext": [
    "Olycka E4 norrgående vid Rotebro",
    "Två fält blockerade"
  ],
  "transcription": "Trafikmeddelande från P4 Stockholm. Det har inträffat en olycka på E4 norrgående vid Rotebro. Två av tre fält är blockerade. Räkna med längre restid.",
  "transcription_status": "done",
  "audio_available": true,
  "timestamp": "2024-02-10T15:32:45"
}
```

This is the **complete** event record — all fields populated. Home Assistant
automations, Node-RED flows, etc. can trigger on `state == "transcribed"` to
get the full announcement including the spoken text.

**4b. `rds/{pi}/traffic/transcription` — dedicated topic:**

Published on a per-station topic for consumers that only care about
transcription results (e.g., a text-to-speech relay, logging pipeline, or
notification bot):

```json
{
  "event_id": 42,
  "station": {
    "pi": "0x9E04",
    "ps": "P4 Stockholm"
  },
  "transcription": "Trafikmeddelande från P4 Stockholm. Det har inträffat en olycka på E4 norrgående vid Rotebro. Två av tre fält är blockerade. Räkna med längre restid.",
  "language": "sv",
  "duration_sec": 135,
  "radiotext": [
    "Olycka E4 norrgående vid Rotebro",
    "Två fält blockerade"
  ],
  "timestamp": "2024-02-10T15:32:45"
}
```

This topic is **retained** (`retain=true`) so new MQTT subscribers immediately
get the last transcription for each station.

#### Stage 4 (error): Transcription Failed — `rds/alert`

If transcription fails (model error, remote server unreachable, timeout):

```json
{
  "type": "traffic",
  "state": "transcription_failed",
  "event_id": 42,
  "station": {
    "pi": "0x9E04",
    "ps": "P4 Stockholm"
  },
  "transcription_status": "error",
  "transcription_error": "Remote server timeout after 120s",
  "audio_available": true,
  "timestamp": "2024-02-10T15:34:15"
}
```

Audio is still available for manual playback even when transcription fails.

#### MQTT State Machine Summary

```
state: "start"                → TA flag on  (recording begins)
state: "update"               → RadioText received during TA
state: "end"                  → TA flag off (recording stops, transcription queued)
state: "transcribed"          → Transcription complete (full text available)
state: "transcription_failed" → Transcription error (audio still available)
```

For consumers that don't care about transcription, the existing `start` →
`update` → `end` flow is unchanged. The `transcribed` and
`transcription_failed` states are additive — ignoring them produces the same
behavior as the current system.

#### Emergency / PTY Alert Events

Emergency broadcasts follow the same pattern but with `type: "emergency"`:

```json
{
  "type": "emergency",
  "state": "transcribed",
  "event_id": 57,
  "station": { "pi": "0x9E04", "ps": "P4 Stockholm" },
  "prog_type": "Alarm",
  "transcription": "Viktigt meddelande till allmänheten...",
  "transcription_status": "done",
  "audio_available": true,
  "timestamp": "2024-02-10T16:00:45"
}
```

The dedicated transcription topic for emergencies:
```
rds/{pi}/emergency/transcription
```

### 2.7 Web Server Changes — `web_server.py`

**New endpoint:**
```
GET /api/audio/{event_id}.{format}    ← Serve audio file (ogg/wav)
```

This endpoint reads the file from `/data/audio/` and returns it with the
appropriate `Content-Type` header (`audio/ogg`, `audio/wav`, etc.).

**Updated event API responses**: The existing `/api/events` and
`/api/events/active` endpoints already return the full event row. The new
`audio_path`, `transcription`, and `transcription_status` columns will be
included automatically. The `audio_path` field will be transformed to a
relative URL (`/api/audio/{event_id}.ogg`) in the response for frontend
consumption.

### 2.8 Web UI Changes — `static/js/events.js` + `static/css/style.css`

**Event card additions:**

```
┌───────────────────────────────────────────────────────┐
│ 🔴 TRAFFIC ANNOUNCEMENT                    13:45:32  │
│ P4 Stockholm · 103.3 MHz                              │
│                                                       │
│ RadioText:                                            │
│   "Olycka E4 norrgående vid Rotebro"                 │
│   "Två fält blockerade"                               │
│                                                       │
│ Transcription:                                        │
│   "Trafikmeddelande från P4 Stockholm. Det har        │
│    inträffat en olycka på E4 norrgående vid Rotebro.  │
│    Två av tre fält är blockerade. Räkna med längre    │
│    restid."                                           │
│                                                       │
│   ▶ Play recording (2:15)              Duration: 2m 15s │
│                                              Ended    │
└───────────────────────────────────────────────────────┘
```

**New UI elements in event cards:**

1. **Transcription block** — Displayed below the RadioText section. Styled
   differently (slightly muted, with a label). Shows a spinner/status indicator
   while transcription is in progress.

2. **Audio player** — A compact inline audio player:
   - Play/pause button with waveform-style progress indicator
   - Duration display
   - Uses the HTML5 `<audio>` element with the `/api/audio/{id}.ogg` source
   - Only shown when `audio_path` is not null
   - Lazy-loads audio (doesn't fetch until user clicks play)

3. **Status indicators:**
   - Recording in progress: pulsing red dot + "Recording..."
   - Transcribing: spinner + "Transcribing..."
   - Done: transcription text shown
   - Error: "Transcription failed" message

---

## 3. Integration Points

### 3.1 Recording Trigger Flow

```
process_group()
    └─▶ rules_engine.on_ta_change(ta=True)
            ├─▶ event_store.insert_event()  → event_id
            ├─▶ audio_recorder.start(event_id)     ← NEW
            └─▶ MQTT pub + WebSocket broadcast

process_group()  [later...]
    └─▶ rules_engine.on_ta_change(ta=False)
            ├─▶ audio_recorder.stop()               ← NEW
            │       └─▶ _save_and_transcribe()
            │               ├─▶ downsample + save WAV/OGG
            │               ├─▶ event_store.update_event_audio()
            │               ├─▶ transcriber.enqueue()
            │               │       └─▶ [async] transcribe()
            │               │               ├─▶ event_store.update_event_transcription()
            │               │               ├─▶ MQTT pub transcription
            │               │               └─▶ WebSocket broadcast update
            │               └─▶ MQTT pub (end event with audio_available=true)
            ├─▶ event_store.end_event()
            └─▶ MQTT pub + WebSocket broadcast
```

### 3.2 Emergency Broadcast Recording

Same pattern but triggered by `on_pty_alert()`. Since emergency broadcasts
don't have an explicit "end" signal (PTY changes back from Alarm), use a
timeout:

- Start recording when PTY → Alarm
- Stop recording when PTY changes away from Alarm, OR after `MAX_RECORDING_SEC`

### 3.3 Thread Safety

```
AudioTee thread:     reads rtl_fm, writes redsea + recorder.feed()
Pipeline thread:     reads redsea JSON, calls process_group()
Transcriber thread:  runs STT model, calls completion callback
Main thread:         signal handling

Shared state:
  audio_recorder._recording  ← protected by _lock
  audio_recorder._buffer     ← protected by _lock
  event_store (SQLite)       ← protected by existing _lock
```

---

## 4. Implementation Plan

### Phase 1: Audio Tee + Recording Infrastructure

**Files to modify:**
- `pipeline.py` — Restructure to use AudioTee instead of direct pipe
- `config.py` — Add recording/transcription config vars
- `event_store.py` — Add schema migration + new columns + new functions

**New files:**
- `audio_tee.py` — PCM stream splitter
- `audio_recorder.py` — Recording lifecycle manager

**Tasks:**
1. Add new config variables to `config.py` (transcription engine settings,
   audio dir, format, etc. — no recording toggle)
2. Add `audio_path`, `transcription`, `transcription_status` columns to
   `event_store.py` with migration
3. Add `update_event_audio()`, `update_event_transcription()`,
   `update_event_transcription_status()` functions
4. Create `audio_tee.py` with the `AudioTee` class
5. Create `audio_recorder.py` with the `AudioRecorder` class (PCM buffering,
   WAV writing, downsampling)
6. Modify `pipeline.py` to use `AudioTee` (always active — recording is
   unconditional)
7. Update retention purge to delete audio files

### Phase 2: Rules Engine Integration

**Files to modify:**
- `rds_guard.py` — Wire recorder start/stop to rules engine events

**Tasks:**
1. Pass `AudioRecorder` instance to `RulesEngine`
2. Call `recorder.start(event_id)` in `on_ta_change(ta=True)`
3. Call `recorder.stop()` in `on_ta_change(ta=False)`
4. Call `recorder.start(event_id)` in `on_pty_alert()` with timeout logic
5. Initialize recorder + tee in `main()` startup sequence

### Phase 3: Transcription Engine

**New files:**
- `transcriber.py` — STT engine abstraction with local + remote backends

**Tasks:**
1. Implement `Transcriber` class with backend selection (`local` / `remote`)
2. Implement local backend: `_transcribe_local()` using `faster-whisper` with
   lazy model loading (first transcription triggers download/load)
3. Implement remote backend: `_transcribe_remote()` using HTTP POST to the
   Whisper ASR Webservice `/asr` endpoint
4. Validate `WHISPER_REMOTE_URL` on startup when `TRANSCRIPTION_ENGINE=remote`
   (log warning if unreachable, but don't block startup)
5. Implement job queue with callback mechanism
6. Wire transcription completion to `event_store.update_event_transcription()`
7. Wire transcription completion to MQTT publish
8. Wire transcription completion to WebSocket broadcast
9. Add retry logic for remote backend (1 retry with backoff on network errors)
10. Start transcriber worker thread in `main()`

### Phase 4: Web API + Audio Serving

**Files to modify:**
- `web_server.py` — Add audio file endpoint + update event responses

**Tasks:**
1. Add `GET /api/audio/{event_id}.{format}` endpoint
2. Return audio files with correct `Content-Type` and caching headers
3. Add `audio_url` computed field to event API responses
4. Handle 404 for missing audio files

### Phase 5: Web UI

**Files to modify:**
- `static/js/events.js` — Add transcription display + audio player
- `static/css/style.css` — Style new elements

**Tasks:**
1. Add transcription section to `renderEventCard()`
2. Add inline audio player with play/pause controls
3. Add transcription status indicators (recording/transcribing/done/error)
4. Style transcription block and audio player
5. Handle live updates via WebSocket for transcription completion

### Phase 6: Docker + Packaging

**Files to modify:**
- `Dockerfile` — Add system dependencies (ffmpeg, audio libraries)
- `requirements.txt` — Add Python dependencies
- `docker-compose.yml` — Update volume mounts if needed
- `.env.example` — Document new env vars

**Tasks:**
1. Add `ffmpeg` to Dockerfile runtime stage (for audio encoding)
2. Add Python deps: `faster-whisper`, `numpy`, `requests`
3. Create `/data/audio` directory in entrypoint
4. Update `.env.example` with new configuration options (see Section 14)
5. Add example `docker-compose.gpu.yml` for combined RDS Guard + remote
   Whisper ASR deployment (see Section 13.9)
6. Test builds on both x86_64 and arm64 (Pi)
7. Document remote Whisper setup in README (see Section 13)

---

## 5. Dependencies

### New Python Packages

```
faster-whisper>=1.0.0      # Local STT engine (includes CTranslate2)
numpy>=1.24.0              # Audio resampling
requests>=2.31.0           # Remote Whisper ASR HTTP client
```

`numpy` is already a transitive dependency of `faster-whisper`, but listing it
explicitly for the resampling use case.

`requests` is used by the remote transcription backend to POST audio to the
Whisper ASR Webservice `/asr` endpoint.

**Note**: When `TRANSCRIPTION_ENGINE=remote` or `none`, the `faster-whisper`
package is installed but never imported (lazy-loaded). No model is downloaded
and no memory is used. The package is included in all image variants so users
can switch between `local` and `remote` without rebuilding.

### New System Packages (Dockerfile)

```
ffmpeg          # Audio encoding (PCM → OGG/Opus)
```

`ffmpeg` is used via subprocess for encoding — no Python binding needed. This
keeps the approach simple and avoids complex native library builds.

---

## 6. Configuration Reference

Audio recording and web playback are **always on** — no configuration needed.
Transcription is **on by default** (local CPU Whisper). The only decisions
the user makes are:

1. Do I want to use a remote GPU server for transcription? → Set
   `TRANSCRIPTION_ENGINE=remote` and `WHISPER_REMOTE_URL`.
2. Do I want to disable transcription entirely? → Set
   `TRANSCRIPTION_ENGINE=none` (audio is still recorded and playable).

### New Variables (all optional)

| Variable | Default | Description |
|----------|---------|-------------|
| **Transcription engine** | | |
| `TRANSCRIPTION_ENGINE` | `local` | `local` = built-in Whisper (default), `remote` = GPU server, `none` = disabled |
| `TRANSCRIPTION_LANGUAGE` | `sv` | Language hint for Whisper (ISO 639-1 code) |
| **Local mode** (`local`) | | |
| `TRANSCRIPTION_MODEL` | `small` | Whisper model size: `tiny`, `base`, `small`, `medium` |
| `TRANSCRIPTION_DEVICE` | `cpu` | Compute device: `cpu` or `cuda` |
| **Remote mode** (`remote`) | | |
| `WHISPER_REMOTE_URL` | _(empty)_ | Base URL of the Whisper ASR server (e.g., `http://192.168.1.50:9000`) |
| `WHISPER_REMOTE_TIMEOUT` | `120` | HTTP timeout in seconds for remote requests |
| **Advanced / rarely changed** | | |
| `AUDIO_DIR` | `/data/audio` | Directory for audio files |
| `RECORD_EVENT_TYPES` | `traffic,emergency` | Which RDS event types trigger recording |
| `AUDIO_FORMAT` | `ogg` | Web playback format: `ogg`, `wav`, `mp3` |
| `MAX_RECORDING_SEC` | `600` | Safety cap on recording duration (seconds) |

### What Is NOT Configurable

| Feature | Behavior |
|---------|----------|
| Audio recording | Always on for configured event types |
| Audio playback in web UI | Always available when audio file exists |
| Transcription in MQTT alerts | Always included if MQTT is enabled (no opt-out) |
| Transcription in web UI | Always displayed when transcription exists |
| Events in SQLite | Always stored (existing behavior) |

---

## 7. Data Flow Diagram — Complete Event Lifecycle

```
T+0s    TA flag → true
        ├─ event_store.insert_event(state='start')           → event_id=42
        ├─ recorder.start(event_id=42)
        │    └─ AudioRecorder begins buffering PCM chunks
        ├─ event_store.update_transcription_status(42, "recording")
        ├─ MQTT: rds/alert
        │    {type:"traffic", state:"start", event_id:42,
        │     station:{pi:"0x9E04", ps:"P4 Stockholm"},
        │     frequency:"103.3M", prog_type:"News"}
        └─ WS broadcast (same payload)

T+5s    RadioText received: "Olycka E4 norrgående"
        ├─ event_store.update_event_radiotext(42, [...])
        ├─ MQTT: rds/alert
        │    {type:"traffic", state:"update",
        │     radiotext:"Olycka E4 norrgående",
        │     all_radiotext:["Olycka E4 norrgående"],
        │     started:"2024-02-10T15:30:00"}
        └─ WS broadcast (same payload)

T+135s  TA flag → false
        │
        │  [synchronous — in rules engine thread]
        ├─ recorder.stop()
        │    └─ spawns background thread: _save_and_transcribe()
        ├─ event_store.end_event(42, state='end', duration=135)
        ├─ MQTT: rds/alert
        │    {type:"traffic", state:"end", event_id:42,
        │     started:"…T15:30:00", ended:"…T15:32:15",
        │     duration_sec:135,
        │     radiotext:["Olycka E4 norrgående", "Två fält blockerade"],
        │     audio_available:true,
        │     transcription_status:"transcribing"}       ← text NOT yet available
        └─ WS broadcast (same payload)
        │
        │  [async — in background thread, ~1-2s later]
        ├─ Downsample: 171 kHz → 16 kHz  (~4.3 MB)
        ├─ Write: /data/audio/42.wav
        ├─ Encode: /data/audio/42.ogg  (Opus, ~200 KB)
        ├─ event_store.update_event_audio(42, "42.ogg")
        ├─ event_store.update_transcription_status(42, "transcribing")
        └─ transcriber.enqueue(42.wav, callback)

T+145s  Transcription complete (~10s for 135s audio on x86;
        ~2-3s via remote GPU)
        │
        │  [in transcriber worker thread]
        ├─ event_store.update_event_transcription(42,
        │    "Trafikmeddelande från P4 Stockholm...", status="done")
        ├─ MQTT: rds/alert
        │    {type:"traffic", state:"transcribed", event_id:42,
        │     station:{pi:"0x9E04", ps:"P4 Stockholm"},
        │     started:"…T15:30:00", ended:"…T15:32:15",
        │     duration_sec:135,
        │     radiotext:["Olycka E4 norrgående", "Två fält blockerade"],
        │     transcription:"Trafikmeddelande från P4 Stockholm. Det har
        │       inträffat en olycka på E4 norrgående vid Rotebro. Två av
        │       tre fält är blockerade. Räkna med längre restid.",
        │     transcription_status:"done",
        │     audio_available:true}
        ├─ MQTT: rds/0x9E04/traffic/transcription  (retain=true)
        │    {event_id:42, station:{…},
        │     transcription:"Trafikmeddelande...",
        │     language:"sv", duration_sec:135,
        │     radiotext:[...]}
        └─ WS broadcast: {topic:"transcription", event_id:42,
             transcription:"Trafikmeddelande..."}

T+145s  (alternate — transcription FAILED)
        ├─ event_store.update_event_transcription(42, null, status="error")
        ├─ MQTT: rds/alert
        │    {type:"traffic", state:"transcription_failed", event_id:42,
        │     transcription_status:"error",
        │     transcription_error:"Remote server timeout after 120s",
        │     audio_available:true}
        └─ WS broadcast: {topic:"transcription_error", event_id:42,
             error:"Remote server timeout after 120s"}
```

---

## 8. Web UI Mockup

### Event Card with Transcription + Audio

```
┌────────────────────────────────────────────────────────────┐
│  ● TRAFFIC ANNOUNCEMENT                         13:45:32  │
│  P4 Stockholm · 103.3 MHz                                  │
│                                                            │
│  ┌─ RadioText ──────────────────────────────────────────┐  │
│  │ "Olycka E4 norrgående vid Rotebro"                   │  │
│  │ "Två fält blockerade"                                │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                            │
│  ┌─ Transcription ──────────────────────────────────────┐  │
│  │ Trafikmeddelande från P4 Stockholm. Det har inträffat│  │
│  │ en olycka på E4 norrgående vid Rotebro. Två av tre   │  │
│  │ fält är blockerade. Räkna med längre restid.         │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                            │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ ▶  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  2:15  │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                            │
│                     Duration: 2m 15s              Ended    │
└────────────────────────────────────────────────────────────┘
```

### Transcription Status States

```
Recording:     ● Recording...
Transcribing:  ◌ Transcribing...
Done:          (shows transcription text)
Error:         ⚠ Transcription failed
No recording:  (nothing shown)
```

---

## 9. Edge Cases & Error Handling

| Scenario | Handling |
|----------|----------|
| RTL-SDR produces silence | Transcription returns empty string; stored as-is |
| Very short TA (< 2s) | Recording discarded; no audio file created |
| Very long TA (> 10 min) | Recording capped at `MAX_RECORDING_SEC` |
| Transcription model fails to load (local) | Status set to "error"; audio still saved and playable |
| Disk full | Audio write fails; logged as error; RDS event still stored |
| Multiple simultaneous TAs (different PIs) | Each gets its own recorder buffer (keyed by PI) |
| App restart during recording | Stale recordings cleaned up on startup |
| Audio file deleted externally | API returns 404; UI shows "Audio unavailable" |
| Whisper model not downloaded yet (local) | Auto-downloads on first use (one-time) |
| `TRANSCRIPTION_ENGINE=none` | No transcription; audio is still recorded and playable |
| Remote Whisper server unreachable | 1 retry with 5s backoff; status set to "error"; audio still available |
| Remote Whisper server returns HTTP error | Logged; status set to "error"; retry on 5xx, fail on 4xx |
| Remote Whisper server times out | After `WHISPER_REMOTE_TIMEOUT` seconds; status "error" |
| `WHISPER_REMOTE_URL` empty when engine=remote | Log error on startup; transcription disabled; recording still works |
| Remote server returns unexpected JSON | Graceful fallback; log raw response; status "error" |
| Network partition during transcription | Request times out; audio preserved; can re-transcribe later |
| MQTT disabled | Events still stored in SQLite with transcription; web UI unaffected |

---

## 10. Performance Considerations

### Memory Impact

- **AudioTee overhead (not recording):** ~zero (one boolean check per chunk)
- **AudioTee overhead (recording):** raw PCM buffer grows at ~342 KB/s
  (171 kHz × 2 bytes). A 2-minute TA ≈ 41 MB in memory.
- **Local Whisper model resident:** ~1 GB for `small`, ~500 MB for `base`
- **Remote mode:** ~zero additional memory (no model loaded; only HTTP client)
- **Mitigation:** Lazy-load model only on first transcription; unload after
  idle timeout (configurable)

### CPU Impact

- **AudioTee:** Negligible — memcpy-level operations
- **Resampling (171→16 kHz):** Brief spike (~1-2s for 2-min recording)
- **FFmpeg encoding:** Brief spike (~1-2s)
- **Local Whisper transcription:** Significant — ~10s per minute of audio on
  x86, ~60s per minute on Pi 4. Runs in dedicated thread, doesn't block
  pipeline.
- **Remote Whisper transcription:** Near-zero CPU on RDS Guard host. Only
  the HTTP POST transfer + JSON parsing. All heavy computation runs on the
  remote GPU server.

### Disk Impact

- **Per event:** ~200 KB OGG + ~4.3 MB WAV per minute of audio
- **OGG-only mode:** ~200 KB per minute
- **30-day retention at 5 events/day:** ~30 MB (OGG only) to ~650 MB (WAV+OGG)
- **Mitigation:** Configurable retention; WAV can be optional (OGG sufficient
  for playback; Whisper can read OGG directly)

### Network Impact (remote mode only)

- **Upload per event:** ~4.3 MB WAV for 1 minute of audio (16 kHz mono)
- **Download:** ~1 KB JSON response
- **Latency:** Typically 2-5 seconds for a 2-minute recording on a local
  network with GPU. Budget for up to `WHISPER_REMOTE_TIMEOUT` seconds.
- **Bandwidth:** Negligible for typical TA frequency (~5/day)

---

## 11. Testing Strategy

### Unit Tests

- `test_audio_tee.py` — Verify chunks forwarded correctly; recording toggle
- `test_audio_recorder.py` — Start/stop lifecycle; WAV output; duration limits
- `test_transcriber.py` — Mock STT model; queue processing; error handling;
  test both `_transcribe_local()` and `_transcribe_remote()` paths
- `test_transcriber_remote.py` — Mock HTTP responses; test error handling for
  timeouts, HTTP errors, malformed JSON, unreachable server
- `test_event_store.py` — Schema migration; new columns; audio/transcription updates

### Integration Tests

- End-to-end: simulated TA flag sequence → audio file + transcription in DB
- MQTT payload verification with transcription fields
- Web API: audio file serving with correct headers
- Web UI: manual testing of player and transcription display
- Remote transcription: start a local Whisper ASR container and verify the
  full POST → response → DB update cycle

### Performance Tests

- AudioTee throughput: verify no dropped samples under load
- Transcription latency benchmarks per model size (local)
- Remote transcription latency over LAN vs. localhost
- Memory profiling during long recordings

---

## 12. Rollout Plan

1. **Works out of the box**: Recording + local transcription are on by default.
   Existing users upgrading get the new features without changing their `.env`.
   The Whisper model downloads automatically on first TA event (~500 MB for
   `small`), so the first transcription may be delayed by the download.
2. **Graceful degradation**: If the local Whisper model is too heavy for the
   host (e.g., Pi with <1 GB free RAM), users can switch to remote
   (`TRANSCRIPTION_ENGINE=remote`) or disable (`TRANSCRIPTION_ENGINE=none`).
   Audio recording and playback work regardless.
3. **MQTT is unchanged**: Existing MQTT users get transcription text in their
   alerts automatically — no new config needed. The `transcribed` state is
   additive; ignoring it preserves existing automation behavior.
4. **Docker image grows**: The image includes `faster-whisper` + `ffmpeg` +
   `numpy` even for remote-only users. A future slim image variant could omit
   these. Document the size increase in release notes.
5. **Documentation**: Update README with transcription engine options, remote
   Whisper setup guide (Section 13), and hardware recommendations per model
   size.

---

## 13. Remote Whisper ASR Server — Deployment Guide

This section covers how to set up a GPU-accelerated Whisper ASR server on a
separate Docker host for use with `TRANSCRIPTION_ENGINE=remote`.

### 13.1 Overview

The remote server runs the
[openai-whisper-asr-webservice](https://github.com/ahmetoner/whisper-asr-webservice)
Docker image. This provides a REST API with the `/asr` endpoint that accepts
audio files and returns transcribed text. It supports NVIDIA GPU acceleration
via CUDA, enabling the use of larger, more accurate Whisper models that would
be impractical on the RDS Guard host.

```
┌─────────────────────┐         HTTP POST          ┌──────────────────────┐
│   RDS Guard Host    │  ──── /asr?language=sv ──▶  │  GPU Host (Whisper)  │
│   (Pi / NUC)        │  ◀── JSON response ──────  │  (NVIDIA GPU)        │
│                     │                             │                      │
│  TRANSCRIPTION_     │                             │  whisper-asr-        │
│  ENGINE=remote      │                             │  webservice:latest-  │
│  WHISPER_REMOTE_    │                             │  gpu                 │
│  URL=http://gpu:9k  │                             │  ASR_MODEL=large-v3  │
└─────────────────────┘                             └──────────────────────┘
```

### 13.2 Prerequisites

On the GPU host:
- **Docker** (with Docker Compose v2)
- **NVIDIA GPU** with at least 10 GB VRAM (for `large-v3` model, less for
  smaller models)
- **NVIDIA Container Toolkit** (`nvidia-container-toolkit`) installed and
  configured so Docker can access the GPU

Verify GPU access from Docker:
```bash
docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
```

### 13.3 Docker Compose — GPU Whisper Server

Create a `docker-compose.yml` on the GPU host:

```yaml
# docker-compose.yml — Whisper ASR Server (GPU)
# Place this on your GPU-equipped Docker host

services:
  whisper-asr:
    image: onerahmet/openai-whisper-asr-webservice:latest-gpu
    container_name: whisper-asr
    restart: unless-stopped
    ports:
      - "9000:9000"
    environment:
      # Whisper model to use — larger = more accurate, more VRAM
      # Options: tiny, base, small, medium, large-v3
      - ASR_MODEL=large-v3

      # Inference engine: faster_whisper (recommended) or openai_whisper
      - ASR_ENGINE=faster_whisper

      # Default language (can be overridden per-request via ?language= param)
      - ASR_MODEL_PATH=/data/whisper-models
    volumes:
      # Persist downloaded models across container restarts
      - whisper-models:/data/whisper-models
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

volumes:
  whisper-models:
```

### 13.4 Starting the Remote Whisper Server

```bash
# On the GPU host:
cd /path/to/whisper-compose
docker compose up -d

# Watch logs (model downloads on first start — may take a few minutes)
docker compose logs -f whisper-asr

# Verify it's running
curl http://localhost:9000/docs
```

The first startup downloads the Whisper model (~3 GB for `large-v3`). This is
cached in the `whisper-models` volume and persists across restarts.

### 13.5 Verifying the `/asr` Endpoint

Test with a sample audio file:

```bash
# Quick test with a WAV file
curl -X POST "http://localhost:9000/asr?encode=true&task=transcribe&language=sv&output=json" \
  -H "Content-Type: multipart/form-data" \
  -F "audio_file=@test_recording.wav"

# Expected response:
# {"text": "Trafikmeddelande från P4 Stockholm..."}
```

The server also exposes interactive API docs at `http://<gpu-host>:9000/docs`
(Swagger UI) for testing directly in the browser.

### 13.6 Configuring RDS Guard to Use the Remote Server

On the RDS Guard host, add these to your `.env` file:

```bash
# Use remote transcription instead of the default local CPU Whisper
TRANSCRIPTION_ENGINE=remote
WHISPER_REMOTE_URL=http://192.168.1.50:9000
```

Replace `192.168.1.50` with the IP or hostname of your GPU host. If both
containers are on the same Docker network, use the service name instead
(e.g., `http://whisper-asr:9000`).

### 13.7 Model Selection for Remote Server

The model is configured on the **remote server** via the `ASR_MODEL`
environment variable, not on the RDS Guard side.

| ASR_MODEL | VRAM Required | Accuracy | Best For |
|-----------|---------------|----------|----------|
| `tiny` | ~1 GB | Fair | Testing, low-end GPUs |
| `base` | ~1 GB | Good | Quick results, older GPUs |
| `small` | ~2 GB | Very good | Good balance |
| `medium` | ~5 GB | Excellent | Most use cases |
| `large-v3` | ~10 GB | Near-perfect | Best Swedish quality (recommended) |

For Swedish traffic announcements, `large-v3` is recommended — it handles
Swedish names, road numbers, and traffic terminology with high accuracy.

### 13.8 Network & Security Considerations

- The `/asr` endpoint has **no authentication** by default. Only expose it
  on a trusted network (LAN) or behind a reverse proxy with auth.
- Use a firewall to restrict access to port 9000 to the RDS Guard host only:
  ```bash
  # On the GPU host (iptables example)
  iptables -A INPUT -p tcp --dport 9000 -s 192.168.1.100 -j ACCEPT
  iptables -A INPUT -p tcp --dport 9000 -j DROP
  ```
- For cross-network deployments, consider a VPN or SSH tunnel.
- Audio data is sent unencrypted over HTTP. For sensitive deployments, use
  HTTPS via a reverse proxy (e.g., Caddy, nginx).

### 13.9 Running Both Services on the Same Docker Host

If the GPU host also runs RDS Guard (e.g., a desktop with both an RTL-SDR
dongle and an NVIDIA GPU), both services can share a Docker network:

```yaml
# docker-compose.yml — Combined RDS Guard + Whisper ASR

services:
  rds-guard:
    image: ghcr.io/tubalainen/rds-guard:latest
    container_name: rds-guard
    restart: unless-stopped
    env_file: .env
    ports:
      - "${WEB_UI_PORT:-8022}:8022"
    volumes:
      - ./rds-data:/data
    devices:
      - /dev/bus/usb:/dev/bus/usb
    depends_on:
      - whisper-asr

  whisper-asr:
    image: onerahmet/openai-whisper-asr-webservice:latest-gpu
    container_name: whisper-asr
    restart: unless-stopped
    environment:
      - ASR_MODEL=large-v3
      - ASR_ENGINE=faster_whisper
      - ASR_MODEL_PATH=/data/whisper-models
    volumes:
      - whisper-models:/data/whisper-models
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]

volumes:
  whisper-models:
```

In this case, use the Docker service name as the URL in `.env`:

```bash
WHISPER_REMOTE_URL=http://whisper-asr:9000
```

### 13.10 CPU-Only Remote Server (No GPU)

If no NVIDIA GPU is available but you still want to offload transcription to a
more powerful x86 machine (e.g., a NAS or server), use the CPU image:

```yaml
services:
  whisper-asr:
    image: onerahmet/openai-whisper-asr-webservice:latest
    # Note: 'latest' instead of 'latest-gpu'
    container_name: whisper-asr
    restart: unless-stopped
    ports:
      - "9000:9000"
    environment:
      - ASR_MODEL=small
      # Use a smaller model for CPU — large-v3 is too slow without GPU
      - ASR_ENGINE=faster_whisper
      - ASR_MODEL_PATH=/data/whisper-models
    volumes:
      - whisper-models:/data/whisper-models

volumes:
  whisper-models:
```

This is useful for offloading from a Raspberry Pi to a more capable x86 NAS,
even without GPU acceleration.

---

## 14. Example `.env` Configurations

### 14.1 Zero-config (everything works out of the box)

```bash
# .env — Minimal setup. Recording, transcription (local CPU), and
# web UI all work with zero additional configuration.
FM_FREQUENCY=103.3M
RTL_GAIN=8

# That's it. Audio recording, local Whisper transcription, and the
# web UI (with playback + transcription text) are all on by default.
# MQTT is off since MQTT_ENABLED defaults to false.
```

### 14.2 With MQTT (typical Home Assistant setup)

```bash
# .env — Add MQTT to get alerts with transcription text.
# Alerts always include transcription — no separate toggle needed.
FM_FREQUENCY=103.3M
RTL_GAIN=8

MQTT_ENABLED=true
MQTT_HOST=192.168.1.100
PUBLISH_MODE=essential
```

### 14.3 Remote GPU transcription (recommended for Raspberry Pi)

```bash
# .env — Offload transcription to a GPU server on the LAN.
# The only additional config needed beyond the basics.
FM_FREQUENCY=103.3M
RTL_GAIN=8

MQTT_ENABLED=true
MQTT_HOST=192.168.1.100
PUBLISH_MODE=essential

# Point transcription at the remote Whisper ASR server
TRANSCRIPTION_ENGINE=remote
WHISPER_REMOTE_URL=http://192.168.1.50:9000
```

### 14.4 Disable transcription (audio-only)

```bash
# .env — Record and play back audio, but don't transcribe.
# Useful if no CPU/GPU budget for Whisper, or for testing.
FM_FREQUENCY=103.3M
RTL_GAIN=8

TRANSCRIPTION_ENGINE=none
# Audio is still recorded and playable in the web UI.
# MQTT alerts will have audio_available=true but no transcription text.
```
