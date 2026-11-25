"""Aggregator configuration from environment variables."""

import os


def _bool(val):
    return str(val).lower() in ("1", "true", "yes", "on")


def _int(val, default):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


BUILD_VERSION = os.environ.get("BUILD_VERSION", "dev")

# --- Network ---
# Ingest API for satellites. Not published on the host: only nginx reaches it.
INGEST_PORT = _int(os.environ.get("INGEST_PORT"), 8081)
# Web UI + read API + node admin. LAN only — never expose it to the internet.
WEB_PORT = _int(os.environ.get("WEB_PORT"), 8080)
# Public address satellites use (shown in the setup snippet for new nodes)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").strip().rstrip("/")

# --- Storage ---
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "aggregator.db"))
AUDIO_DIR = os.environ.get("AUDIO_DIR", os.path.join(DATA_DIR, "audio"))
EVENT_RETENTION_DAYS = _int(os.environ.get("EVENT_RETENTION_DAYS"), 90)

# --- Ingest limits ---
MAX_JSON_BYTES = 64 * 1024
MAX_AUDIO_BYTES = 8 * 1024 * 1024
# Per-node request budget (token bucket): sustained rate and burst
INGEST_RATE_PER_MIN = max(1, _int(os.environ.get("INGEST_RATE_PER_MIN"), 120))
INGEST_BURST = max(1, _int(os.environ.get("INGEST_BURST"), 60))

# A node counts as offline when no heartbeat arrived for this long
NODE_OFFLINE_SEC = max(30, _int(os.environ.get("NODE_OFFLINE_SEC"), 120))

# --- Alerts ---
# After an event has ended, wait this long for its audio before alerting
ALERT_AUDIO_WAIT_SEC = _int(os.environ.get("ALERT_AUDIO_WAIT_SEC"), 90)
# After the audio has arrived, wait this long for the transcription
ALERT_TRANSCRIPTION_WAIT_SEC = _int(
    os.environ.get("ALERT_TRANSCRIPTION_WAIT_SEC"), 300)
# Events that ended longer ago than this (e.g. delivered late after a
# satellite outage) are stored but not alerted on MQTT
ALERT_MAX_DELAY_MIN = _int(os.environ.get("ALERT_MAX_DELAY_MIN"), 60)

# --- MQTT (optional) ---
MQTT_ENABLED = _bool(os.environ.get("MQTT_ENABLED", "false"))
MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = _int(os.environ.get("MQTT_PORT"), 1883)
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")
MQTT_TOPIC_PREFIX = os.environ.get("MQTT_TOPIC_PREFIX", "rds-aggregator")
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "rds-guard-aggregator")

# --- Transcription (same settings as the satellite) ---
TRANSCRIPTION_ENGINE = os.environ.get("TRANSCRIPTION_ENGINE", "local")
TRANSCRIPTION_LANGUAGE = os.environ.get("TRANSCRIPTION_LANGUAGE", "sv")
TRANSCRIPTION_MODEL = os.environ.get("TRANSCRIPTION_MODEL", "small")
TRANSCRIPTION_DEVICE = os.environ.get("TRANSCRIPTION_DEVICE", "cpu")
WHISPER_REMOTE_URL = os.environ.get("WHISPER_REMOTE_URL", "")
WHISPER_REMOTE_TIMEOUT = _int(os.environ.get("WHISPER_REMOTE_TIMEOUT"), 120)
