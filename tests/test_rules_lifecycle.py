"""The satellite's core, end to end: decoded RDS groups in, stored events
and uplink outbox rows out. This must keep working when the satellite is
made headless (plan phase 3), whatever else is removed around it."""

import pytest

import rds_guard as rg


@pytest.fixture
def engine(store, monkeypatch):
    store.enable_outbox()
    monkeypatch.setattr(rg, "state", rg.StationState())
    monkeypatch.setattr(rg, "broadcast_ws", lambda msg: None)
    return rg.RulesEngine(recorder=None, frequency="103.5M")


PI = "0xE824"


def group(engine, ts, **fields):
    rg.process_group(None, {"pi": PI, "timestamp": ts, **fields}, _rules_engine=engine)


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
