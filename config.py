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
