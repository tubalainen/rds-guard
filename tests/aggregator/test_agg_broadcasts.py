"""One broadcast heard by several nodes (or receivers) is one event: one
transcription, one alert, one push. Each node's reception is kept."""

import time
import uuid

import pytest

from aggregator import config, pipeline, store
from notify_rules import matches



class FakeMqtt:
    def __init__(self):
        self.connected = True
        self.messages = []

    def publish(self, topic, payload, qos=1, retain=False):
        self.messages.append((topic, payload, retain))
        return True

    def topics(self):
        return [m[0] for m in self.messages]


class FakeTranscriber:
    def __init__(self):
        self.jobs = []

    def enqueue(self, path, event_id, callback, hints=None):
        self.jobs.append((path, event_id, callback))

NOW = time.time()


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _uid():
    return str(uuid.uuid4())


def ev(start, end=None, state=None, rev=1, pi="0xE824", type_="traffic", **kw):
    state = state or ("end" if end is not None else "start")
    d = {"rev": rev, "type": type_, "state": state, "pi": pi,
         "station_ps": "P4 Värml", "long_ps": None, "frequency": "103.5M",
         "prog_type": None, "radiotext": kw.pop("radiotext", []),
         "started_at": _iso(start), "ended_at": _iso(end) if end else None,
         "duration_sec": int(end - start) if end else None,
         "sent_at": _iso(end or start), "receiver": kw.pop("receiver", "0")}
    d.update(kw)
    return d


@pytest.fixture
def nodes(agg_store):
    a, _ = agg_store.create_node("Kalle")
    b, _ = agg_store.create_node("Lisa")
    return a, b


def put(node, uid, body):
    return store.upsert_event(node["id"], uid, body, config.MATCH_WINDOW_SEC,
                              config.BROADCAST_END_GRACE_SEC)


def count_events():
    return store._conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]


T = NOW - 600   # a broadcast ten minutes ago


def test_two_nodes_one_broadcast(agg_store, nodes):
    a, b = nodes
    mqtt = FakeMqtt()
    p = pipeline.Pipeline(mqtt=mqtt)
    ua, ub = _uid(), _uid()
    _, prev, row = put(a, ua, ev(T))
    p.on_event(row, prev)
    _, prev, row = put(b, ub, ev(T + 4, radiotext=["Olycka E18"]))
    p.on_event(row, prev)                     # same broadcast: no second TA on
    assert count_events() == 1
    assert row["event_uid"] == ua
    assert row["node_names"] == ["Kalle", "Lisa"]
    assert row["radiotext"] == ["Olycka E18"]
    _, prev, row = put(a, ua, ev(T, T + 90, rev=2))
    p.on_event(row, prev)
    assert row["state"] != "end"              # Lisa still hears it
    _, prev, row = put(b, ub, ev(T + 4, T + 92, rev=2))
    p.on_event(row, prev)
    assert row["state"] == "end" and row["started_at"] == _iso(T)
    assert row["ended_at"] == _iso(T + 92)
    ta = [m[1]["active"] for m in mqtt.messages if m[0].endswith("traffic/ta")]
    assert ta == [True, False]


def test_vma_is_pushed_once_when_two_nodes_hear_it(agg_store, nodes, monkeypatch):
    a, b = nodes
    pushed = []
    monkeypatch.setattr(pipeline.notifier, "dispatch_async",
                        lambda row, pub: pushed.append(row["event_uid"]))
    p = pipeline.Pipeline(mqtt=FakeMqtt())
    for node in (a, b):
        _, prev, row = put(node, _uid(), ev(T, state="active", type_="emergency"))
        p.on_event(row, prev)
    assert len(pushed) == 1 and count_events() == 1


def test_clock_offset_is_applied(agg_store, nodes):
    a, b = nodes
    # Lisa's clock is 3 minutes slow: her start reads 3 minutes early
    store.record_heartbeat(b["id"], "Lisa", "1", {}, 180.0)
    put(a, _uid(), ev(T))
    put(b, _uid(), ev(T - 175))
    assert count_events() == 1


def test_null_clock_offset_counts_as_zero(agg_store, nodes):
    a, b = nodes
    put(a, _uid(), ev(T))
    put(b, _uid(), ev(T + 10))       # Lisa never sent a heartbeat
    assert count_events() == 1


def test_two_announcements_a_minute_apart_stay_two(agg_store, nodes):
    a, b = nodes
    put(a, _uid(), ev(T, T + 40))
    put(a, _uid(), ev(T + 100, T + 140))
    assert count_events() == 2
    # Another node starting after the first ended is the second one
    put(b, _uid(), ev(T + 101))
    rows, _ = store.query_events()
    assert sorted(len(r["node_names"]) for r in rows) == [1, 2]


def test_fragments_of_a_weak_node_join_the_whole(agg_store, nodes):
    a, b = nodes
    put(a, _uid(), ev(T))                     # Kalle hears it all, still on
    put(b, _uid(), ev(T + 10, T + 50))        # Lisa: a fragment …
    put(b, _uid(), ev(T + 90))                # … and another while it is on
    assert count_events() == 1
    assert store._conn().execute("SELECT COUNT(*) FROM receptions").fetchone()[0] == 3


def test_other_station_or_type_is_another_broadcast(agg_store, nodes):
    a, b = nodes
    put(a, _uid(), ev(T))
    put(b, _uid(), ev(T, pi="0x9E04"))
    put(b, _uid(), ev(T, type_="emergency", state="active"))
    assert count_events() == 3


def test_longest_recording_is_transcribed_once(agg_store, nodes):
    a, b = nodes
    tr = FakeTranscriber()
    p = pipeline.Pipeline(transcriber=tr, mqtt=FakeMqtt())
    ua, ub = _uid(), _uid()
    put(a, ua, ev(T, T + 60))
    put(b, ub, ev(T + 5, T + 95))             # Lisa heard more of it
    row = store.set_event_audio(a["id"], ua, f"{ua}.ogg", None, 9000)
    p.on_audio(row)
    assert tr.jobs == []                       # Lisa's recording may follow
    row = store.set_event_audio(b["id"], ub, f"{ub}.ogg", None, 8000)
    p.on_audio(row)
    assert len(tr.jobs) == 1                   # both in: the longest at once
    chosen = store.get_event(ua)
    assert chosen["audio_path"] == f"{ub}.ogg" and chosen["node_name"] == "Lisa"
    # A recording sent again later changes nothing
    row = store.set_event_audio(a["id"], ua, f"{ua}.ogg", None, 9000)
    p.on_audio(row)
    p.sweep()
    assert len(tr.jobs) == 1
    assert store.get_event(ua)["audio_path"] == f"{ub}.ogg"


def test_a_single_recording_is_transcribed_at_once(agg_store, nodes):
    a, _ = nodes
    tr = FakeTranscriber()
    p = pipeline.Pipeline(transcriber=tr)
    ua = _uid()
    put(a, ua, ev(T, T + 60))
    p.on_audio(store.set_event_audio(a["id"], ua, f"{ua}.ogg", None, 100))
    assert len(tr.jobs) == 1


def test_the_wait_for_more_recordings_survives_a_restart(agg_store, nodes):
    a, b = nodes
    ua = _uid()
    put(a, ua, ev(T, T + 60))
    put(b, _uid(), ev(T + 5))                 # Lisa never sends audio
    pipeline.Pipeline(transcriber=FakeTranscriber()).on_audio(
        store.set_event_audio(a["id"], ua, f"{ua}.ogg", None, 100))
    tr = FakeTranscriber()                      # "after a restart"
    p = pipeline.Pipeline(transcriber=tr)
    p.sweep(now=time.time())
    assert tr.jobs == []
    p.sweep(now=time.time() + config.BEST_AUDIO_WAIT_SEC + 1)
    assert len(tr.jobs) == 1


def test_a_silent_node_does_not_hold_the_alert(agg_store, nodes, monkeypatch):
    a, b = nodes
    mqtt = FakeMqtt()
    p = pipeline.Pipeline(mqtt=mqtt)
    ua = _uid()
    _, prev, row = put(a, ua, ev(T))
    p.on_event(row, prev)
    put(b, _uid(), ev(T + 3))                 # Lisa's satellite then goes silent
    _, prev, row = put(a, ua, ev(T, T + 60, rev=2))
    p.on_event(row, prev)
    assert row["state"] != "end"
    monkeypatch.setattr(config, "BROADCAST_END_GRACE_SEC", 0)
    p.sweep()
    assert store.get_event(ua)["state"] == "end"
    ta = [m[1]["active"] for m in mqtt.messages if m[0].endswith("traffic/ta")]
    assert ta == [True, False]
    p.sweep(now=time.time() + config.ALERT_AUDIO_WAIT_SEC + 5)
    assert mqtt.topics().count("alert") == 1


def test_late_arrivals_do_not_reopen_an_ended_broadcast(agg_store, nodes):
    a, b = nodes
    mqtt = FakeMqtt()
    p = pipeline.Pipeline(mqtt=mqtt)
    ua = _uid()
    put(a, ua, ev(T, T + 60))
    _, prev, row = put(b, _uid(), ev(T + 2))  # Lisa's backlog: still "on"
    p.on_event(row, prev)
    assert row["state"] == "end" and count_events() == 1
    assert mqtt.messages == []


def test_deleting_receptions(agg_store, nodes, tmp_path):
    a, b = nodes
    ua, ub = _uid(), _uid()
    put(a, ua, ev(T))
    put(b, ub, ev(T + 2))
    # Kalle's (the first) is discarded: the broadcast keeps its id
    rec, before, after = store.delete_event(a["id"], ua)
    assert after is not None and after["event_uid"] == ua
    assert after["node_names"] == ["Lisa"] and after["node_name"] == "Lisa"
    rec, before, after = store.delete_event(b["id"], ub)
    assert after is None and count_events() == 0


def test_removing_a_node_keeps_what_others_heard(agg_store, nodes):
    a, b = nodes
    ua, ub, only = _uid(), _uid(), _uid()
    put(a, ua, ev(T, T + 60))
    put(b, ub, ev(T + 2, T + 61))
    put(a, only, ev(T + 600, T + 640))
    store.set_event_audio(a["id"], ua, f"{ua}.ogg", None, 10)
    store.set_event_audio(b["id"], ub, f"{ub}.ogg", None, 10)
    store.choose_audio(store.get_event(ua)["id"], None, 0)
    files = store.delete_node(a["id"])
    assert f"{ua}.ogg" in files               # Kalle's recording goes too
    assert count_events() == 1
    kept = store.get_event(ua)
    assert kept["node_names"] == ["Lisa"]
    assert kept["audio_path"] == f"{ub}.ogg"  # Lisa's recording plays now


def test_search_and_rules_see_every_node_that_heard_it(agg_store, nodes):
    a, b = nodes
    ua = _uid()
    put(a, ua, ev(T, T + 60))
    put(b, _uid(), ev(T + 2, T + 61))
    rows, total = store.query_events(node="Lisa")
    assert total == 1 and rows[0]["event_uid"] == ua
    rule = {"name": "Lisas", "enabled": True, "event_types": ["traffic"],
            "nodes": ["Lisa"], "priority": "normal", "topic": "t"}
    from zoneinfo import ZoneInfo
    assert matches(rule, rows[0], ZoneInfo("Europe/Stockholm"))
    stations = store.list_stations()
    assert stations[0]["nodes"] == ["Kalle", "Lisa"]


def test_alert_names_every_node(agg_store, nodes):
    a, b = nodes
    ua = _uid()
    put(a, ua, ev(T, T + 60))
    put(b, _uid(), ev(T + 2, T + 61))
    payload = pipeline.alert_payload(store.get_event(ua), "none")
    assert payload["node"] == {"name": "Kalle"}
    assert payload["nodes"] == [{"name": "Kalle"}, {"name": "Lisa"}]


def test_events_from_before_get_their_reception(agg_store, nodes):
    a, _ = nodes
    conn = store._conn()
    conn.execute(
        "INSERT INTO events (event_uid, node_id, rev, type, state, pi, radiotext, "
        "received_at, updated_at, audio_path) VALUES (?, ?, 1, 'traffic', 'end', "
        "'0x1', '[]', ?, ?, 'x.ogg')", ("aaaaaaaa-0000-4000-8000-000000000000",
                                       a["id"], _iso(T), _iso(T)))
    conn.commit()
    assert store.ensure_receptions() == 1
    assert store.ensure_receptions() == 0
    rec = store.get_reception("aaaaaaaa-0000-4000-8000-000000000000")
    assert rec["audio_path"] == "x.ogg" and rec["node_name"] == "Kalle"


async def test_api_shows_who_heard_it_and_each_recording(aiohttp_client, agg_store,
                                                          nodes, tmp_path):
    from aggregator import api
    a, b = nodes
    ua, ub = _uid(), _uid()
    put(a, ua, ev(T, T + 60))
    put(b, ub, ev(T + 2, T + 70))
    audio = tmp_path / "audio"
    audio.mkdir(exist_ok=True)
    for u in (ua, ub):
        (audio / f"{u}.ogg").write_bytes(b"OggS" + u.encode())
        store.set_event_audio(a["id"] if u == ua else b["id"], u, f"{u}.ogg", None, 10)
    store.choose_audio(store.get_event(ua)["id"], None, 0)
    web = await aiohttp_client(api.create_web_app())
    data = await (await web.get(f"/api/v1/events/{ua}")).json()
    assert data["nodes"] == ["Kalle", "Lisa"] and data["node_name"] == "Lisa"
    assert [r["node_name"] for r in data["receptions"]] == ["Kalle", "Lisa"]
    assert [r["transcribed"] for r in data["receptions"]] == [False, True]
    # The broadcast's audio is the transcribed one; each node's plays too
    assert (await (await web.get(data["audio_url"])).read()).endswith(ub.encode())
    first = data["receptions"][0]["audio_url"]
    assert (await (await web.get(first)).read()).endswith(ua.encode())
    listed = (await (await web.get("/api/v1/events?node=Kalle")).json())["events"]
    assert [e["event_uid"] for e in listed] == [ua]
    # Only the broadcast's id is an event; a reception's id is not
    assert (await web.get(f"/api/v1/events/{ub}")).status == 404


def _silent(node, ago):
    conn = store._conn()
    conn.execute("UPDATE nodes SET last_seen_at = ? WHERE id = ?", (_iso(time.time() - ago), node["id"]))
    conn.commit()


def test_a_broadcast_left_open_by_a_silent_node_takes_no_later_one(agg_store, nodes, monkeypatch):
    """Lisa went silent mid-broadcast; Kalle's next announcement five minutes
    later is a new event with its own TA, transcription and alert."""
    a, b = nodes
    mqtt, tr = FakeMqtt(), FakeTranscriber()
    p = pipeline.Pipeline(mqtt=mqtt, transcriber=tr)
    t = time.time() - 900
    first = _uid()
    put(a, first, ev(t))
    put(b, _uid(), ev(t + 2))                 # never ends
    put(a, first, ev(t, t + 60, rev=2))
    _silent(b, 600)
    monkeypatch.setattr(config, "BROADCAST_END_GRACE_SEC", 0)
    p.sweep()
    assert store.get_event(first)["state"] == "end"
    second = _uid()
    _, prev, row = put(a, second, ev(t + 360))
    p.on_event(row, prev)
    assert count_events() == 2 and row["event_uid"] == second
    _, prev, row = put(a, second, ev(t + 360, t + 420, rev=2))
    p.on_event(row, prev)
    p.on_audio(store.set_event_audio(a["id"], second, f"{second}.ogg", None, 10))
    assert len(tr.jobs) == 1
    ta = [m[1]["active"] for m in mqtt.messages if m[0].endswith("traffic/ta")]
    assert ta[-2:] == [True, False]


def test_a_reception_stuck_open_does_not_swallow_the_next_hour(agg_store, nodes):
    """One node, its first reception never ended (still heard from): a new
    announcement long after is not folded into it."""
    a, _ = nodes
    t = time.time() - 3600
    put(a, _uid(), ev(t))
    put(a, _uid(), ev(t + 1800))
    assert count_events() == 2


def test_open_broadcast_of_a_silent_node_takes_no_new_reception(agg_store, nodes):
    a, b = nodes
    t = time.time() - 600
    put(b, _uid(), ev(t))                     # Lisa: open, then silent
    _silent(b, 400)
    put(a, _uid(), ev(t + 200))
    assert count_events() == 2


def test_a_short_ended_broadcast_takes_nothing_after_its_end(agg_store, nodes):
    a, b = nodes
    put(a, _uid(), ev(T, T + 12))
    put(b, _uid(), ev(T + 20))                # within 30 s of the start, after the end
    assert count_events() == 2
    put(b, _uid(), ev(T - 5, T + 11))          # began 5 s earlier: the same one
    assert count_events() == 2
    rows, _ = store.query_events()
    assert sorted(len(r["node_names"]) for r in rows) == [1, 2]
