"""Push notifications on the satellite.

When an event is finished (traffic: after the transcription, or when it
times out; emergency: as soon as the broadcast starts) every enabled rule
is checked and each matching one gets a ready-to-show message on
<MQTT_TOPIC_PREFIX>/notify/<rule topic>. Home Assistant, ntfy or Node-RED
subscribe to that and send it on to a phone.

Every attempt is written to the notify log, so the web UI can tell for each
event whether a push went out, and why not.
"""

import json
import logging
import threading

import config
import event_store
import notify_rules

log = logging.getLogger("rds-guard")

_zone = None


def zone():
    global _zone
    if _zone is None:
        _zone = notify_rules.get_zone(config.NOTIFY_TIMEZONE) or False
    return _zone or None


def init():
    """Create the default rules (VMA + all traffic) on a fresh install."""
    defaults = [notify_rules.validate(r) for r in notify_rules.DEFAULT_RULES]
    if event_store.seed_rules_once(defaults):
        log.info("Created default push rules: %s",
                 ", ".join(r["name"] for r in defaults))
    if zone() is None:
        log.warning("Time zone %s not available — push rule times use UTC",
                    config.NOTIFY_TIMEZONE)


def rule_event(row):
    """An events row in the shape notify_rules expects."""
    ev = dict(row)
    rt = ev.get("radiotext")
    if isinstance(rt, str):
        try:
            ev["radiotext"] = json.loads(rt)
        except (TypeError, ValueError):
            ev["radiotext"] = [rt]
    return ev


def _status(result):
    if result is None:
        return event_store.NOTIFY_MQTT_OFF
    return event_store.NOTIFY_SENT if result else event_store.NOTIFY_FAILED


def dispatch(event_id, publish):
    """Check the rules for one event and publish a message per match.

    publish(topic, payload) returns True (sent), False (failed) or None
    (MQTT not configured). Returns the list of statuses.
    """
    row = event_store.get_event(event_id)
    if row is None:
        return []
    ev = rule_event(row)
    matched = [r for r in event_store.list_rules()
               if notify_rules.matches(r, ev, zone())]
    if not matched:
        event_store.log_notification(event_id, event_store.NOTIFY_NO_MATCH)
        return []
    audio = f"/api/audio/{row['audio_path']}" if row.get("audio_path") else None
    statuses = []
    for rule in matched:
        payload = notify_rules.build_payload(ev, rule, event_id,
                                             config.WEB_UI_URL, audio)
        topic = f"notify/{rule['topic']}"
        status = _status(publish(topic, payload))
        event_store.log_notification(event_id, status, rule=rule,
                                     topic=topic, title=payload["title"])
        statuses.append(status)
        log.info("Push '%s' for event #%s: %s", rule["name"], event_id, status)
    return statuses


def dispatch_async(event_id, publish):
    """dispatch() on its own thread: callers may hold locks."""
    def run():
        try:
            dispatch(event_id, publish)
        except Exception:
            log.exception("Push dispatch failed for event #%s", event_id)
    threading.Thread(target=run, name=f"notify-{event_id}", daemon=True).start()


def send_test(rule, publish):
    """Publish a made-up message for a rule. Returns (status, topic, payload)."""
    ev = notify_rules.test_event(rule)
    payload = notify_rules.build_payload(ev, rule, "test", config.WEB_UI_URL,
                                         test=True)
    topic = f"notify/{rule['topic']}"
    status = _status(publish(topic, payload))
    event_store.log_notification(None, status, rule=rule, topic=topic,
                                 title=payload["title"], test=True)
    return status, topic, payload
