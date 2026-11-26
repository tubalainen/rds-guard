"""Radio incidents linked to Trafikverket's deviations: matching, the map
layer, the timeline and push messages for updates."""

import time

import pytest

import geolocate as geo
import notifier
import notify_rules
import trafikverket as trv

NOW = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def iso(offset_min=0, z=""):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(
        time.time() + offset_min * 60)) + z


def trv_time(offset_min=0):
    """A time as Trafikverket writes it (UTC with offset)."""
    return iso(offset_min) + "+00:00"


BERGVIK = (59.379, 13.43247)


def deviation(dev_id="D1", **kw):
    d = {
        "Id": dev_id, "MessageType": "Olycka", "MessageCode": "Olycka",
        "Message": "Olycka med två personbilar.",
        "LocationDescriptor": "E18 från Trafikplats Bergvik till Trafikplats Våxnäs",
        "RoadNumber": "E18", "RoadNumberNumeric": 18,
        "SeverityCode": 4, "SeverityText": "Stor påverkan",
        "TrafficRestrictionType": "Körfält blockerat",
        "AffectedDirectionValue": "OneDirection",
        "StartTime": trv_time(-10), "CreationTime": trv_time(-8),
        "EndTime": trv_time(90), "VersionTime": trv_time(-8),
        "Geometry": {"Point": {"WGS84": f"POINT ({BERGVIK[1]} {BERGVIK[0]} 0)"},
                     "Line": {"WGS84": "LINESTRING (13.42 59.3785 0, 13.44 59.3795 0)"}},
    }
    d.update(kw)
    return d


def situation(sit_id="S1", devs=None, deleted=False):
    s = {"Id": sit_id, "Deviation": devs if devs is not None else [deviation()]}
    if deleted:
        s["Deleted"] = True
    return s


def placement(lat=59.3795, lon=13.43, radius=500, road="E18", what="Olycka"):
    return geo.Placement(lat=lat, lon=lon, radius_m=radius, road=road,
                         label="E18 Bergviksmotet", what=what,
                         level="nära en plats", names=["Bergviksmotet"])


def radio_event(store, text="Olycka på E18 vid Bergviksmotet i riktning mot Grums."):
    eid = store.insert_event("traffic", "warning", "start", "E203", {},
                             station_ps="P4 Värml", frequency="103.5M",
                             radiotext=[], started_at=NOW)
    store.end_event(eid, NOW, 60, radiotext_list=[])
    store.update_event_transcription(eid, text)
    return eid


def place(store, eid, pl=None):
    result = geo.GeoResult("rules", [pl or placement()])
    return store.save_geolocation(eid, result, "done")


# ---------------------------------------------------------------- words

def test_road_keys():
    assert trv.road_key("E 18") == "E18" and trv.road_key("Rv 61") == "61"
    assert trv.road_key("Lv 236") == "236" and trv.road_key("ingen väg") is None
    dev = {"road_number": None, "road_numeric": None,
           "location": "Väg 924 från Valbo-Ryr till Rönningen"}
    assert trv.deviation_road(dev) == "924"
    assert trv.deviation_road({"road_number": "Väg 73"}) == "73"


def test_kinds():
    assert trv.kinds_in("Trafikolycka med två bilar") == {"accident"}
    assert "obstacle" in trv.kinds_in("Älg på vägen")
    assert trv.deviation_kinds({"message_type": "Trafikmeddelande",
                                "message_code": "Körfältsavstängningar",
                                "message": "Ledningsarbete."}) == {"work"}
    # An accident with towing work is an accident
    assert trv.deviation_kinds({"message_type": "Olycka",
                                "message": "Bärgningsarbete pågår"}) == {
        "accident", "obstacle"}


# ---------------------------------------------------------------- scoring

def inc(**kw):
    d = {"lat": 59.3795, "lon": 13.43, "radius_m": 500, "road": "E18",
         "what": "Olycka"}
    d.update(kw)
    return d


def dev_dict(**kw):
    return trv.normalize(deviation(**kw), "S1")


def test_accident_on_the_same_road_matches():
    assert trv.match_score(inc(), dev_dict(), NOW) >= trv.MIN_SCORE


def test_other_road_does_not_match():
    assert trv.match_score(inc(road="Rv 61"), dev_dict(), NOW) is None


def test_too_far_does_not_match():
    assert trv.match_score(inc(lat=59.45), dev_dict(), NOW) is None


def test_published_long_after_the_broadcast_does_not_match():
    late = dev_dict(CreationTime=trv_time(120), StartTime=trv_time(120))
    assert trv.match_score(inc(), late, NOW) is None


def test_over_before_the_broadcast_does_not_match():
    over = dev_dict(EndTime=trv_time(-90))
    assert trv.match_score(inc(), over, NOW) is None


def test_planned_works_do_not_match_an_accident():
    work = dev_dict(MessageType="Trafikmeddelande", MessageCode="Vägarbete",
                    Message="Beläggningsarbete.", CreationTime=trv_time(-60 * 24 * 10))
    assert trv.match_score(inc(), work, NOW) is None
    # ... but road works the radio talks about do
    assert trv.match_score(inc(what="Vägarbete"), work, NOW,
                           "Vägarbete på E18") >= trv.MIN_SCORE


def test_no_road_and_no_kind_is_not_enough():
    assert trv.match_score(inc(road=None, what=""), dev_dict(), NOW) is None


# ---------------------------------------------------------------- linking

def test_radio_incident_is_linked_and_moved(store):
    store.trv.apply([situation()])
    [inc_id] = place(store, radio_event(store))
    row = store.incident(inc_id)
    assert row["trv_id"] == "D1"
    assert (row["lat"], row["lon"]) == BERGVIK
    assert row["radius_m"] == trv.LINKED_RADIUS_M and row["level"] == "trafikverket"


def test_trafikverket_after_the_radio_links_too(store):
    [inc_id] = place(store, radio_event(store))
    assert store.incident(inc_id)["trv_id"] is None
    ids = store.trv.apply([situation()])
    new_links, _ = store.trv_relink(ids)
    assert new_links == {inc_id: "D1"}
    assert store.incident(inc_id)["trv_id"] == "D1"


def test_ended_by_trafikverket_turns_the_incident_grey(store):
    store.trv.apply([situation()])
    [inc_id] = place(store, radio_event(store))
    ids = store.trv.apply([situation(deleted=True)])
    _, updated = store.trv_relink(ids)
    assert updated == [inc_id]
    assert store.incident(inc_id)["status"] == "cleared"


def test_layer_leaves_out_linked_and_works(store):
    store.trv.apply([situation(devs=[
        deviation("D1"),
        deviation("ALG", MessageType="Hinder", MessageCode="Djur på vägen",
                  Message="Älg på vägen.", RoadNumber="Väg 63",
                  Geometry={"Point": {"WGS84": "POINT (13.9 59.6 0)"}}),
        deviation("WORK", MessageType="Trafikmeddelande",
                  MessageCode="Körfältsavstängningar", Message="Ledningsarbete.",
                  SeverityCode=2, Geometry={"Point": {"WGS84": "POINT (13.7 59.5 0)"}}),
    ])])
    assert {d["id"] for d in store.trv.layer()} == {"D1", "ALG"}
    place(store, radio_event(store))
    assert [d["id"] for d in store.trv.layer()] == ["ALG"]


def test_attach_gives_the_timeline(store):
    store.trv.apply([situation()])
    [inc_id] = place(store, radio_event(store))
    store.trv.apply([situation(devs=[deviation(Message="Bärgning pågår.")])])
    [row] = trv.attach(store.trv, [store.incident(inc_id)])
    t = row["trafikverket"]
    assert t["id"] == "D1" and t["message"] == "Bärgning pågår."
    assert [set(v["changes"]) >= {"message"} for v in t["versions"]] == [True, True]
    assert not t["ended"]


# ---------------------------------------------------------------- push

def test_update_text():
    dev = {"message": "Bärgning pågår", "severity_text": "Mycket stor påverkan",
           "end_time": "2026-11-26T13:30:00", "status": "active"}
    zone = notify_rules.get_zone("Europe/Stockholm")
    text = trv.update_text(dev, {"message": 1, "severity_code": 5, "end_time": 1}, zone)
    assert text == "Bärgning pågår. Mycket stor påverkan. Beräknas klart 14:30."
    assert trv.update_text({"status": "deleted"}, {"status": "deleted"}) == \
        "Avslutad enligt Trafikverket."


def test_gate_holds_back_frequent_updates_but_not_the_end():
    gate = trv.UpdateGate(interval=600)
    assert gate.worth("D1", {"message": "a"}, False, now=1000)
    assert not gate.worth("D1", {"message": "b"}, False, now=1300)
    assert not gate.worth("D2", {"version_time": "x"}, False, now=1300)
    assert gate.worth("D1", {"status": "deleted"}, True, now=1301)


@pytest.fixture
def pushed_incident(store):
    """A radio message linked to D1, pushed by the rule 'Trafik'."""
    notifier.init()
    rule = next(r for r in store.list_rules() if r["name"] == "Trafik")
    store.trv.apply([situation()])
    eid = radio_event(store)
    [inc_id] = place(store, eid)
    store.log_notification(eid, store.NOTIFY_SENT, rule=rule,
                           topic=f"notify/{rule['topic']}", title="Trafik: …")
    notifier._gate = trv.UpdateGate()
    return eid, inc_id, rule


def test_update_is_pushed_to_the_same_rule(store, pushed_incident):
    eid, inc_id, rule = pushed_incident
    sent = []
    since = store.trv.last_version_id()
    ids = store.trv.apply([situation(devs=[deviation(Message="Bärgning pågår.")])])
    n = notifier.trv_updates(ids, since, lambda t, p: sent.append((t, p)) or True)
    assert n == 1
    topic, payload = sent[0]
    assert topic == f"notify/{rule['topic']}"
    assert payload["title"] == "Trafik, uppdatering: E18 Bergviksmotet"
    assert payload["message"] == "Bärgning pågår."
    assert payload["incident_id"] == inc_id and payload["event_id"] == str(eid)
    assert payload["trafikverket"]["id"] == "D1"


def test_no_push_without_a_pushed_message(store):
    store.trv.apply([situation()])
    place(store, radio_event(store))
    since = store.trv.last_version_id()
    ids = store.trv.apply([situation(devs=[deviation(Message="Bärgning pågår.")])])
    assert notifier.trv_updates(ids, since, lambda t, p: True) == 0


def test_end_is_pushed(store, pushed_incident):
    sent = []
    since = store.trv.last_version_id()
    ids = store.trv.apply([situation(deleted=True)])
    store.trv_relink(ids)
    notifier.trv_updates(ids, since, lambda t, p: sent.append(p) or True)
    assert sent[0]["message"] == "Avslutad enligt Trafikverket."
    assert sent[0]["trafikverket"]["ended"] is True


def test_newly_linked_incident_gets_no_update(store, pushed_incident):
    _, inc_id, _ = pushed_incident
    since = store.trv.last_version_id()
    ids = store.trv.apply([situation(devs=[deviation(Message="Ny text.")])])
    assert notifier.trv_updates(ids, since, lambda t, p: True,
                                skip_incidents={inc_id}) == 0


# ---------------------------------------------------------------- API

async def test_incidents_api_carries_trafikverket(aiohttp_client, store):
    import web_server
    store.trv.apply([situation(devs=[
        deviation("D1"),
        deviation("ALG", MessageType="Hinder", MessageCode="Djur på vägen",
                  Message="Älg på vägen.", Geometry={"Point": {"WGS84": "POINT (13.9 59.6 0)"}})])])
    eid = radio_event(store)
    place(store, eid)
    client = await aiohttp_client(web_server.create_app())
    data = await (await client.get("/api/incidents")).json()
    [inc] = data["incidents"]
    assert inc["trafikverket"]["id"] == "D1" and inc["mentions"]
    assert inc["trafikverket"]["versions"]
    layer = await (await client.get("/api/trafikverket?layer=1")).json()
    assert [d["id"] for d in layer["deviations"]] == ["ALG"]
    assert layer["deviations"][0]["kinds"] == ["obstacle"]
    ev = await (await client.get(f"/api/events/{eid}")).json()
    assert ev["incidents"][0]["trafikverket"]["id"] == "D1"


def test_current_deviation_keeps_the_incident_on_the_map(store):
    store.trv.apply([situation()])
    [inc_id] = place(store, radio_event(store))
    conn = store._conn()
    conn.execute("UPDATE incidents SET last_seen = '2000-01-01T00:00:00' "
                 "WHERE id = ?", (inc_id,))
    conn.commit()
    assert store.trv.touch_linked() == 1
    assert store.incident(inc_id)["last_seen"] > "2026"
    # Not when Trafikverket has ended it
    store.trv.apply([situation(devs=[deviation(EndTime=trv_time(-60))])])
    conn.execute("UPDATE incidents SET last_seen = '2000-01-01T00:00:00' "
                 "WHERE id = ?", (inc_id,))
    conn.commit()
    assert store.trv.touch_linked() == 0
