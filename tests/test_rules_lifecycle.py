"""The satellite's core, end to end: decoded RDS groups in, stored events
and uplink outbox rows out. This must keep working when the satellite is
made headless (plan phase 3), whatever else is removed around it."""

import pytest

import rds_guard as rg


@pytest.fixture
def engine(store):
    store.enable_outbox()
    return rg.Station("tak", "103.5M")


PI = "0xE824"


def group(station, ts, **fields):
    rg.process_group(station, {"pi": PI, "timestamp": ts, **fields})


def outbox(store):
    return [(r["kind"], r["event_id"]) for r in
            store._conn().execute("SELECT kind, event_id FROM outbox ORDER BY id")]


def test_traffic_announcement(store, engine):
    group(engine, "2026-11-27T10:00:00", ta=False)          # first group: TA off
    assert store._conn().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert outbox(store) == []

    group(engine, "2026-11-27T10:00:05", ta=True)
    [ev] = store.query_events()[0]
    assert ev["type"] == "traffic" and ev["state"] == "start"
    assert ev["frequency"] == "103.5M" and ev["event_uid"]
    assert ev["receiver"] == "tak"
    eid = ev["id"]
    assert outbox(store) == [("event", eid)]                # sent at once

    group(engine, "2026-11-27T10:00:20", group="2A", radiotext="Olycka på E18  ")
    group(engine, "2026-11-27T10:01:05", ta=False)
    ev = store.get_event(eid)
    assert ev["state"] == "end" and ev["duration_sec"] == 60
    import json
    assert json.loads(ev["radiotext"]) == ["Olycka på E18"]
    assert outbox(store) == [("event", eid)] * 3            # start, update, end


def test_a_short_blip_is_deleted_and_the_delete_queued(store, engine):
    group(engine, "2026-11-27T10:00:00", ta=False)
    group(engine, "2026-11-27T10:00:05", ta=True)
    [ev] = store.query_events()[0]
    group(engine, "2026-11-27T10:00:09", ta=False)          # 4 s: noise
    assert store.get_event(ev["id"]) is None
    assert outbox(store) == [("event", ev["id"]), ("delete", ev["id"])]


def test_emergency_broadcast(store, engine):
    group(engine, "2026-11-27T10:00:00", prog_type="News")
    group(engine, "2026-11-27T10:00:10", prog_type="Alarm")
    [ev] = store.query_events()[0]
    assert ev["type"] == "emergency" and ev["state"] == "active"
    group(engine, "2026-11-27T10:02:10", prog_type="News")
    ev = store.get_event(ev["id"])
    assert ev["state"] == "end" and ev["duration_sec"] == 120
    assert [k for k, _ in outbox(store)] == ["event", "event"]


def test_repeated_flags_change_nothing(store, engine):
    group(engine, "2026-11-27T10:00:00", ta=False)
    group(engine, "2026-11-27T10:00:05", ta=True)
    for s in range(6, 20):
        group(engine, f"2026-11-27T10:00:{s:02d}", ta=True)
    assert store._conn().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
    assert len(outbox(store)) == 1


def test_the_same_radiotext_in_the_next_announcement_is_kept(store, engine):
    for ts, fields in (("10:00:00", {"ta": False}), ("10:00:05", {"ta": True}),
                       ("10:00:10", {"group": "2A", "radiotext": "Olycka E18"}),
                       ("10:01:05", {"ta": False}), ("10:05:00", {"ta": True}),
                       ("10:05:10", {"group": "2A", "radiotext": "Olycka E18"}),
                       ("10:06:05", {"ta": False})):
        group(engine, f"2026-11-27T{ts}", **fields)
    import json
    rows, _ = store.query_events()
    assert [json.loads(r["radiotext"]) for r in rows] == [["Olycka E18"], ["Olycka E18"]]


# P4 switches PI between the shared national programme (0xE224) and its
# regional PI (0xE824), also in the middle of an announcement.

def flip(station, pi, ts, **fields):
    rg.process_group(station, {"pi": pi, "timestamp": ts, **fields})


def test_an_announcement_that_ends_on_the_other_pi_ends(store, engine):
    flip(engine, "0xE824", "2026-11-27T10:00:00", ta=False)
    flip(engine, "0xE224", "2026-11-27T19:00:00", ta=False)
    flip(engine, "0xE824", "2026-11-27T22:00:00", ta=True)     # regional break-in
    flip(engine, "0xE224", "2026-11-27T22:02:00", ta=False)    # back to national
    [ev] = store.query_events()[0]
    assert ev["state"] == "end" and ev["pi"] == "0xE824" and ev["duration_sec"] == 120
    assert not engine.engine.busy()

    flip(engine, "0xE824", "2026-11-28T01:00:00", ta=True)     # the next one is seen
    flip(engine, "0xE224", "2026-11-28T01:01:00", ta=False)
    evs = store.query_events()[0]
    assert len(evs) == 2 and all(e["state"] == "end" for e in evs)


def test_a_pi_switch_mid_announcement_keeps_the_event_and_its_text(store, engine):
    flip(engine, "0xE224", "2026-11-27T22:00:00", ta=False)
    flip(engine, "0xE224", "2026-11-27T22:00:05", ta=True)
    flip(engine, "0xE824", "2026-11-27T22:00:10", ta=True)     # same announcement
    flip(engine, "0xE824", "2026-11-27T22:00:20", group="2A", radiotext="Olycka på E18")
    flip(engine, "0xE824", "2026-11-27T22:01:05", ta=False)
    [ev] = store.query_events()[0]
    import json
    assert ev["state"] == "end" and ev["pi"] == "0xE224" and ev["duration_sec"] == 60
    assert json.loads(ev["radiotext"]) == ["Olycka på E18"]


def test_an_emergency_that_ends_on_the_other_pi_ends(store, engine):
    flip(engine, "0xE824", "2026-11-27T10:00:00", prog_type="News")
    flip(engine, "0xE824", "2026-11-27T10:00:05", prog_type="Alarm")
    flip(engine, "0xE224", "2026-11-27T10:01:05", prog_type="Pop music")
    [ev] = store.query_events()[0]
    assert ev["type"] == "emergency" and ev["state"] == "end"


def test_a_station_gone_silent_ends_its_announcement(store, engine):
    flip(engine, "0xE824", "2026-11-27T10:00:00", ta=False)
    flip(engine, "0xE824", "2026-11-27T10:00:05", ta=True)
    assert engine.engine.end_silent(200)
    [ev] = store.query_events()[0]
    assert ev["state"] == "end"
    assert not engine.engine.busy()
