"""uplink: delivery order, payload contents and error classification."""

import pytest

import uplink as up


class FakeResponse:
    def __init__(self, status, from_aggregator=True, text=""):
        self.status_code = status
        self.headers = {up.PROTOCOL_HEADER: "1"} if from_aggregator else {}
        self.text = text


class FakeHttp:
    """Records requests; answers from a queue (default 200)."""

    def __init__(self):
        self.calls = []
        self.answers = []

    def request(self, method, url, json=None, data=None, headers=None,
                timeout=None, allow_redirects=True):
        self.calls.append({"method": method, "url": url, "json": json,
                           "data": data, "headers": headers})
        if self.answers:
            a = self.answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        return FakeResponse(200)


@pytest.fixture
def link(store, tmp_path):
    http = FakeHttp()
    u = up.Uplink("https://agg.example/", "tok123", tmp_path, node_name="Test",
                  version="1.2.3", http=http)
    store.enable_outbox(u.notify)
    u.http = http
    return u


def _drain(u):
    outcomes = []
    while True:
        o = u.process_one()
        if o is None:
            return outcomes
        outcomes.append(o)
        if o not in (up.OK, up.DROP):
            return outcomes


def _traffic_event(store, tmp_path):
    event_id = store.insert_event(
        event_type="traffic", severity="warning", state="start", pi="0x9E04",
        data_payload={"prog_type": "Information",
                      "station": {"pi": "0x9E04", "long_ps": "P4 Test Long"}},
        station_ps="P4 Test", frequency="103.3M",
        started_at="2026-11-25T10:00:00.123")
    store.update_event_radiotext(event_id, ["Olycka på E4"])
    store.end_event(event_id, "2026-11-25T10:01:00", 60, ["Olycka på E4"])
    (tmp_path / f"{event_id}.ogg").write_bytes(b"OggS" + b"\0" * 100)
    store.update_event_audio(event_id, f"{event_id}.ogg")
    store.update_event_transcription(event_id, "secret text", status="done")
    return event_id


def test_full_event_is_sent_once_then_audio(store, link, tmp_path):
    event_id = _traffic_event(store, tmp_path)
    uid = store.get_event(event_id)["event_uid"]

    assert _drain(link) == [up.OK, up.OK]
    calls = link.http.calls
    assert [c["method"] for c in calls] == ["PUT", "PUT"]
    assert calls[0]["url"] == f"https://agg.example/api/v1/ingest/events/{uid}"
    assert calls[1]["url"].endswith(f"/events/{uid}/audio")

    body = calls[0]["json"]
    assert body["state"] == "end"
    assert body["radiotext"] == ["Olycka på E4"]
    assert body["started_at"] == "2026-11-25T10:00:00Z"
    assert body["ended_at"] == "2026-11-25T10:01:00Z"
    assert body["long_ps"] == "P4 Test Long"
    assert body["prog_type"] == "Information"
    assert body["rev"] > 0
    # Raw data only — never the transcription
    assert "secret text" not in repr(body)
    assert not any("transcri" in k for k in body)

    hdrs = calls[0]["headers"]
    assert hdrs["Authorization"] == "Bearer tok123"
    assert hdrs[up.PROTOCOL_HEADER] == "1"
    assert calls[1]["data"].startswith(b"OggS")
    assert calls[1]["headers"]["Content-Type"] == "audio/ogg"
    assert store.outbox_depth() == 0


def test_network_error_keeps_queue(store, link, tmp_path):
    _traffic_event(store, tmp_path)
    depth = store.outbox_depth()
    link.http.answers = [OSError("no route")]
    assert link.process_one() == up.RETRY
    assert store.outbox_depth() == depth
    assert link.snapshot()["state"] == "offline"


def test_server_error_and_rate_limit_retry(store, link, tmp_path):
    _traffic_event(store, tmp_path)
    for status in (500, 503, 429):
        link.http.answers = [FakeResponse(status)]
        assert link.process_one() == up.RETRY


def test_proxy_error_never_drops(store, link, tmp_path):
    """A 4xx from nginx/Cloudflare (no protocol header) must not lose data."""
    _traffic_event(store, tmp_path)
    depth = store.outbox_depth()
    link.http.answers = [FakeResponse(404, from_aggregator=False)]
    assert link.process_one() == up.RETRY
    assert store.outbox_depth() == depth


def test_unauthorized_keeps_queue(store, link, tmp_path):
    _traffic_event(store, tmp_path)
    depth = store.outbox_depth()
    link.http.answers = [FakeResponse(401)]
    assert link.process_one() == up.UNAUTHORIZED
    assert store.outbox_depth() == depth
    assert link.snapshot()["state"] == "unauthorized"


def test_incompatible_protocol_holds(store, link, tmp_path):
    _traffic_event(store, tmp_path)
    link.http.answers = [FakeResponse(426)]
    assert link.process_one() == up.INCOMPATIBLE
    assert link.snapshot()["state"] == "incompatible"


def test_rejected_item_is_dropped(store, link, tmp_path):
    _traffic_event(store, tmp_path)
    link.http.answers = [FakeResponse(422, text="bad")]
    assert link.process_one() == up.DROP
    # The event snapshot is gone, the audio upload is next
    assert store.outbox_head()["kind"] == "audio"


def test_short_event_delete_is_forwarded(store, link):
    event_id = store.insert_event(
        event_type="traffic", severity="warning", state="start", pi="0x1",
        data_payload={}, started_at="2026-11-25T10:00:00")
    uid = store.get_event(event_id)["event_uid"]
    store.delete_event(event_id)
    link.http.answers = [FakeResponse(200), FakeResponse(404)]
    assert _drain(link) == [up.OK, up.OK]
    assert [c["method"] for c in link.http.calls] == ["DELETE"]
    assert link.http.calls[0]["url"].endswith(f"/events/{uid}")
    assert store.outbox_depth() == 0


def test_missing_audio_file_is_skipped(store, link, tmp_path):
    event_id = _traffic_event(store, tmp_path)
    (tmp_path / f"{event_id}.ogg").unlink()
    assert _drain(link) == [up.OK, up.OK]
    assert len(link.http.calls) == 1   # only the event snapshot


def test_heartbeat_payload(store, tmp_path):
    http = FakeHttp()
    u = up.Uplink("https://agg.example", "t", tmp_path, node_name="Kalle",
                  version="9", http=http,
                  status_fn=lambda: {"uptime_sec": 5, "stations": [{"pi": "0x1"}]})
    assert u.send_heartbeat() == up.OK
    call = http.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://agg.example/api/v1/ingest/heartbeat"
    body = call["json"]
    assert body["node_name"] == "Kalle"
    assert body["version"] == "9"
    assert body["stations"] == [{"pi": "0x1"}]
    assert body["queue_depth"] == 0
    assert body["sent_at"].endswith("Z")


def test_utc_z():
    assert up.utc_z("2026-11-25T10:00:00") == "2026-11-25T10:00:00Z"
    assert up.utc_z("2026-11-25T10:00:00.123456") == "2026-11-25T10:00:00Z"
    assert up.utc_z(None) is None
    assert up.utc_z("garbage") is None
