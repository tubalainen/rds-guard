"""Stations are frequency + PI: two P4 regions sharing a PI (P4 Örebro and
P4 Värmland both send 0xE224) stay apart, each with its name and area."""

import gzip
import time
import uuid

import pytest

import geo_service
import geolocate
from aggregator import config, store
from aggregator.pipeline import Pipeline, alert_payload
from notify_rules import headline, matches, validate

T = time.time() - 600


def _iso(epoch):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def ev(freq, start=T, pi="0xE224", **kw):
    d = {"rev": 1, "type": "traffic", "state": "start", "pi": pi,
         "station_ps": "P4 SR", "long_ps": None, "frequency": freq,
         "prog_type": None, "radiotext": [], "started_at": _iso(start),
         "ended_at": None, "duration_sec": None, "sent_at": _iso(start),
         "receiver": "0"}
    d.update(kw)
    return d


def put(node, body, uid=None):
    return store.upsert_event(node["id"], uid or str(uuid.uuid4()), body,
                              config.MATCH_WINDOW_SEC,
                              config.BROADCAST_END_GRACE_SEC)


@pytest.fixture
def node(agg_store):
    n, _ = agg_store.create_node("Hemma")
    return n


def count_events():
    return store._conn().execute("SELECT COUNT(*) FROM events").fetchone()[0]


def test_station_key():
    assert store.station_key("102.8M", "0xe224") == "102800/E224"
    assert store.station_key("102800000", "E224") == "102800/E224"
    assert store.station_key("102.8", "E224") == "102800/E224"
    assert store.station_key(None, "0xE224") == "E224"


def test_same_pi_on_two_frequencies_is_two_broadcasts(node):
    _, _, a = put(node, ev("103.5M"))
    _, _, b = put(node, ev("102.8M", start=T + 2))
    assert count_events() == 2
    assert a["station_key"] == "103500/E224" and b["station_key"] == "102800/E224"


def test_same_station_from_two_nodes_is_one_broadcast(agg_store, node):
    other, _ = agg_store.create_node("Lisa")
    put(node, ev("102.8M"))
    put(other, ev("102.8M", start=T + 3))
    assert count_events() == 1


def test_name_from_radiotext_and_admin(node):
    _, _, row = put(node, ev("102.8M", radiotext=["Du lyssnar på P4 Örebro"]))
    assert row["station_name"] == "P4 Örebro"
    _, _, row = put(node, ev("103.5M", start=T + 1, radiotext=["P4 Extra med Lotta"]))
    assert row["station_name"] == "P4 SR"          # a programme, not a region
    assert store.set_station("103500/E224", "P4 Värmland", "Värmlands län")
    assert store.get_event(row["event_uid"])["station_name"] == "P4 Värmland"
    st = {s["station_key"]: s for s in store.list_stations()}
    assert st["103500/E224"]["area"] == "Värmlands län"
    assert st["102800/E224"]["display_name"] == "P4 Örebro"


def test_long_ps_wins_over_radiotext():
    assert store.rds_station_name("P4 SR", "P4 Örebro", ["P4 Värmland"]) == "P4 Örebro"
    assert store.rds_station_name("P4 SR", "P4 SR", []) is None


def test_heartbeat_registers_stations(agg_store, node):
    agg_store.record_heartbeat(node["id"], "Hemma", "1.0", {"stations": [
        {"frequency": "102.8M", "pi": "0xE224", "ps": "P4 SR"}]}, 0)
    assert [s["station_key"] for s in store.list_stations()] == ["102800/E224"]
    [named] = store.name_stations([{"frequency": "102.8M", "pi": "0xE224",
                                    "ps": "P4 SR"}])
    assert named["station_name"] == "P4 SR"


def test_events_filter_by_station(node):
    put(node, ev("103.5M"))
    put(node, ev("102.8M", start=T + 2))
    rows, total = store.query_events(station="102800/E224")
    assert total == 1 and rows[0]["frequency"] == "102.8M"
    assert store.query_events(station="E224")[1] == 2


def test_rule_for_one_station(node):
    rule = validate({"name": "Örebro", "event_types": ["traffic"],
                           "stations": ["102800/0xe224"], "topic": "orebro"})
    assert rule["stations"] == ["102800/E224"]
    _, _, a = put(node, ev("103.5M", state="end"))
    _, _, b = put(node, ev("102.8M", start=T + 2, state="end"))
    assert not matches(rule, a, "Europe/Stockholm")
    assert matches(rule, b, "Europe/Stockholm")
    old = validate({"name": "Alla", "event_types": ["traffic"],
                          "stations": ["E224"], "topic": "alla"})
    assert matches(old, a, "Europe/Stockholm") and matches(old, b, "Europe/Stockholm")


def test_suggest_area():
    assert store.suggest_area("P4 Örebro") == "Örebro län"
    assert store.suggest_area("P4 Värmland") == "Värmlands län"


@pytest.fixture
def geo(tmp_path):
    path = tmp_path / "gaz.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("Karlstad\tcity\t59.38091\t13.50276\tS\t\n"
                "Örebro\tcity\t59.27412\t15.2066\tT\t\n"
                "Kumla\ttown\t59.1277\t15.1434\tT\t\n")
    return geo_service.GeoService(geolocate.create_geolocator("rules"),
                                  gazetteer_path=str(path))


def test_place_of_uses_station_area(agg_store, node, geo):
    agg_store.set_node_location(node["id"], "Karlstad, Värmlands län")
    _, _, row = put(node, ev("102.8M"))
    pipe = Pipeline(transcriber=None, mqtt=None, geo=geo)
    place, label = pipe.place_of(row)
    assert place.name == "Karlstad"                 # no area yet: the node
    store.set_station("102800/E224", "P4 Örebro", "Örebro län")
    place, label = pipe.place_of(store.get_event(row["event_uid"]))
    assert place.name == "Örebro" and label == "Örebro län"


def test_payloads_carry_the_name(node):
    _, _, row = put(node, ev("102.8M", state="end",
                             radiotext=["Trafik i P4 Örebro"]))
    payload = alert_payload(row, "none")
    assert payload["station"]["name"] == "P4 Örebro"
    assert payload["station"]["key"] == "102800/E224"
    assert headline(dict(row, radiotext=[])) == "Trafikmeddelande på P4 Örebro"


def test_rds_name_only_from_the_p4_regions():
    assert store.rds_station_name("P4 SR", None, ["DU LYSSNAR PÅ P4 ÖREBRO"]) == "P4 Örebro"
    assert store.rds_station_name("P4 SR", None, ["P4 Granskar: ny vecka"]) is None
    assert store.rds_station_name("P4 SR", None, ["P4 Västerås"]) is None
    assert store.rds_station_name("P4 SR", None, ["Nu P4 Västmanland"]) == "P4 Västmanland"


def test_rds_name_is_learned_once(node):
    put(node, ev("102.8M", radiotext=["Du lyssnar på P4 Örebro"]))
    put(node, ev("102.8M", start=T + 3600, radiotext=["Nu sänder P4 Värmland och P4 Örebro"]))
    assert store.get_station("102800/E224")["rds_name"] == "P4 Örebro"


def test_suggest_area_without_a_region_is_none_and_fast():
    t = time.time()
    assert store.suggest_area("P4 SR") is None
    assert store.suggest_area("P4 Skaraborg") == "Skövde, Västra Götalands län"
    assert time.time() - t < 0.05


def test_older_events_get_their_station(agg_store, node, tmp_path):
    put(node, ev("102.8M"))
    put(node, ev("103.5M", start=T + 2))
    conn = store._conn()
    conn.execute("UPDATE events SET station_key = NULL")
    conn.execute("DELETE FROM stations")
    conn.commit()
    store.init_db(store._DB_PATH)
    keys = sorted(r[0] for r in conn.execute("SELECT station_key FROM events"))
    assert keys == ["102800/E224", "103500/E224"]
    assert sorted(s["station_key"] for s in store.list_stations()) == keys


@pytest.fixture
async def web(aiohttp_client, agg_store, monkeypatch):
    from aggregator import api
    monkeypatch.setattr(config, "ADMIN_PASSWORD", "hemligt")
    client = await aiohttp_client(api.create_web_app())
    return client


async def test_station_settings_api(web, node):
    put(node, ev("102.8M"))
    put(node, ev("103.5M", start=T + 2))
    body = {"key": "102800/E224", "name": "P4 Örebro", "area": "Örebro län"}
    assert (await web.patch("/api/v1/station-settings", json=body)).status == 401
    await web.post("/api/v1/admin/login", json={"password": "hemligt"})
    resp = await web.patch("/api/v1/station-settings", json=body)
    assert resp.status == 200
    st = (await resp.json())["station"]
    assert st["display_name"] == "P4 Örebro" and st["area"] == "Örebro län"
    resp = await web.patch("/api/v1/station-settings", json=dict(body, key="1/X"))
    assert resp.status == 404
    data = await (await web.get("/api/v1/events?station=102800/E224")).json()
    assert [e["station_name"] for e in data["events"]] == ["P4 Örebro"]
    data = await (await web.get("/api/v1/stations")).json()
    assert {s["station_key"]: s["display_name"] for s in data["stations"]} == {
        "102800/E224": "P4 Örebro", "103500/E224": "P4 SR"}


def test_cut_short_ps_names_its_p4_region():
    assert store.rds_station_name("P4 Värml") == "P4 Värmland"
    assert store.rds_station_name("P4 Örebr") == "P4 Örebro"
    assert store.rds_station_name("P4 Väst") == "P4 Väst"
    assert store.rds_station_name("P4 Väster") is None      # two regions
    assert store.rds_station_name("P4 SR") is None
    assert store.rds_station_name("P4 Extra") is None
    assert store.suggest_area("P4 Värml") == "Värmlands län"


def test_shared_programme_pi_takes_the_name_of_its_frequency(node):
    # 103.5 in the regional hours (0xE824 "P4 Värml"), and in the evening
    # with the shared P4 programme (0xE224 "P4 SR")
    put(node, ev("103.5M", pi="0xE824", station_ps="P4 Värml"))
    _, _, row = put(node, ev("103.5M", start=T + 3600))
    put(node, ev("100.3M", start=T + 3700))
    st = {s["station_key"]: s for s in store.list_stations()}
    assert st["103500/E824"]["display_name"] == "P4 Värmland"
    assert st["103500/E224"]["display_name"] == "P4 Värmland"
    assert st["103500/E224"]["inherited_from"] == "103500/E824"
    assert st["100300/E224"]["display_name"] == "P4 SR"     # nothing to go by
    store.set_station("103500/E824", None, "Värmlands län")
    assert store.with_station_name(dict(row))["station_area"] == "Värmlands län"
    assert store.list_stations()[1]["suggested_area"] is None
    # A name of its own wins
    store.set_station("103500/E224", "P4 Riks", None)
    assert store.get_station("103500/E224")["inherited_name"] is None
    assert store.with_station_name(dict(row))["station_name"] == "P4 Riks"


def test_two_names_on_one_frequency_are_not_guessed(node):
    put(node, ev("102.8M", pi="0xE524", station_ps="P4 Örebr"))
    put(node, ev("102.8M", pi="0xE624", station_ps="P4 Värml", start=T + 60))
    put(node, ev("102.8M", start=T + 3600))
    assert store.get_station("102800/E224")["inherited_name"] is None


def test_older_cut_short_ps_is_learned_at_start(agg_store, node):
    put(node, ev("103.5M", pi="0xE824", station_ps="P4 Värml"))
    conn = store._conn()
    conn.execute("UPDATE stations SET rds_name = NULL")
    conn.commit()
    store.init_db(store._DB_PATH)
    assert store.get_station("103500/E824")["rds_name"] == "P4 Värmland"
