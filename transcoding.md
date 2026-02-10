# Voice Recording & Transcription Plan

## Objective

When a Traffic Announcement (TA), Emergency Broadcast, or similar RDS event
occurs, **record the FM audio**, **transcribe it to text** using speech-to-text,
and make both the transcription and the audio clip available via:

- The SQLite event store (new columns)
- MQTT (if enabled) — transcription text in event payloads
- The web UI — transcription text display + audio playback button

---

## 1. Architecture Overview

### Current Pipeline

```
RTL-SDR → rtl_fm (stdout=PCM @ 171 kHz) → redsea (stdin) → JSON → Python
```

`rtl_fm -M fm` outputs **demodulated FM baseband audio** as raw signed-16-bit
little-endian PCM at 171 kHz. The `redsea` decoder reads this stream to extract
RDS data from the 57 kHz subcarrier. The audible voice content (0–15 kHz) is
present in the same stream but is currently discarded after RDS extraction.

### Proposed Pipeline

```
RTL-SDR → rtl_fm (stdout=PCM @ 171 kHz)
                    │
                    ▼
            ┌── AudioTee (Python) ──┐
            │                       │
            ▼                       ▼
     redsea (stdin)         AudioRecorder
     JSON → Python          (conditional)
            │                       │
            ▼                       ▼
     RulesEngine ──trigger──▶ start/stop
                                    │
                                    ▼
                            PCM buffer → WAV file
                                    │
                                    ▼
                            Transcriber (async)
                                    │
                            ┌───────┴───────┐
                            ▼               ▼
                      event_store        MQTT pub
                      (update row)    (transcription)
                            │
                            ▼
                         Web UI
                    (text + playback)
```

**Key change**: Instead of connecting `rtl_fm.stdout` directly to
`redsea.stdin`, Python sits in the middle as a **tee**. Every chunk read from
`rtl_fm` is forwarded to `redsea` AND, when a recordable event is active,
simultaneously written to an audio buffer.

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

Converts recorded audio to text using a speech-to-text engine.

**Primary choice: `faster-whisper`**

Rationale:
- Uses CTranslate2 — 4x faster than original Whisper, lower memory
- Runs on CPU (no GPU needed — suitable for Raspberry Pi / NUC)
- Excellent Swedish language support (Whisper was trained on Swedish data)
- Quantized models (int8) reduce memory to ~1 GB for `small` model
- Apache 2.0 license

**Alternative options** (configurable):

| Engine | Pros | Cons |
|--------|------|------|
| `faster-whisper` (default) | Fast, accurate, local, Swedish | ~1 GB RAM for `small` model |
| `whisper.cpp` (via subprocess) | Very lightweight, C++ | Requires separate binary build |
| `vosk` | Very lightweight (~50 MB) | Less accurate for Swedish |
| External API (webhook) | Offload to cloud/other service | Network dependency, privacy |

**Design:**

```python
class Transcriber:
    """Speech-to-text engine with async job queue."""

    def __init__(self, model_size="small", language="sv", device="cpu"):
        self._model_size = model_size
        self._language = language
        self._device = device
        self._queue = queue.Queue()
        self._model = None  # lazy-loaded on first use

    def _load_model(self):
        """Load the Whisper model (one-time, ~10-30s on first call)."""
        from faster_whisper import WhisperModel
        self._model = WhisperModel(
            self._model_size,
            device=self._device,
            compute_type="int8",  # quantized for CPU
        )

    def enqueue(self, audio_path, event_id, callback):
        """Add a transcription job to the queue."""
        self._queue.put((audio_path, event_id, callback))

    def run(self):
        """Worker loop — runs in a dedicated thread."""
        while True:
            audio_path, event_id, callback = self._queue.get()
            try:
                if self._model is None:
                    self._load_model()
                text = self._transcribe(audio_path)
                callback(event_id, text, None)
            except Exception as e:
                callback(event_id, None, e)

    def _transcribe(self, audio_path):
        """Run transcription on a single file."""
        segments, info = self._model.transcribe(
            str(audio_path),
            language=self._language,
            beam_size=5,
            vad_filter=True,       # skip silence
            vad_parameters=dict(
                min_silence_duration_ms=500,
            ),
        )
        # Combine all segments into a single string
        texts = [seg.text.strip() for seg in segments if seg.text.strip()]
        return " ".join(texts)
```

**Model selection guidance** (for `TRANSCRIPTION_MODEL` config):

| Model | Size | RAM | Accuracy | Speed (Pi 4) | Speed (x86) |
|-------|------|-----|----------|---------------|--------------|
| `tiny` | 75 MB | ~400 MB | Fair | ~2x real-time | ~10x |
| `base` | 150 MB | ~500 MB | Good | ~1x real-time | ~6x |
| `small` | 500 MB | ~1 GB | Very good | ~0.3x | ~3x |
| `medium` | 1.5 GB | ~2.5 GB | Excellent | Too slow | ~1x |

Recommendation: `small` for x86/NUC, `base` or `tiny` for Raspberry Pi 4.

### 2.4 Configuration — `config.py` additions

```python
# --- Voice Recording & Transcription ---
RECORDING_ENABLED = _bool(os.environ.get("RECORDING_ENABLED", "false"))
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")

# Which event types trigger recording
# Comma-separated: traffic,emergency,eon_traffic,tmc
RECORD_EVENT_TYPES = os.environ.get("RECORD_EVENT_TYPES", "traffic,emergency")

# Transcription engine: "faster-whisper", "whisper-cpp", "vosk", "none"
TRANSCRIPTION_ENGINE = os.environ.get("TRANSCRIPTION_ENGINE", "faster-whisper")
TRANSCRIPTION_MODEL = os.environ.get("TRANSCRIPTION_MODEL", "small")
TRANSCRIPTION_LANGUAGE = os.environ.get("TRANSCRIPTION_LANGUAGE", "sv")

# "cpu" or "cuda" (if GPU available)
TRANSCRIPTION_DEVICE = os.environ.get("TRANSCRIPTION_DEVICE", "cpu")

# Web playback format: "ogg", "wav", "mp3"
AUDIO_FORMAT = os.environ.get("AUDIO_FORMAT", "ogg")

# Max recording duration in seconds (safety cap)
MAX_RECORDING_SEC = _int(os.environ.get("MAX_RECORDING_SEC"), 600)

# Publish transcription to MQTT
MQTT_PUBLISH_TRANSCRIPTION = _bool(os.environ.get("MQTT_PUBLISH_TRANSCRIPTION", "true"))
```

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

When transcription completes, publish an updated event payload that includes
the transcription text:

**New topic (essential mode):**
```
rds/alert                          ← existing; now includes transcription field
rds/{pi}/traffic/transcription     ← new; dedicated transcription topic
```

**Updated alert payload structure** (traffic end event):
```json
{
  "type": "traffic",
  "state": "end",
  "station": { "pi": "0x9E04", "ps": "P4 Stockholm" },
  "frequency": "103.3M",
  "started": "2024-02-10T15:30:00",
  "ended": "2024-02-10T15:32:15",
  "duration_sec": 135,
  "radiotext": ["Olycka E4 norrgående vid Rotebro", "Två fält blockerade"],
  "transcription": "Trafikmeddelande från P4 Stockholm. Det har inträffat en olycka på E4 norrgående vid Rotebro. Två av tre fält är blockerade. Räkna med längre restid.",
  "audio_available": true,
  "timestamp": "2024-02-10T15:32:15"
}
```

**Separate transcription event** (published when transcription finishes, which
may be after the TA end event):
```
rds/{pi}/traffic/transcription
```
```json
{
  "event_id": 42,
  "transcription": "Trafikmeddelande från P4 Stockholm...",
  "language": "sv",
  "duration_sec": 135,
  "timestamp": "2024-02-10T15:32:45"
}
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
1. Add new config variables to `config.py`
2. Add `audio_path`, `transcription`, `transcription_status` columns to
   `event_store.py` with migration
3. Add `update_event_audio()`, `update_event_transcription()`,
   `update_event_transcription_status()` functions
4. Create `audio_tee.py` with the `AudioTee` class
5. Create `audio_recorder.py` with the `AudioRecorder` class (PCM buffering,
   WAV writing, downsampling)
6. Modify `pipeline.py` to use `AudioTee` when `RECORDING_ENABLED=true`
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
- `transcriber.py` — STT engine abstraction + worker thread

**Tasks:**
1. Implement `Transcriber` class with `faster-whisper` backend
2. Add lazy model loading (first transcription triggers download/load)
3. Implement job queue with callback mechanism
4. Wire transcription completion to `event_store.update_event_transcription()`
5. Wire transcription completion to MQTT publish
6. Wire transcription completion to WebSocket broadcast
7. Start transcriber worker thread in `main()`

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
2. Add Python deps: `faster-whisper`, `numpy` (for resampling)
3. Create `/data/audio` directory in entrypoint
4. Update `.env.example` with new configuration options
5. Test builds on both x86_64 and arm64 (Pi)

---

## 5. Dependencies

### New Python Packages

```
faster-whisper>=1.0.0      # STT engine (includes CTranslate2)
numpy>=1.24.0              # Audio resampling
```

`numpy` is already a transitive dependency of `faster-whisper`, but listing it
explicitly for the resampling use case.

### New System Packages (Dockerfile)

```
ffmpeg          # Audio encoding (PCM → OGG/Opus)
```

`ffmpeg` is used via subprocess for encoding — no Python binding needed. This
keeps the approach simple and avoids complex native library builds.

### Optional (for alternative engines)

```
# whisper.cpp — if chosen instead of faster-whisper
# Built from source in Dockerfile, similar to redsea

# vosk — if chosen for lightweight deployment
vosk>=0.3.45
```

---

## 6. Configuration Reference

| Variable | Default | Description |
|----------|---------|-------------|
| `RECORDING_ENABLED` | `false` | Enable voice recording during events |
| `AUDIO_DIR` | `/data/audio` | Directory for audio files |
| `RECORD_EVENT_TYPES` | `traffic,emergency` | Event types that trigger recording |
| `TRANSCRIPTION_ENGINE` | `faster-whisper` | STT engine (`faster-whisper`, `whisper-cpp`, `vosk`, `none`) |
| `TRANSCRIPTION_MODEL` | `small` | Whisper model size (`tiny`, `base`, `small`, `medium`) |
| `TRANSCRIPTION_LANGUAGE` | `sv` | Language hint for transcription |
| `TRANSCRIPTION_DEVICE` | `cpu` | Compute device (`cpu` or `cuda`) |
| `AUDIO_FORMAT` | `ogg` | Web playback format (`ogg`, `wav`, `mp3`) |
| `MAX_RECORDING_SEC` | `600` | Maximum recording duration (seconds) |
| `MQTT_PUBLISH_TRANSCRIPTION` | `true` | Include transcription in MQTT payloads |

---

## 7. Data Flow Diagram — Complete Event Lifecycle

```
T+0s    TA flag → true
        ├─ INSERT event (state='start')                      → event_id=42
        ├─ recorder.start(event_id=42)
        │    └─ AudioRecorder begins buffering PCM chunks
        ├─ event_store.update_transcription_status(42, "recording")
        ├─ MQTT: rds/alert  {type:traffic, state:start, recording:true}
        └─ WS broadcast: {recording: true}

T+5s    RadioText received: "Olycka E4 norrgående"
        ├─ UPDATE event 42 (radiotext += [...])
        ├─ MQTT: rds/alert  {type:traffic, state:update}
        └─ WS broadcast

T+135s  TA flag → false
        ├─ recorder.stop()
        │    ├─ Raw PCM: 135s × 171000 Hz × 2 bytes = ~46 MB
        │    ├─ Downsample: 171 kHz → 16 kHz  (~4.3 MB)
        │    ├─ Write: /data/audio/42.wav  (16 kHz, ~4.3 MB)
        │    ├─ Encode: /data/audio/42.ogg  (Opus, ~200 KB)
        │    ├─ event_store.update_event_audio(42, "42.ogg")
        │    ├─ event_store.update_transcription_status(42, "transcribing")
        │    └─ transcriber.enqueue(42.wav, callback)
        ├─ UPDATE event 42 (state='end', duration=135)
        ├─ MQTT: rds/alert  {type:traffic, state:end, audio_available:true}
        └─ WS broadcast

T+145s  Transcription complete (~10s processing for 135s audio)
        ├─ event_store.update_event_transcription(42, "Trafikmeddelande...")
        ├─ MQTT: rds/{pi}/traffic/transcription  {event_id:42, text:...}
        ├─ MQTT: rds/alert  {type:traffic, state:transcribed, text:...}
        └─ WS broadcast: {event_id:42, transcription:"..."}
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
| Transcription model fails to load | Status set to "error"; event still has audio |
| Disk full | Audio write fails; logged as error; event unaffected |
| `RECORDING_ENABLED=false` | Entire feature disabled; no overhead |
| Multiple simultaneous TAs (different PIs) | Each gets its own recorder buffer (keyed by PI) |
| App restart during recording | Stale recordings cleaned up on startup |
| Audio file deleted externally | API returns 404; UI shows "Audio unavailable" |
| Whisper model not downloaded yet | Auto-downloads on first use (one-time) |

---

## 10. Performance Considerations

### Memory Impact

- **AudioTee overhead (not recording):** ~zero (one boolean check per chunk)
- **AudioTee overhead (recording):** raw PCM buffer grows at ~342 KB/s
  (171 kHz × 2 bytes). A 2-minute TA ≈ 41 MB in memory.
- **Whisper model resident:** ~1 GB for `small`, ~500 MB for `base`
- **Mitigation:** Lazy-load model only on first transcription; unload after
  idle timeout (configurable)

### CPU Impact

- **AudioTee:** Negligible — memcpy-level operations
- **Resampling (171→16 kHz):** Brief spike (~1-2s for 2-min recording)
- **FFmpeg encoding:** Brief spike (~1-2s)
- **Whisper transcription:** Significant — ~10s per minute of audio on x86,
  ~60s per minute on Pi 4. Runs in dedicated thread, doesn't block pipeline.

### Disk Impact

- **Per event:** ~200 KB OGG + ~4.3 MB WAV per minute of audio
- **OGG-only mode:** ~200 KB per minute
- **30-day retention at 5 events/day:** ~30 MB (OGG only) to ~650 MB (WAV+OGG)
- **Mitigation:** Configurable retention; WAV can be optional (OGG sufficient
  for playback; Whisper can read OGG directly)

---

## 11. Testing Strategy

### Unit Tests

- `test_audio_tee.py` — Verify chunks forwarded correctly; recording toggle
- `test_audio_recorder.py` — Start/stop lifecycle; WAV output; duration limits
- `test_transcriber.py` — Mock STT model; queue processing; error handling
- `test_event_store.py` — Schema migration; new columns; audio/transcription updates

### Integration Tests

- End-to-end: simulated TA flag sequence → audio file + transcription in DB
- MQTT payload verification with transcription fields
- Web API: audio file serving with correct headers
- Web UI: manual testing of player and transcription display

### Performance Tests

- AudioTee throughput: verify no dropped samples under load
- Transcription latency benchmarks per model size
- Memory profiling during long recordings

---

## 12. Rollout Plan

1. **Feature flag**: `RECORDING_ENABLED=false` by default — zero impact on
   existing users
2. **Incremental deployment**: Audio recording can work without transcription
   (`TRANSCRIPTION_ENGINE=none`)
3. **Model download**: First transcription triggers model download;
   alternatively, pre-download during `docker build`
4. **Documentation**: Update README with new config options, hardware
   recommendations, and example `.env` additions
