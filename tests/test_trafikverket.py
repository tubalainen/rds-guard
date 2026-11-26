"""Trafikverket's deviations: request, parsing, storage, feed and API.

The responses are written in the shape of the open API (Situation 1.6,
data.json); no test calls the network.
"""

import json
import threading

import pytest

import trafikverket as trv

KARLSTAD = (59.38091, 13.50276)


def deviation(dev_id="D1", **kw):
    d = {
        "Id": dev_id,
        "MessageType": "Olycka",
        "MessageTypeValue": "Accident",
        "MessageCode": "Olycka",
        "Header": "Olycka",
        "Message": "Olycka med två personbilar.",
        "LocationDescriptor": "E18 från Trafikplats Bergvik (14) till "
                              "Trafikplats Våxnäs (15)",
        "RoadNumber": "E18",
        "RoadNumberNumeric": 18,
        "AffectedDirectionValue": "OneDirection",
        "AffectedDirection": "En riktning",
        "SeverityCode": 4,
        "SeverityText": "Stor påverkan",
        "TrafficRestrictionType": "Körfält blockerat",
        "NumberOfLanesRestricted": 1,
        "CountyNo": 17,
        "StartTime": "2026-11-26T13:02:00.000+01:00",
        "EndTime": "2026-11-26T15:00:00.000+01:00",
        "CreationTime": "2026-11-26T13:05:12.000+01:00",
        "VersionTime": "2026-11-26T13:05:12.000+01:00",
        "Geometry": {
            "Point": {"WGS84": "POINT (13.43247 59.379 0)"},
            "Line": {"WGS84": "LINESTRING (13.43 59.379 0, 13.44 59.3795 0, "
                              "13.46373 59.38152 0)"},
        },
    }
    d.update(kw)
    return d


def situation(sit_id="S1", devs=None, deleted=False):
    s = {"Id": sit_id, "Deviation": devs if devs is not None else [deviation()]}
    if deleted:
        s["Deleted"] = True
    return s


def response(situations, lastchangeid="100"):
    return {"RESPONSE": {"RESULT": [{"Situation": situations,
                                     "INFO": {"LASTCHANGEID": lastchangeid}}]}}


@pytest.fixture
def tstore(store):
    """The satellite's TrvStore on a fresh database."""
    return store.trv


# ------------------------------------------------------------------ request

def test_request_filters_area_and_types_and_escapes_the_key():
    xml = trv.build_request('a"b<c', [KARLSTAD], 150, changeid=42)
    assert 'authenticationkey="a&quot;b&lt;c"' in xml
    assert 'objecttype="Situation"' in xml and 'schemaversion="1.6"' in xml
    assert 'changeid="42"' in xml and 'includedeletedobjects="true"' in xml
    assert ('<WITHIN name="Deviation.Geometry.Point.WGS84" shape="center" '
            'value="13.50276 59.38091" radius="150000m"/>') in xml
    assert '<EQ name="Deviation.MessageType" value="Olycka"/>' in xml
    assert "Vägarbete" not in xml


def test_request_with_several_centers_uses_or():
    xml = trv.build_request("k", [KARLSTAD, (59.27, 15.21)], 100)
    assert xml.count("<WITHIN") == 2 and xml.count("<OR>") == 2


def test_nearby_centers_are_merged():
    near = (59.39, 13.52)
    far = (59.27, 15.21)
    assert trv.merge_centers([KARLSTAD, near, far, None], 150) == [
        (59.3809, 13.5028), (59.27, 15.21)]


# ------------------------------------------------------------------ parsing

def test_wkt_and_times():
    assert trv.parse_point("POINT (13.43247 59.379 0)") == (59.379, 13.43247)
    assert trv.parse_point("POINT Z (13.4 59.3 0)") == (59.3, 13.4)
    assert trv.parse_point("garbage") is None
    assert trv.parse_line("LINESTRING (13.1 59.1 0, 13.2 59.2 0)") == [
        (59.1, 13.1), (59.2, 13.2)]
    assert trv.parse_time("2026-11-26T13:05:12.000+01:00") == "2026-11-26T12:05:12"
    assert trv.parse_time("2026-07-01T13:00:00+02:00",
                          "%Y-%m-%dT%H:%M:%SZ") == "2026-07-01T11:00:00Z"
    assert trv.parse_time(None) is None and trv.parse_time("nej") is None


def test_api_error_is_raised():
    bad = {"RESPONSE": {"RESULT": [{"ERROR": {
        "SOURCE": "Security", "MESSAGE": "Invalid authentication"}}]}}
    with pytest.raises(trv.ApiError, match="Invalid authentication"):
        trv.parse_response(bad)
    with pytest.raises(trv.ApiError):
        trv.parse_response({"nope": 1})


def test_normalize_reads_the_fields():
    rec = trv.normalize(deviation(), "S1")
    assert rec["lat"] == 59.379 and rec["lon"] == 13.43247
    assert rec["direction"] == "one" and rec["road_numeric"] == 18
    assert rec["severity_code"] == 4 and rec["lanes"] == 1
    assert json.loads(rec["line"])[0] == [59.379, 13.43]
    assert rec["status"] == "active"
    assert trv.normalize(deviation(Suspended=True), "S1")["status"] == "suspended"


def test_point_falls_back_to_the_middle_of_the_line():
    dev = deviation()
    del dev["Geometry"]["Point"]
    rec = trv.normalize(dev, "S1")
    assert (rec["lat"], rec["lon"]) == (59.3795, 13.44)


# ------------------------------------------------------------------ storage

def test_new_deviation_and_its_updates_make_a_timeline(tstore):
    assert tstore.apply([situation()]) == ["D1"]
    # The same version again changes nothing
    assert tstore.apply([situation()]) == []
    tstore.apply([situation(devs=[deviation(
        Message="Olycka med två personbilar. Bärgning pågår.",
        EndTime="2026-11-26T14:30:00.000+01:00",
        VersionTime="2026-11-26T13:40:00.000+01:00")])])
    d = tstore.get("D1")
    assert d["message"].endswith("Bärgning pågår.")
    first, second = d["versions"]
    assert first["changes"]["severity_text"] == "Stor påverkan"
    assert set(second["changes"]) == {"message", "end_time"}
    assert second["version_time"] == "2026-11-26T12:40:00"


def test_removed_situation_and_vanished_deviation_are_marked_deleted(tstore):
    tstore.apply([situation(devs=[deviation("D1"), deviation("D2")])])
    # D2 is no longer part of the situation
    assert tstore.apply([situation(devs=[deviation("D1")])]) == ["D2"]
    assert tstore.get("D2")["status"] == "deleted"
    # The whole situation is removed
    assert tstore.apply([situation(deleted=True)]) == ["D1"]
    assert tstore.get("D1")["versions"][-1]["changes"] == {"status": "deleted"}
    assert tstore.count_active() == 0


def test_road_works_are_not_kept(tstore):
    tstore.apply([situation(devs=[deviation("V1", MessageType="Vägarbete")])])
    assert tstore.get("V1") is None


def test_full_refresh_drops_what_is_no_longer_there(tstore):
    tstore.apply([situation("S1"), situation("S2", [deviation("D2")])])
    assert tstore.apply([situation("S1")], full=True) == ["D2"]
    assert tstore.get("D2")["status"] == "deleted"
    assert tstore.get("D1")["status"] == "active"


def test_list_marks_ended_deviations(tstore):
    tstore.apply([situation(devs=[
        deviation("OLD", EndTime="2020-01-01T10:00:00+01:00"),
        deviation("OPEN", EndTime="2020-01-01T10:00:00+01:00",
                  ValidUntilFurtherNotice=True),
        deviation("NOW", EndTime="2099-01-01T10:00:00+01:00")])])
    rows = {d["id"]: d for d in tstore.list()}
    assert rows["OLD"]["ended"] and not rows["OPEN"]["ended"]
    assert not rows["NOW"]["ended"]
    assert {d["id"] for d in tstore.list(include_ended=False)} == {"OPEN", "NOW"}


def test_purge_with_old_events(store):
    store.trv.apply([situation()])
    store._conn().execute("UPDATE trv_deviations SET last_seen = '2000-01-01'")
    store._conn().commit()
    store.purge_old_events(30)
    assert store.trv.get("D1") is None
    assert store.trv.versions("D1") == []


# ------------------------------------------------------------------ feed

class FakeApi:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.bodies = []

    def __call__(self, url, body):
        self.bodies.append(body)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def test_feed_starts_from_zero_then_asks_for_changes(tstore):
    api = FakeApi(response([situation()], "100"), response([], "105"))
    seen = []
    feed = trv.TrafikverketFeed("k", tstore, lambda: [KARLSTAD],
                                on_change=seen.append, post=api)
    assert feed.poll_once() and feed.poll_once()
    assert 'changeid="0"' in api.bodies[0]
    assert 'changeid="100"' in api.bodies[1]
    assert seen == [["D1"]]
    assert tstore.get_state("lastchangeid") == "105"
    snap = feed.snapshot()
    assert snap["enabled"] and snap["polls"] == 2 and snap["active"] == 1


def test_feed_restarts_from_zero_when_the_area_changes(tstore):
    center = [KARLSTAD]
    api = FakeApi(response([situation()], "100"), response([], "7"))
    feed = trv.TrafikverketFeed("k", tstore, lambda: center, post=api)
    feed.poll_once()
    center[0] = (59.27, 15.21)
    feed.poll_once()
    assert 'changeid="0"' in api.bodies[1]
    # The full answer lacked D1: it is gone from the new area
    assert tstore.get("D1")["status"] == "deleted"


def test_feed_waits_for_a_location(tstore):
    api = FakeApi()
    feed = trv.TrafikverketFeed("k", tstore, lambda: [], post=api)
    assert feed.poll_once() is False and api.bodies == []


def test_feed_keeps_running_after_errors(tstore):
    api = FakeApi(OSError("nätet nere"), response([situation()]))
    feed = trv.TrafikverketFeed("k", tstore, lambda: [KARLSTAD], post=api,
                                poll_sec=30)
    waits = []

    class Stop:
        def is_set(self):
            return len(waits) >= 2

        def wait(self, sec):
            waits.append(sec)

    feed._stop = Stop()
    feed._run()
    assert waits == [60, 30]              # backed off after the error, then normal
    assert tstore.get("D1") is not None
    assert feed.snapshot()["last_error"] is None


def test_no_key_no_feed(tstore):
    assert trv.create_feed("", tstore, lambda: [KARLSTAD]) is None


# ------------------------------------------------------------------ API

async def test_satellite_api_and_status(aiohttp_client, store, monkeypatch):
    import rds_guard as rg
    import web_server
    store.trv.apply([situation()])
    feed = trv.TrafikverketFeed("k", store.trv, lambda: [KARLSTAD],
                                post=FakeApi())
    monkeypatch.setattr(rg, "trv_feed", feed)
    client = await aiohttp_client(web_server.create_app())
    data = await (await client.get("/api/trafikverket")).json()
    [d] = data["deviations"]
    assert d["id"] == "D1" and d["versions"][0]["changes"]["road_number"] == "E18"
    assert data["trafikverket"]["enabled"] is True
    monkeypatch.setattr(rg, "trv_feed", None)
    data = await (await client.get("/api/trafikverket")).json()
    assert data["trafikverket"]["enabled"] is False
