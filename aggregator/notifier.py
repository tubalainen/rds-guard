"""Push notifications on the aggregator.

Same rules as on the satellite (notify_rules.py), checked for events from
every node. A traffic announcement is pushed when its alert goes out (after
the transcription or its timeout); an emergency as soon as the start of the
broadcast arrives. Messages go to <MQTT_TOPIC_PREFIX>/notify/<rule topic>.
"""

import logging
import threading

import notify_rules
import trafikverket

from . import config, store

log = logging.getLogger("rds-aggregator")

_zone = None


def zone():
    global _zone
    if _zone is None:
        _zone = notify_rules.get_zone(config.NOTIFY_TIMEZONE) or False
    return _zone or None


def init():
    defaults = [notify_rules.validate(r) for r in notify_rules.DEFAULT_RULES]
    if store.seed_rules_once(defaults):
        log.info("Created default push rules: %s",
                 ", ".join(r["name"] for r in defaults))


def _status(result):
    if result is None:
        return store.NOTIFY_MQTT_OFF
    return store.NOTIFY_SENT if result else store.NOTIFY_FAILED


def dispatch(row, publish):
    """Check the rules for one event row; publish(topic, payload) -> bool|None."""
    uid = row["event_uid"]
    matched = [r for r in store.list_rules()
               if notify_rules.matches(r, row, zone())]
    if not matched:
        store.log_notification(uid, store.NOTIFY_NO_MATCH)
        return []
    audio = f"/api/v1/audio/{uid}.ogg" if row.get("audio_path") else None
    statuses = []
    for rule in matched:
        payload = notify_rules.build_payload(row, rule, uid, config.WEB_UI_URL,
                                             audio)
        topic = f"notify/{rule['topic']}"
        status = _status(publish(topic, payload))
        store.log_notification(uid, status, rule=rule, topic=topic,
                               title=payload["title"])
        statuses.append(status)
        log.info("Push '%s' for %s from %s: %s", rule["name"],
                 row.get("station_ps") or row.get("pi"), row.get("node_name"),
                 status)
    return statuses


def dispatch_async(row, publish):
    def run():
        try:
            dispatch(row, publish)
        except Exception:
            log.exception("Push dispatch failed for %s", row.get("event_uid"))
    threading.Thread(target=run, daemon=True, name="notify").start()


def send_test(rule, publish):
    ev = notify_rules.test_event(rule)
    payload = notify_rules.build_payload(ev, rule, "test", config.WEB_UI_URL,
                                         test=True)
    topic = f"notify/{rule['topic']}"
    status = _status(publish(topic, payload))
    store.log_notification(None, status, rule=rule, topic=topic,
                           title=payload["title"], test=True)
    return status, topic, payload


# ---------------------------------------------------------------------------
# Trafikverket's updates of incidents a push already went out for
# ---------------------------------------------------------------------------

_gate = trafikverket.UpdateGate()


def trv_updates(dev_ids, after_vid, publish, skip_incidents=()):
    """Same as on the satellite: push what Trafikverket changed for
    incidents whose radio message was pushed, to the same rules."""
    now = store.trv.now()
    sent = 0
    for dev_id in dev_ids:
        dev = store.trv.get(dev_id, versions=False)
        incs = [i for i in store.incidents_for_deviation(dev_id)
                if i["id"] not in skip_incidents]
        if dev is None or not incs:
            continue
        changes = trafikverket.latest_changes(store.trv, dev_id, after_vid)
        ended = trafikverket.deviation_ended(dev, now)
        if not changes or not _gate.worth(dev_id, changes, ended):
            continue
        for inc in incs:
            keys = store.incident_event_keys(inc["id"])
            for rule_id in store.rules_sent_for(keys):
                rule = store.get_rule(rule_id)
                if not rule or not rule.get("enabled"):
                    continue
                payload = trafikverket.update_payload(
                    dev, changes, inc, rule, keys[-1], config.WEB_UI_URL,
                    zone(), now)
                topic = f"notify/{rule['topic']}"
                status = _status(publish(topic, payload))
                store.log_notification(keys[-1], status, rule=rule,
                                       topic=topic, title=payload["title"])
                sent += 1
                log.info("Push '%s', Trafikverket update for incident #%s: %s",
                         rule["name"], inc["id"], status)
    return sent
