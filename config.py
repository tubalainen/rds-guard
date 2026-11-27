"""Configuration from environment variables with defaults."""

import os
import sys


def _bool(val):
    return str(val).lower() in ("1", "true", "yes", "on")


def _int(val, default):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _parse_freq_hz(freq_str: str) -> int:
    """Convert a frequency string like '103.5M' or '103500000' to Hz (int)."""
    s = freq_str.strip().upper()
    if s.endswith("M"):
        return int(float(s[:-1]) * 1_000_000)
    if s.endswith("K"):
        return int(float(s[:-1]) * 1_000)
    return int(float(s))


# Build version (injected at Docker build time, defaults to "dev")
BUILD_VERSION = os.environ.get("BUILD_VERSION", "dev")

# RTL-SDR
FM_FREQUENCY = os.environ.get("FM_FREQUENCY", "103.5M")
RTL_GAIN = os.environ.get("RTL_GAIN", "8")
PPM_CORRECTION = os.environ.get("PPM_CORRECTION", "0")
RTL_DEVICE_SERIAL = os.environ.get("RTL_DEVICE_SERIAL", "")
RTL_DEVICE_INDEX = os.environ.get("RTL_DEVICE_INDEX", "0")

# Multi-station support
# FM_FREQUENCIES: comma-separated list of up to 4 frequencies, e.g. "103.5M,102.9M"
# If unset, falls back to single-station FM_FREQUENCY.
_freqs_raw = os.environ.get("FM_FREQUENCIES", "").strip()
if _freqs_raw:
    STATION_FREQS = [f.strip() for f in _freqs_raw.split(",") if f.strip()]
else:
    STATION_FREQS = [FM_FREQUENCY]

# Validate: max 4 stations
if len(STATION_FREQS) > 4:
    print(
        f"ERROR: FM_FREQUENCIES contains {len(STATION_FREQS)} frequencies — maximum is 4. "
        "Aborting.",
        file=sys.stderr,
    )
    sys.exit(1)

# Validate: all frequencies must fit within 2.0 MHz of each other
if len(STATION_FREQS) > 1:
    _freq_hz_list = [_parse_freq_hz(f) for f in STATION_FREQS]
    _span = max(_freq_hz_list) - min(_freq_hz_list)
    if _span > 2_000_000:
        print(
            f"ERROR: FM_FREQUENCIES span {_span/1e6:.2f} MHz exceeds the 2.0 MHz "
            "usable bandwidth limit. All frequencies must be within 2 MHz of each "
            "other. Aborting.",
            file=sys.stderr,
        )
        sys.exit(1)

# True when 2 or more stations are configured — activates wideband IQ path
MULTI_STATION: bool = len(STATION_FREQS) > 1

# RTL-SDR sample rate and centre frequency for multi-station wideband capture
# 2 394 000 = 171 000 × 14 (exact integer decimation ratio)
RTL_SAMPLE_RATE = 2_394_000
# Centre frequency: override via env or auto-computed as midpoint of all freqs
_rtl_center_raw = os.environ.get("RTL_CENTER_FREQ", "").strip()
if _rtl_center_raw:
    RTL_CENTER_FREQ_HZ: int = _parse_freq_hz(_rtl_center_raw)
elif MULTI_STATION:
    _freq_hz_list = [_parse_freq_hz(f) for f in STATION_FREQS]
    RTL_CENTER_FREQ_HZ = (_min := min(_freq_hz_list)) + (max(_freq_hz_list) - _min) // 2
else:
    RTL_CENTER_FREQ_HZ = _parse_freq_hz(FM_FREQUENCY)

# Redsea
REDSEA_SHOW_PARTIAL = _bool(os.environ.get("REDSEA_SHOW_PARTIAL", "true"))
REDSEA_SHOW_RAW = _bool(os.environ.get("REDSEA_SHOW_RAW", "false"))

# MQTT
MQTT_ENABLED = _bool(os.environ.get("MQTT_ENABLED", "false"))
MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = _int(os.environ.get("MQTT_PORT"), 1883)
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "rds")
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "rds-guard")
MQTT_QOS = _int(os.environ.get("MQTT_QOS"), 1)
MQTT_RETAIN_STATE = _bool(os.environ.get("MQTT_RETAIN_STATE", "true"))

# Publishing control
#   "essential" = alert topic carries only traffic announcements and emergencies
#                 (EON and other decoded RDS data are excluded from the alert topic)
#   "all"       = every decoded RDS field gets its own topic, alert topic includes EON
PUBLISH_MODE = os.environ.get("PUBLISH_MODE", "essential").lower()
PUBLISH_RAW = _bool(os.environ.get("PUBLISH_RAW", "false"))
STATUS_INTERVAL = _int(os.environ.get("STATUS_INTERVAL"), 30)

# Web UI
WEB_UI_PORT = _int(os.environ.get("WEB_UI_PORT"), 8022)
EVENT_RETENTION_DAYS = _int(os.environ.get("EVENT_RETENTION_DAYS"), 30)
# Address the web UI is reached on, e.g. https://rds.example.se. Optional:
# when set, push messages carry a link to the event and its audio.
WEB_UI_URL = os.environ.get("WEB_UI_URL", "").strip().rstrip("/")

# Push notification rules (edited in the web UI) check days and times in
# this time zone.
NOTIFY_TIMEZONE = os.environ.get("NOTIFY_TIMEZONE", "Europe/Stockholm")

# Password for the admin pages in the web UI (push rules). Empty = admin off.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

# --- Audio Recording (always on) ---
AUDIO_DIR = os.environ.get("AUDIO_DIR", "/data/audio")
RECORD_EVENT_TYPES = os.environ.get("RECORD_EVENT_TYPES", "traffic,emergency")
AUDIO_FORMAT = os.environ.get("AUDIO_FORMAT", "ogg")
MAX_RECORDING_SEC = _int(os.environ.get("MAX_RECORDING_SEC"), 600)

# --- Transcription ---
# "elevenlabs" = ElevenLabs Scribe API (default) with local faster-whisper
#                as fallback; without ELEVENLABS_API_KEY it runs as "local"
# "local"  = built-in faster-whisper on CPU (works out of the box)
# "remote" = external Whisper ASR server via HTTP /asr endpoint
# "none"   = disable transcription (audio is still recorded and playable)
TRANSCRIPTION_ENGINE = os.environ.get("TRANSCRIPTION_ENGINE", "elevenlabs")
TRANSCRIPTION_LANGUAGE = os.environ.get("TRANSCRIPTION_LANGUAGE", "sv")

# Local engine settings (only used when TRANSCRIPTION_ENGINE=local)
# Empty = automatic: for Swedish the KB-Whisper small "strict" model bundled
# in the Docker image (else KBLab/kb-whisper-small from the hub), else "small".
# Any faster-whisper size (tiny … large-v3, large-v3-turbo) or a Hugging Face
# CTranslate2 repo id (e.g. KBLab/kb-whisper-medium) works.
TRANSCRIPTION_MODEL = os.environ.get("TRANSCRIPTION_MODEL", "").strip()
TRANSCRIPTION_DEVICE = os.environ.get("TRANSCRIPTION_DEVICE", "cpu")
# Optional vocabulary hint (place names, road numbers) given to Whisper
TRANSCRIPTION_PROMPT = os.environ.get("TRANSCRIPTION_PROMPT", "").strip()
TRANSCRIPTION_BEAM_SIZE = _int(os.environ.get("TRANSCRIPTION_BEAM_SIZE"), 5)
# 0 = let CTranslate2 decide
TRANSCRIPTION_CPU_THREADS = _int(os.environ.get("TRANSCRIPTION_CPU_THREADS"), 0)

# Remote engine settings (only used when TRANSCRIPTION_ENGINE=remote)
WHISPER_REMOTE_URL = os.environ.get("WHISPER_REMOTE_URL", "")
WHISPER_REMOTE_TIMEOUT = _int(os.environ.get("WHISPER_REMOTE_TIMEOUT"), 120)

# ElevenLabs Scribe (only used when TRANSCRIPTION_ENGINE=elevenlabs)
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_MODEL = os.environ.get("ELEVENLABS_MODEL", "scribe_v3").strip()
# Comma-separated words the model should listen for (road numbers, places)
ELEVENLABS_KEYTERMS = os.environ.get("ELEVENLABS_KEYTERMS", "")
ELEVENLABS_TIMEOUT = _int(os.environ.get("ELEVENLABS_TIMEOUT"), 60)
# What to do when the API fails: "local" = transcribe with faster-whisper
# instead (model loaded on first use), "none" = mark the clip as failed
TRANSCRIPTION_FALLBACK = os.environ.get(
    "TRANSCRIPTION_FALLBACK", "local").strip().lower()

# --- Uplink (satellite mode, optional) ---
# Set UPLINK_URL + UPLINK_TOKEN to forward events, their audio and a periodic
# status heartbeat to an RDS Guard aggregator.  Leave UPLINK_URL empty to run
# standalone (the default).  Everything keeps working locally either way.
UPLINK_URL = os.environ.get("UPLINK_URL", "").strip().rstrip("/")
UPLINK_TOKEN = os.environ.get("UPLINK_TOKEN", "").strip()
# Free-text name shown on the aggregator next to this node's reports
NODE_NAME = os.environ.get("NODE_NAME", "").strip()
UPLINK_HEARTBEAT_SEC = max(10, _int(os.environ.get("UPLINK_HEARTBEAT_SEC"), 30))
# Queued items older than this are dropped instead of delivered late
UPLINK_MAX_AGE_HOURS = max(1, _int(os.environ.get("UPLINK_MAX_AGE_HOURS"), 72))

# Set by docker-compose.yml when the aggregator runs in the same compose
# project: UPLINK_URL then points at it, and until UPLINK_TOKEN is set this
# installation runs standalone instead of refusing to start.  Once it reports
# there, the aggregator transcribes, maps and publishes MQTT/push, so this
# installation only records (unless LOCAL_PROCESSING / SATELLITE_MQTT_ENABLED
# say otherwise).
LOCAL_AGGREGATOR = _bool(os.environ.get("LOCAL_AGGREGATOR", "false"))
if LOCAL_AGGREGATOR and not UPLINK_TOKEN:
    if UPLINK_URL:
        print("NOTE: the aggregator in this compose project is not used yet: "
              "set UPLINK_TOKEN in .env (docker compose exec aggregator "
              "python3 -m aggregator.nodes add \"<name>\") and run "
              "docker compose up -d. Running standalone until then.",
              file=sys.stderr)
    UPLINK_URL = ""

UPLINK_ENABLED: bool = bool(UPLINK_URL)

# --- Location and map (geolocation of traffic messages) ---
# Where this receiver is: "Ort, Län", e.g. "Örebro, Örebro län". Used to find
# the places a traffic message mentions, and sent to the aggregator.
NODE_LOCATION = os.environ.get("NODE_LOCATION", "").strip()
# Process recordings here (transcription and map)? Only a satellite can turn
# this off: it then records and forwards raw audio, and the aggregator does
# all processing. A standalone installation always processes.
LOCAL_PROCESSING = _bool(os.environ.get(
    "LOCAL_PROCESSING",
    "false" if LOCAL_AGGREGATOR and UPLINK_ENABLED else "true"))
# Hand MQTT (alerts, push) over to the aggregator in the same compose
# project, so nothing is published twice
if LOCAL_AGGREGATOR and UPLINK_ENABLED and MQTT_ENABLED:
    MQTT_ENABLED = _bool(os.environ.get("SATELLITE_MQTT_ENABLED", "false"))
    if not MQTT_ENABLED:
        print("NOTE: MQTT and push come from the aggregator in this compose "
              "project; this satellite does not publish to MQTT "
              "(SATELLITE_MQTT_ENABLED=true turns it on).", file=sys.stderr)
# "claude" = the Claude API reads the transcription, corrects misheard words
#            and finds the places (rules engine as fallback); without
#            ANTHROPIC_API_KEY it runs as "rules"
# "rules"  = local patterns and place-name registry only (free, offline)
# "none"   = no map
GEO_ENGINE = os.environ.get("GEO_ENGINE", "claude").strip().lower()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5").strip()
CLAUDE_TIMEOUT = _int(os.environ.get("CLAUDE_TIMEOUT"), 60)
# Give the transcriber local place names to listen for: ElevenLabs keyterms
# (about 20 % more per clip) and the local Whisper prompt
TRANSCRIPTION_PLACE_NAMES = _bool(
    os.environ.get("TRANSCRIPTION_PLACE_NAMES", "true"))

# --- Trafikverket (optional): exact places and updates for traffic incidents ---
# A free key from Trafikverket's open API (data.trafikverket.se). Empty = off.
TRAFIKVERKET_API_KEY = os.environ.get("TRAFIKVERKET_API_KEY", "").strip()
# Deviations within this many km of the receiver(s) are kept
TRAFIKVERKET_RADIUS_KM = max(10, min(500, _int(
    os.environ.get("TRAFIKVERKET_RADIUS_KM"), 150)))
# Seconds between polls (only changes are fetched)
TRAFIKVERKET_POLL_SEC = max(30, _int(os.environ.get("TRAFIKVERKET_POLL_SEC"), 60))

if not LOCAL_PROCESSING and not UPLINK_ENABLED:
    print("WARNING: LOCAL_PROCESSING=false only applies to a satellite "
          "(UPLINK_URL set); a standalone installation processes everything "
          "itself, so it stays on.", file=sys.stderr)
    LOCAL_PROCESSING = True
if UPLINK_ENABLED:
    if not UPLINK_URL.lower().startswith(("http://", "https://")):
        print(
            f"ERROR: UPLINK_URL must start with http:// or https:// "
            f"(got '{UPLINK_URL}'). Aborting.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not UPLINK_TOKEN:
        print(
            "ERROR: UPLINK_URL is set but UPLINK_TOKEN is empty. Ask the "
            "aggregator's owner for a node token. Aborting.",
            file=sys.stderr,
        )
        sys.exit(1)
