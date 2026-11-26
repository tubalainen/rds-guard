"""The measuring tool for the map engines (tools/eval_geolocation.py)."""

import gzip

import pytest

import gazetteer as gz
import geolocate as geo
from tools import eval_geolocation as ev

KARLSTAD = (59.38091, 13.50276)
PLACES = [
    ("Karlstad", "city", 59.38091, 13.50276, "S"),
    ("Bergviksmotet", "junction", 59.379, 13.43247, "S"),
    ("Väse", "village", 59.38448, 13.85288, "S"),
    ("Skattkärr", "town", 59.41338, 13.67437, "S"),
]


@pytest.fixture
def reg(tmp_path):
    path = tmp_path / "gaz.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for name, kind, lat, lon, county in PLACES:
            f.write(f"{name}\t{kind}\t{lat}\t{lon}\t{county}\t\n")
    return gz.Gazetteer.load(str(path))


def item(incident, kind, near, road="E18", km=2):
    return {"incident": incident, "kind": kind, "road": road, "near": near, "km": km}


def corpus(*cases):
    return {"node_location": "Karlstad, Värmlands län", "cases": list(cases)}


def case(cid, at, text, expect):
    return {"id": cid, "at": at, "text": text, "expect": expect}


def evaluate(c, reg):
    runs = ev.run(c, geo.Geolocator(None), reg, KARLSTAD)
    return runs, ev.score(runs, reg, KARLSTAD)


def test_messages_about_one_incident_are_linked(reg):
    c = corpus(
        case("a", "2026-11-26T10:00:00", "Olycka på E18 vid Bergviksmotet.",
             [item("berg", "new", ["Bergviksmotet"])]),
        case("b", "2026-11-26T10:30:00", "Fortfarande köer på E18 vid Bergviksmotet.",
             [item("berg", "update", ["Bergviksmotet"])]),
        case("c", "2026-11-26T11:00:00", "E18 vid Bergviksmotet är öppen igen.",
             [item("berg", "cleared", ["Bergviksmotet"])]),
    )
    runs, s = evaluate(c, reg)
    assert [p.group for r in runs for p in r.predicted] == ["new", "linked", "cleared"]
    assert s["found"] == s["expected"] == 3
    assert s["kind_ok"] == s["kind_scored"] == 3
    assert (s["linked_ok"], s["link_pairs"], s["merged_wrong"]) == (3, 3, 0)


def test_different_incidents_merged_together_are_counted(reg):
    # The facit says two incidents; the engine places both at one junction
    c = corpus(
        case("a", "2026-11-26T10:00:00", "Olycka på E18 vid Bergviksmotet.",
             [item("one", "new", ["Bergviksmotet"])]),
        case("b", "2026-11-26T10:30:00", "Lastbil stillastående på E18 vid Bergviksmotet.",
             [item("two", "new", ["Bergviksmotet"])]),
    )
    _, s = evaluate(c, reg)
    assert s["merged_wrong"] == 1 and s["link_pairs"] == 0
    assert s["kind_ok"] == 1                    # the second became "known"


def test_missed_extra_and_unlabelled_messages(reg):
    c = corpus(
        case("a", "2026-11-26T10:00:00", "Olycka på E18 vid Bergviksmotet.", None),
        case("b", "2026-11-26T10:30:00", "Älg på E18 vid Väse.",
             [item("x", "new", ["Skattkärr"], km=1)]),
    )
    runs, s = evaluate(c, reg)
    assert len(runs) == 2 and s["labelled"] == 1      # "a" runs, is not scored
    assert (s["expected"], s["found"], s["extra"]) == (1, 0, 1)


def test_another_road_does_not_match(reg):
    c = corpus(case("a", "2026-11-26T10:00:00", "Olycka på E18 vid Bergviksmotet.",
                    [item("x", "new", ["Bergviksmotet"], road="Rv 61")]))
    _, s = evaluate(c, reg)
    assert s["found"] == 0


def test_validate_reports_unknown_names_and_kinds(reg):
    c = corpus(case("a", "2026-11-26T10:00:00", "x",
                    [item("x", "crash", ["Nowhere"]), {"kind": "new"}]))
    problems = ev.validate(c, reg, KARLSTAD)
    assert any("okänd kind" in p for p in problems)
    assert any("Nowhere" in p for p in problems)
    assert any("'incident' saknas" in p for p in problems)


def test_import_adds_new_transcribed_messages_once():
    c = corpus()
    events = [
        {"id": 7, "started_at": "2026-11-26 10:00:00Z", "transcription": "E18 …",
         "station_ps": "P4 Värml", "radiotext": ["Trafik"]},
        {"id": 8, "started_at": "2026-11-26 09:00:00Z", "transcription": ""},
    ]
    assert ev.import_events(c, events) == 1
    assert ev.import_events(c, events) == 0
    [new] = c["cases"]
    assert new["at"] == "2026-11-26T10:00:00" and new["expect"] is None


def test_bundled_corpus_is_valid():
    import geo_service
    c = ev.load_corpus()
    svc = geo_service.GeoService(geo.create_geolocator("rules"))
    node = svc.locate_node(c["node_location"])
    assert ev.validate(c, svc.area(node.point), node.point) == []
