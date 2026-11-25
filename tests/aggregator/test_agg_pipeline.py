"""Aggregator pipeline: alert timing, MQTT payloads, transcription, nodes."""

import time

import pytest

from aggregator import config, pipeline, store

T0 = 1_800_000_000.0   # fixed "now" for timing tests


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


class FakeMqtt:
    def __init__(self, connected=True):
        self.connected = connected
        self.messages = []

    def publish(self, topic, payload, qos=1, retain=False):
        if not self.connected:
            return False
        self.messages.append((topic, payload, retain))
        return True

    def topics(self):
        return [m[0] for m in self.messages]


class FakeTranscriber:
    def __init__(self):
        self.jobs = []

    def enqueue(self, path, event_id, callback):
        self.jobs.append((path, event_id, callback))


@pytest.fixture
def node(agg_store):
    n, _ = agg_store.create_node("Kalle")
    return n


def _store_event(node, uid="11111111-1111-4111-8111-111111111111",
                 state="end", rev=1, **kw):
    ev = {"rev": rev, "type": "traffic", "state": state, "pi": "0x9E04",
          "station_ps": "P4 Test", "long_ps": None, "frequency": "103.3M",
          "prog_type": "Information", "radiotext": ["Kö på E18"],
          "started_at": "2026-11-25T10:00:00Z",
          "ended_at": "2026-11-25T10:01:00Z" if state == "end" else None,
          "duration_sec": 60 if state == "end" else None,
          "sent_at": "2026-11-25T10:01:01Z"}
    ev.update(kw)
    return store.upsert_event(node["id"], uid, ev)


# -- alert_decision ----------------------------------------------------------

def _row(**kw):
    row = {"ended_at": _iso(T0 - 10), "end_received_at": _iso(T0 - 10),
           "audio_path": None, "audio_received_at": None,
           "transcription_status": None}
    row.update(kw)
    return row


def test_waits_for_audio_then_publishes_without():
    assert pipeline.alert_decision(_row(), T0) == ("wait", None)
    late = _row(end_received_at=_iso(T0 - config.ALERT_AUDIO_WAIT_SEC - 1))
    assert pipeline.alert_decision(late, T0) == ("publish", "none")


def test_waits_for_transcription_then_times_out():
    r = _row(audio_path="x.ogg", audio_received_at=_iso(T0 - 5),
             transcription_status="queued")
    assert pipeline.alert_decision(r, T0) == ("wait", None)
    r["audio_received_at"] = _iso(T0 - config.ALERT_TRANSCRIPTION_WAIT_SEC - 1)
    assert pipeline.alert_decision(r, T0) == ("publish", "timeout")


@pytest.mark.parametrize("status", ["done", "error"])
def test_publishes_when_transcription_finished(status):
    r = _row(audio_path="x.ogg", audio_received_at=_iso(T0),
             transcription_status=status)
    assert pipeline.alert_decision(r, T0) == ("publish", status)


def test_transcription_disabled_publishes_immediately_with_audio():
    r = _row(audio_path="x.ogg", audio_received_at=_iso(T0))
    assert pipeline.alert_decision(r, T0, engine_enabled=False) == ("publish", "none")


def test_late_backlog_is_skipped():
    old = T0 - config.ALERT_MAX_DELAY_MIN * 60 - 1
    r = _row(ended_at=_iso(old), audio_path="x.ogg",
             transcription_status="done")
    assert pipeline.alert_decision(r, T0) == ("skip", None)


# -- end to end through the store ---------------------------------------------

def test_alert_published_once_with_transcription(agg_store, node):
    mqtt, tr = FakeMqtt(), FakeTranscriber()
    p = pipeline.Pipeline(transcriber=tr, mqtt=mqtt)
    _, _, row = _store_event(node, ended_at=_iso(time.time()))
    row = agg_store.set_event_audio(node["id"], row["event_uid"],
                                    f"{row['event_uid']}.ogg", "queued")
    p.on_audio(row)
    assert len(tr.jobs) == 1

    p.sweep()
    assert "alert" not in mqtt.topics()          # still transcribing

    _, event_id, callback = tr.jobs[0]
    callback(event_id, "Olycka vid Gnesta", None, 3.2)
    p.sweep()
    p.sweep()
    alerts = [m for m in mqtt.messages if m[0] == "alert"]
    assert len(alerts) == 1
    payload = alerts[0][1]
    assert payload["transcribed_text"] == "Olycka vid Gnesta"
    assert payload["transcription_status"] == "done"
    assert payload["event_type"] == "traffic_announcement"
    assert payload["node"] == {"name": "Kalle"}
    assert payload["station"]["ps"] == "P4 Test"
    assert payload["radiotext"] == ["Kö på E18"]
    assert "0x9E04/traffic/transcription" in mqtt.topics()
    assert agg_store.get_event(row["event_uid"])["alert_status"] == "published"


def test_alert_retried_until_mqtt_connects(agg_store, node):
    mqtt = FakeMqtt(connected=False)
    p = pipeline.Pipeline(transcriber=None, mqtt=mqtt)
    _, _, row = _store_event(node, ended_at=_iso(time.time()))
    agg_store.set_event_audio(node["id"], row["event_uid"], "a.ogg", None)
    p.sweep()
    assert agg_store.get_event(row["event_uid"])["alert_published_at"] is None
    mqtt.connected = True
    p.sweep()
    assert mqtt.topics().count("alert") == 1


def test_without_mqtt_alerts_are_marked(agg_store, node):
    p = pipeline.Pipeline(transcriber=None, mqtt=None)
    _, _, row = _store_event(node, ended_at=_iso(time.time()))
    agg_store.set_event_audio(node["id"], row["event_uid"], "a.ogg", None)
    p.sweep()
    assert agg_store.get_event(row["event_uid"])["alert_status"] == "mqtt_disabled"


def test_ta_flag_follows_event(agg_store, node):
    mqtt = FakeMqtt()
    p = pipeline.Pipeline(mqtt=mqtt)
    res, prev, row = _store_event(node, state="start")
    p.on_event(row, prev)
    res, prev, row = _store_event(node, state="update", rev=2)
    p.on_event(row, prev)                       # still active: no new message
    res, prev, row = _store_event(node, state="end", rev=3)
    p.on_event(row, prev)
    ta = [m[1]["active"] for m in mqtt.messages if m[0] == "0x9E04/traffic/ta"]
    assert ta == [True, False]


def test_backlog_event_arriving_ended_sets_no_ta(agg_store, node):
    mqtt = FakeMqtt()
    p = pipeline.Pipeline(mqtt=mqtt)
    _, prev, row = _store_event(node, state="end")
    p.on_event(row, prev)
    assert mqtt.messages == []


def test_deleted_short_event_clears_ta(agg_store, node):
    mqtt = FakeMqtt()
    p = pipeline.Pipeline(mqtt=mqtt)
    _, prev, row = _store_event(node, state="start")
    p.on_event(row, prev)
    p.on_delete(agg_store.delete_event(node["id"], row["event_uid"]))
    ta = [m[1]["active"] for m in mqtt.messages if m[0] == "0x9E04/traffic/ta"]
    assert ta == [True, False]


def test_requeue_pending_after_restart(agg_store, node):
    _, _, row = _store_event(node)
    agg_store.set_event_audio(node["id"], row["event_uid"], "a.ogg", "queued")
    tr = FakeTranscriber()
    pipeline.Pipeline(transcriber=tr).requeue_pending()
    assert [j[1] for j in tr.jobs] == [row["id"]]


def test_transcription_error_is_stored(agg_store, node):
    tr = FakeTranscriber()
    p = pipeline.Pipeline(transcriber=tr)
    _, _, row = _store_event(node)
    row = agg_store.set_event_audio(node["id"], row["event_uid"], "a.ogg", "queued")
    p.on_audio(row)
    _, event_id, cb = tr.jobs[0]
    cb(event_id, None, RuntimeError("boom"), None)
    assert agg_store.get_event(row["event_uid"])["transcription_status"] == "error"


def test_node_online_offline(agg_store, node):
    mqtt = FakeMqtt()
    p = pipeline.Pipeline(mqtt=mqtt)
    agg_store.record_heartbeat(node["id"], "Kalle", "1.0", {}, 0.0)
    p.on_heartbeat(agg_store.get_node(node["id"]))
    assert p.node_online(node["id"])
    p.sweep(now=time.time() + config.NODE_OFFLINE_SEC + 5)
    assert not p.node_online(node["id"])
    states = [m[1]["online"] for m in mqtt.messages if m[0] == "nodes/kalle/status"]
    assert states == [True, False]


def test_retention_removes_audio(agg_store, node, tmp_path):
    _, _, row = _store_event(node)
    audio = tmp_path / "audio"
    audio.mkdir(exist_ok=True)
    (audio / "a.ogg").write_bytes(b"OggS")
    agg_store.set_event_audio(node["id"], row["event_uid"], "a.ogg", None)
    agg_store._conn().execute("UPDATE events SET received_at = '2000-01-01T00:00:00Z'")
    agg_store._conn().commit()
    pipeline.Pipeline().sweep()
    assert agg_store.get_event(row["event_uid"]) is None
    assert not (audio / "a.ogg").exists()


def test_topic_slug():
    from aggregator.mqtt_out import topic_slug
    assert topic_slug("Kalle, Karlstad") == "kalle-karlstad"
    assert topic_slug("a/b#+") == "a-b"
    assert topic_slug("Göteborg, Älvsjö") == "goteborg-alvsjo"
    assert topic_slug("!!!") == "node"
