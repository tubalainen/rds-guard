"""event_store: event_uid and the uplink outbox."""

import uuid


def _new_traffic(store):
    return store.insert_event(
        event_type="traffic", severity="warning", state="start",
        pi="0x9E04", data_payload={"prog_type": "Information"},
        station_ps="P4 Test", frequency="103.3M",
        started_at="2026-11-25T10:00:00")


def _kinds(store):
    conn = store._conn()
    return [r["kind"] for r in conn.execute("SELECT kind FROM outbox ORDER BY id")]


def test_standalone_queues_nothing(store):
    event_id = _new_traffic(store)
    store.end_event(event_id, "2026-11-25T10:01:00", 60)
    store.update_event_audio(event_id, f"{event_id}.ogg")
    assert store.outbox_depth() == 0
    # event_uid is assigned in every mode
    uid = store.get_event(event_id)["event_uid"]
    assert str(uuid.UUID(uid)) == uid


def test_lifecycle_is_queued_in_order(store):
    woke = []
    store.enable_outbox(lambda: woke.append(1))
    event_id = _new_traffic(store)
    store.update_event_radiotext(event_id, ["Olycka på E4"])
    store.end_event(event_id, "2026-11-25T10:01:00", 60, ["Olycka på E4"])
    store.update_event_audio(event_id, f"{event_id}.ogg")
    assert _kinds(store) == ["event", "event", "event", "audio"]
    assert len(woke) == 4


def test_transcription_and_housekeeping_not_queued(store):
    store.enable_outbox()
    event_id = _new_traffic(store)
    base = store.outbox_depth()
    store.update_event_transcription_status(event_id, "transcribing")
    store.update_event_transcription(event_id, "text", status="done")
    assert store.outbox_depth() == base
    store.delete_all_events()
    assert store.outbox_depth() == base


def test_delete_is_queued_with_uid(store):
    store.enable_outbox()
    event_id = _new_traffic(store)
    uid = store.get_event(event_id)["event_uid"]
    assert store.delete_event(event_id)
    conn = store._conn()
    last = dict(conn.execute("SELECT * FROM outbox ORDER BY id DESC LIMIT 1").fetchone())
    assert last["kind"] == "delete"
    assert last["event_uid"] == uid


def test_close_stale_events_is_queued(store):
    event_id = _new_traffic(store)          # created before uplink was enabled
    store.enable_outbox()
    assert store.close_stale_events() == 1
    assert _kinds(store) == ["event"]
    assert store.outbox_head()["event_id"] == event_id


def test_rows_without_uid_are_skipped(store):
    """Events from before the upgrade have no event_uid — nothing to send."""
    event_id = _new_traffic(store)
    conn = store._conn()
    conn.execute("UPDATE events SET event_uid = NULL WHERE id = ?", (event_id,))
    conn.commit()
    store.enable_outbox()
    store.end_event(event_id, "2026-11-25T10:01:00", 60)
    assert store.outbox_depth() == 0


def test_ack_event_collapses_snapshots(store):
    store.enable_outbox()
    event_id = _new_traffic(store)
    store.update_event_radiotext(event_id, ["a"])
    rev = store.outbox_latest_event_rev(event_id)
    store.end_event(event_id, "2026-11-25T10:01:00", 60)  # after the watermark
    store.outbox_ack_event(event_id, rev)
    assert _kinds(store) == ["event"]       # the later change is still queued


def test_purge_old_rows(store):
    store.enable_outbox()
    _new_traffic(store)
    conn = store._conn()
    conn.execute("UPDATE outbox SET created_at = '2000-01-01T00:00:00'")
    conn.commit()
    assert store.outbox_purge(72) == 1
    assert store.outbox_depth() == 0


def test_migration_is_idempotent(store, tmp_path):
    store._local.conn = None
    store.init_db(str(tmp_path / "events.db"))   # second run on same file
    assert store.outbox_depth() == 0
