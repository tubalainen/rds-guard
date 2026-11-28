"""The satellite's settings, from environment variables with defaults.

The satellite only receives, records and reports: radio (receivers.yml),
recording and the aggregator it reports to. Everything else
(transcription, map, push, MQTT, web UI) is the aggregator's.

Nothing here exits: main() calls problems() and refuses to start on them.
"""

import os


def _drop_env_comments():
    """Docker Compose reads `KEY=        # a comment` in .env as the value
    "# a comment" (only a comment after a value, `KEY=x  # …`, is cut off).
    Older .env.example files had such lines, so NODE_NAME, TRANSCRIPTION_MODEL
    and others got the comment as their value. A value that is only a
    comment ("#" then a space) is taken as empty. Returns the keys."""
    import re
    found = []
    for key, value in list(os.environ.items()):
        if re.match(r"\s*#(\s|$)", value):
            os.environ[key] = ""
            found.append(key)
    return sorted(found)


# Before anything reads the environment
ENV_COMMENTS_DROPPED = _drop_env_comments()


def _bool(val):
    return str(val).lower() in ("1", "true", "yes", "on")


def _int(val, default):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _parse_freq_hz(freq_str: str) -> int:
    """'103.5M', '103500k' or '103500000' -> Hz."""
    s = str(freq_str).strip().upper()
    if s.endswith("M"):
        return int(round(float(s[:-1]) * 1_000_000))
    if s.endswith("K"):
        return int(round(float(s[:-1]) * 1_000))
    return int(round(float(s)))


BUILD_VERSION = os.environ.get("BUILD_VERSION", "dev")

# --- Files ---
DATA_DIR = os.environ.get("DATA_DIR", "/data")
DB_PATH = os.environ.get("DB_PATH", os.path.join(DATA_DIR, "satellite.db"))
# Recordings waiting for (or recently delivered to) the aggregator
SPOOL_DIR = os.environ.get("SPOOL_DIR", os.path.join(DATA_DIR, "spool"))
HEALTH_FILE = os.environ.get("HEALTH_FILE", os.path.join(DATA_DIR, "health.json"))
# The receivers: one entry per RTL-SDR, by serial number
RECEIVERS_FILE = os.environ.get("RECEIVERS_FILE", os.path.join(DATA_DIR, "receivers.yml"))
# Before v1.0: the satellite's database and recordings (left for the import)
OLD_DB_PATH = os.path.join(DATA_DIR, "events.db")
OLD_AUDIO_DIR = os.environ.get("AUDIO_DIR", os.path.join(DATA_DIR, "audio"))

# --- Radio: only in receivers.yml ---
# Before v1.1 one dongle could be set here. Only read to move them into
# receivers.yml once, when that file is not there yet (receivers.load)
FM_FREQUENCY = os.environ.get("FM_FREQUENCY", "").strip()
FM_FREQUENCIES = [f.strip() for f in os.environ.get("FM_FREQUENCIES", "").split(",")
                  if f.strip()]
RTL_DEVICE_SERIAL = os.environ.get("RTL_DEVICE_SERIAL", "").strip()

# The wideband capture: 2 394 000 = 171 000 x 14 (exact decimation)
RTL_SAMPLE_RATE = 2_394_000

REDSEA_SHOW_PARTIAL = _bool(os.environ.get("REDSEA_SHOW_PARTIAL", "true"))

# --- Recording ---
RECORD_EVENT_TYPES = os.environ.get("RECORD_EVENT_TYPES", "traffic,emergency")
MAX_RECORDING_SEC = min(1200, max(30, _int(os.environ.get("MAX_RECORDING_SEC"), 600)))
# Recordings the aggregator acknowledged are kept this long, then removed
SATELLITE_KEEP_DAYS = max(1, _int(os.environ.get("SATELLITE_KEEP_DAYS"), 7))

# --- The aggregator ---
UPLINK_URL = (os.environ.get("AGGREGATOR_URL", "").strip()
              or os.environ.get("UPLINK_URL", "").strip()).rstrip("/")
# Optional: without a token the satellite makes its own and asks to join; the
# aggregator's owner approves it (or the same JOIN_KEY approves it at once)
UPLINK_TOKEN = os.environ.get("UPLINK_TOKEN", "").strip()
JOIN_KEY = os.environ.get("JOIN_KEY", "").strip()
NODE_NAME = os.environ.get("NODE_NAME", "").strip()
# "Ort, Län", suggested to the aggregator for the map
NODE_LOCATION = os.environ.get("NODE_LOCATION", "").strip()
UPLINK_HEARTBEAT_SEC = max(10, _int(os.environ.get("UPLINK_HEARTBEAT_SEC"), 10))
UPLINK_MAX_AGE_HOURS = max(1, _int(os.environ.get("UPLINK_MAX_AGE_HOURS"), 168))

UPLINK_ENABLED: bool = bool(UPLINK_URL)
# More aggregators to report to as well, comma-separated (e.g. a friend's):
# each its own identity, queue and approval. JOIN_KEY, UPLINK_TOKEN and the
# commands (Kalibrera om) stay with AGGREGATOR_URL
ALSO_REPORT_TO = list(dict.fromkeys(
    u.strip().rstrip("/") for u in os.environ.get("ALSO_REPORT_TO", "").split(",")
    if u.strip() and u.strip().rstrip("/") != UPLINK_URL))

# Settings only the satellite had before v1.0 and no longer uses (those the
# aggregator reads, e.g. MQTT_* in a shared .env, are not named)
RETIRED = (
    "PUBLISH_MODE", "PUBLISH_RAW", "STATUS_INTERVAL", "LOCAL_PROCESSING",
    "SATELLITE_MQTT_ENABLED", "AUDIO_FORMAT", "RTL_DEVICE_INDEX", "RTL_CENTER_FREQ",
    "RTL_GAIN", "PPM_CORRECTION", "LOCAL_AGGREGATOR",
)


def problems():
    """What stops the satellite from starting (list of messages)."""
    out = []
    if not UPLINK_URL:
        out.append(
            "RDS Guard needs an aggregator: set AGGREGATOR_URL (the address of "
            "your aggregator, e.g. http://aggregator:8081 in the main "
            "docker-compose.yml, or https://rds.example.com). From v1.0 the "
            "satellite only receives and records; the archive, transcription, "
            "map, push and MQTT are the aggregator's. See the README.")
    elif not UPLINK_URL.lower().startswith(("http://", "https://")):
        out.append(f"AGGREGATOR_URL must start with http:// or https:// (got '{UPLINK_URL}')")
    for url in ALSO_REPORT_TO:
        if not url.lower().startswith(("http://", "https://")):
            out.append(f"ALSO_REPORT_TO: each address must start with http:// or "
                       f"https:// (got '{url}')")
    return out


def retired_in_use():
    """Settings in the environment that the satellite no longer uses."""
    return [k for k in RETIRED if os.environ.get(k, "").strip()]
