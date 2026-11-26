"""Notification rules: which events become a push message on MQTT.

Shared by the satellite (rds_guard.py) and the aggregator. Only the logic
lives here -- validating a rule, deciding whether an event matches it and
building the message. Each side stores its rules in its own database.

A rule looks like this (every list may be empty, which means "any"):

    {
        "name": "Pendling",
        "enabled": true,
        "event_types": ["traffic"],          # traffic and/or emergency
        "stations": ["E203"],                # PI codes
        "nodes": [],                         # aggregator only: node names
        "keywords": ["E4", "Essingeleden"],  # any of them, in RadioText
                                             # or the transcription
        "days": [0, 1, 2, 3, 4],             # 0 = Monday
        "windows": [{"from": "06:00", "to": "09:00"}],  # local time
        "priority": "normal",                # normal | high | critical
        "topic": "pendling"                  # -> <prefix>/notify/pendling
    }

Days and time windows are checked against the local time when the
broadcast started (NOTIFY_TIMEZONE, default Europe/Stockholm), both when a
message is sent and when a rule is tried against the stored history, so
the preview shows exactly what the rule would have sent.
"""

import re
from datetime import datetime, timezone

EVENT_TYPES = ("traffic", "emergency")
PRIORITIES = ("normal", "high", "critical")
TYPE_LABELS = {"traffic": "Trafik", "emergency": "VMA"}

MAX_NAME = 60
MAX_LIST = 20
MAX_KEYWORD = 60
MAX_MESSAGE = 400

_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_PI_RE = re.compile(r"^(0[xX])?[0-9A-Fa-f]{4}$")

DEFAULT_RULES = (
    {"name": "VMA", "enabled": True, "event_types": ["emergency"],
     "priority": "critical", "topic": "vma"},
    {"name": "Trafik", "enabled": True, "event_types": ["traffic"],
     "priority": "normal", "topic": "trafik"},
)


class RuleError(ValueError):
    """The rule is not valid; the message is shown to the user."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _str_list(value, field, max_len=MAX_KEYWORD):
    if value is None:
        return []
    if not isinstance(value, list):
        raise RuleError(f"{field} måste vara en lista")
    out = []
    for item in value:
        if not isinstance(item, str):
            raise RuleError(f"{field} får bara innehålla text")
        item = " ".join(item.split())
        if not item:
            continue
        if len(item) > max_len:
            raise RuleError(f"{field}: högst {max_len} tecken per värde")
        if item.casefold() not in (o.casefold() for o in out):
            out.append(item)
    if len(out) > MAX_LIST:
        raise RuleError(f"{field}: högst {MAX_LIST} värden")
    return out


def slugify(text):
    """Make a topic level from a rule name: 'Pendling E4' -> 'pendling-e4'."""
    text = text.lower()
    for a, b in (("å", "a"), ("ä", "a"), ("ö", "o"), ("é", "e"), ("ü", "u")):
        text = text.replace(a, b)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:40].strip("-") or "regel"


def validate(raw):
    """Return a clean rule dict, or raise RuleError."""
    if not isinstance(raw, dict):
        raise RuleError("regeln måste vara ett JSON-objekt")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise RuleError("regeln behöver ett namn")
    name = " ".join(name.split())
    if len(name) > MAX_NAME:
        raise RuleError(f"namnet får vara högst {MAX_NAME} tecken")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise RuleError("enabled måste vara true eller false")

    types = raw.get("event_types") or []
    if not isinstance(types, list) or not types:
        raise RuleError("välj minst en typ: trafik eller VMA")
    if any(t not in EVENT_TYPES for t in types):
        raise RuleError("okänd händelsetyp")
    types = [t for t in EVENT_TYPES if t in types]

    stations = _str_list(raw.get("stations"), "stations", 6)
    for pi in stations:
        if not _PI_RE.match(pi):
            raise RuleError(f"ogiltig PI-kod: {pi}")
    stations = [norm_pi(s) for s in stations]

    nodes = _str_list(raw.get("nodes"), "nodes", 64)
    keywords = _str_list(raw.get("keywords"), "keywords")

    days = raw.get("days") or []
    if (not isinstance(days, list)
            or any(not isinstance(d, int) or isinstance(d, bool)
                   or not 0 <= d <= 6 for d in days)):
        raise RuleError("days måste vara veckodagar 0–6 (0 = måndag)")
    days = sorted(set(days))
    if len(days) == 7:
        days = []

    windows = raw.get("windows") or []
    if not isinstance(windows, list) or len(windows) > 6:
        raise RuleError("högst sex tidsintervall")
    clean_windows = []
    for w in windows:
        if not isinstance(w, dict):
            raise RuleError("tidsintervall måste ha from och to")
        start, end = w.get("from"), w.get("to")
        if not (isinstance(start, str) and _TIME_RE.match(start)
                and isinstance(end, str) and _TIME_RE.match(end)):
            raise RuleError("tider skrivs som HH:MM, t.ex. 06:30")
        if start == end:
            raise RuleError("ett tidsintervall kan inte börja och sluta samtidigt")
        clean_windows.append({"from": start, "to": end})

    priority = raw.get("priority", "normal")
    if priority not in PRIORITIES:
        raise RuleError("prioritet måste vara normal, high eller critical")

    topic = raw.get("topic")
    if topic in (None, ""):
        topic = slugify(name)
    if not isinstance(topic, str) or not _TOPIC_RE.match(topic):
        raise RuleError("ämnet får bara innehålla a–z, 0–9, - och _ (högst 40 tecken)")

    return {
        "name": name,
        "enabled": enabled,
        "event_types": types,
        "stations": stations,
        "nodes": nodes,
        "keywords": keywords,
        "days": days,
        "windows": clean_windows,
        "priority": priority,
        "topic": topic,
    }


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def norm_pi(pi):
    """'0x9e04', '9E04' -> '9E04' (redsea reports PI as 0x....)."""
    pi = (pi or "").strip().upper()
    return pi[2:] if pi.startswith("0X") else pi


def get_zone(name):
    """ZoneInfo for name, or None (then UTC is used) if tzdata is missing."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return None


def parse_utc(ts):
    """'2026-11-24T17:12:04' or '...Z' (always UTC here) -> aware datetime."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def local_start(event, zone):
    dt = parse_utc(event.get("started_at") or event.get("created_at")
                   or event.get("received_at"))
    if dt is None:
        return None
    return dt.astimezone(zone) if zone is not None else dt


def event_text(event):
    """Everything a keyword is searched in: RadioText and transcription."""
    rt = event.get("radiotext") or []
    if isinstance(rt, str):
        rt = [rt]
    parts = [str(r) for r in rt]
    if event.get("transcription"):
        parts.append(event["transcription"])
    if event.get("transcription_corrected"):
        # Corrected place names match keywords the misheard ones missed
        parts.append(event["transcription_corrected"])
    return " ".join(parts)


def _minutes(hhmm):
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(windows, minute_of_day):
    if not windows:
        return True
    for w in windows:
        start, end = _minutes(w["from"]), _minutes(w["to"])
        if start < end:
            if start <= minute_of_day < end:
                return True
        elif minute_of_day >= start or minute_of_day < end:  # over midnight
            return True
    return False


def matched_keywords(rule, event):
    if not rule.get("keywords"):
        return []
    text = event_text(event).casefold()
    return [k for k in rule["keywords"] if k.casefold() in text]


def matches(rule, event, zone):
    """True if the event should be pushed under this rule."""
    if not rule.get("enabled", True):
        return False
    if event.get("type") not in rule["event_types"]:
        return False
    if rule.get("stations") and norm_pi(event.get("pi")) not in rule["stations"]:
        return False
    if rule.get("nodes"):
        node = (event.get("node_name") or "").casefold()
        if node not in (n.casefold() for n in rule["nodes"]):
            return False
    if rule.get("keywords") and not matched_keywords(rule, event):
        return False
    if rule.get("days") or rule.get("windows"):
        start = local_start(event, zone)
        if start is None:
            return False
        if rule.get("days") and start.weekday() not in rule["days"]:
            return False
        if not in_window(rule.get("windows"), start.hour * 60 + start.minute):
            return False
    return True


# ---------------------------------------------------------------------------
# The message
# ---------------------------------------------------------------------------

def _shorten(text, limit):
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(",.;:") + "…"


def headline(event):
    rt = [r for r in (event.get("radiotext") or []) if r and str(r).strip()]
    if rt:
        return _shorten(str(rt[-1]), 90)
    station = event.get("station_ps") or event.get("pi") or "okänd station"
    if event.get("type") == "emergency":
        return f"Viktigt meddelande till allmänheten på {station}"
    return f"Trafikmeddelande på {station}"


def build_payload(event, rule, event_ref, public_url="", audio_path=None,
                  test=False, timestamp=None):
    """The JSON a push service gets on <prefix>/notify/<topic>.

    title/message are ready to show as-is; url opens the event in the web UI
    when a public address is configured.
    """
    etype = event.get("type", "traffic")
    station = {"pi": event.get("pi"), "ps": event.get("station_ps")}
    message = (event.get("transcription_corrected") or event.get("transcription")
               or " – ".join(
                   str(r) for r in (event.get("radiotext") or []) if r))
    if not message:
        message = headline(event)
    base = (public_url or "").rstrip("/")
    payload = {
        "title": f"{TYPE_LABELS.get(etype, etype)}: {headline(event)}",
        "message": _shorten(message, MAX_MESSAGE),
        "priority": rule.get("priority", "normal"),
        "event_type": etype,
        "rule": {"id": rule.get("id"), "name": rule.get("name")},
        "matched_keywords": matched_keywords(rule, event),
        "station": station,
        "frequency": event.get("frequency"),
        "started": event.get("started_at"),
        "duration_sec": event.get("duration_sec"),
        "event_id": event_ref,
        "url": f"{base}/#/event/{event_ref}" if base else None,
        "audio_url": f"{base}{audio_path}" if base and audio_path else None,
        "test": test,
        "timestamp": timestamp or datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
    }
    if event.get("node_name"):
        payload["node"] = {"name": event["node_name"]}
    return payload


def test_event(rule):
    """A made-up event for 'Skicka test'."""
    etype = rule["event_types"][0] if rule.get("event_types") else "traffic"
    word = rule["keywords"][0] if rule.get("keywords") else "E4"
    return {
        "type": etype,
        "pi": rule["stations"][0] if rule.get("stations") else "0000",
        "station_ps": "RDS Guard",
        "radiotext": [f"Test av regeln {rule.get('name')}"],
        "transcription": f"Det här är ett testmeddelande från RDS Guard. "
                         f"Regeln bevakar bland annat {word}.",
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "duration_sec": 0,
    }


# ---------------------------------------------------------------------------
# Trying a rule on the history
# ---------------------------------------------------------------------------

def preview(rule, events, zone, sample_limit=5):
    """How the rule would have behaved on the stored events.

    events: newest first. Returns totals, a per-day breakdown (local dates)
    and the newest matching events.
    """
    rule = dict(rule, enabled=True)
    days = {}
    samples = []
    matched = 0
    for ev in events:
        start = local_start(ev, zone)
        day = start.strftime("%Y-%m-%d") if start else "?"
        bucket = days.setdefault(day, [0, 0])
        bucket[0] += 1
        if matches(rule, ev, zone):
            matched += 1
            bucket[1] += 1
            if len(samples) < sample_limit:
                samples.append(ev)
    return {
        "total": len(events),
        "matched": matched,
        "days": [{"date": d, "total": v[0], "matched": v[1]}
                 for d, v in sorted(days.items())],
        "samples": samples,
    }
