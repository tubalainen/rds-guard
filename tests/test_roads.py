"""Road registry: where the numbered roads run, and placing on them."""

import gzip
import os
import sys

import pytest

import gazetteer as gz
import geolocate as geo
import roads

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import build_roads  # noqa: E402

KARLSTAD = (59.38091, 13.50276)
# E18 west of Karlstad, a little south of the junctions (about 300 m)
E18 = [(59.3760, 13.1000), (59.3760, 13.4300), (59.3770, 13.5000)]
# Rv 61 north-west from Karlstad toward Kil
RV61 = [(59.3900, 13.4500), (59.4500, 13.3600), (59.5000, 13.3200)]
PLACES = [
    ("Karlstad", "city", 59.38091, 13.50276, "S"),
    ("Bergviksmotet", "junction", 59.379, 13.43247, "S"),
    ("Grums", "town", 59.35189, 13.10568, "S"),
    ("Kil", "town", 59.50206, 13.31754, "S"),
    # Two hamlets called Kärr: one next to Rv 61, one nearer Karlstad but
    # far from it
    ("Kärr", "hamlet", 59.4520, 13.3560, "S"),
    ("Kärr", "hamlet", 59.3300, 13.6000, "S"),
]


def write_roads(path, lines):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("# test roads\n")
        for key, county, pts in lines:
            f.write(f"{key}\t{county}\t{build_roads.encode(pts)}\n")


@pytest.fixture
def reg(tmp_path):
    path = tmp_path / "gaz.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for name, kind, lat, lon, county in PLACES:
            f.write(f"{name}\t{kind}\t{lat}\t{lon}\t{county}\t\n")
    rpath = tmp_path / "roads.tsv.gz"
    write_roads(rpath, [("E18", "", E18), ("61", "", RV61)])
    return gz.Gazetteer.load(str(path), roads_path=str(rpath))


def place(text, reg):
    return geo.Geolocator(None).locate(text, reg, KARLSTAD).placements


@pytest.mark.parametrize("road,key", [
    ("E18", "E18"), ("E 18", "E18"), ("Rv 61", "61"), ("Väg 61", "61"),
    ("Lv 236", "236"), (None, None), ("", None),
])
def test_road_keys(road, key):
    assert roads.road_key(road) == key


@pytest.mark.parametrize("ref,keys", [
    ("E 18", [("E18", "")]), ("61;62", [("61", ""), ("62", "")]),
    ("S 554", [("554", "S")]), ("AC 123", [("123", "AC")]), ("Fv123", []),
])
def test_refs_from_openstreetmap(ref, keys):
    assert build_roads.parse_ref(ref) == keys


def test_ways_join_end_to_end():
    lines = build_roads.chain([[(0, 0), (0, 1)], [(0, 2), (0, 1)], [(0, 2), (0, 3)]])
    assert lines == [[(0, 0), (0, 1), (0, 2), (0, 3)]] or \
        lines == [[(0, 3), (0, 2), (0, 1), (0, 0)]]


def test_simplify_keeps_the_shape():
    line = [(59.0, 13.0 + i * 0.001) for i in range(50)] + [(59.1, 13.05)]
    out = build_roads.simplify(line)
    assert out[0] == line[0] and out[-1] == line[-1]
    assert len(out) == 3


def test_nearest_point_and_direction(reg):
    lat, lon, d, bearing = reg.roads.nearest("E18", (59.379, 13.30))
    assert lat == pytest.approx(59.376, abs=1e-4)
    assert 0.2 < d < 0.4
    assert bearing in (pytest.approx(90, abs=2), pytest.approx(270, abs=2))
    assert reg.roads.nearest("E18", (59.60, 13.30), max_km=5) is None
    assert reg.roads.nearest("E20", (59.379, 13.30)) is None


def test_lines_far_from_the_node_are_not_loaded(tmp_path):
    path = tmp_path / "roads.tsv.gz"
    write_roads(path, [("E18", "", E18), ("E4", "", [(65.0, 21.0), (65.1, 21.1)])])
    r = roads.Roads.load(str(path), centers=[KARLSTAD], radius_km=100)
    assert len(r) == 1


def test_no_road_file_is_no_roads(tmp_path):
    r = roads.Roads.load(str(tmp_path / "missing.tsv.gz"))
    assert not r


def test_the_pin_goes_onto_the_road(reg):
    [p] = place("E18 i Karlstad i höjd med Bergviksmotet i riktning mot Grums. "
                "En olycka.", reg)
    assert p.label == "E18 Bergviksmotet"
    assert p.lat == pytest.approx(59.376, abs=1e-4)     # on E18, not 300 m north
    assert p.radius_m == 500
    assert p.bearing == pytest.approx(270, abs=3)       # along E18 toward Grums


def test_the_place_beside_the_road_is_the_one_meant(reg):
    [p] = place("Riksväg 61 vid Kärr. Ett fordon har fått stopp.", reg)
    assert (p.lat, p.lon) == pytest.approx((59.4517, 13.3567), abs=2e-3)


def test_without_the_road_the_nearer_place_wins(reg):
    reg.roads = None
    [p] = place("Riksväg 61 vid Kärr. Ett fordon har fått stopp.", reg)
    assert (p.lat, p.lon) == (59.33, 13.6)


def test_a_road_not_in_the_registry_changes_nothing(reg):
    [p] = place("Riksväg 63 vid Kärr. Ett fordon har fått stopp.", reg)
    assert (p.lat, p.lon) == (59.33, 13.6)


def test_along_picks_the_direction_toward_the_place():
    assert roads.along(90, 80) == 90
    assert roads.along(90, 250) == 270


# ---------------------------------------------------- Trafikverket (NVDB)

import build_roads_trafikverket as tv  # noqa: E402


def test_wkt_lines_and_points():
    assert tv.wkt_lines("LINESTRING (13.1 59.3, 13.2 59.4)") == [[(59.3, 13.1), (59.4, 13.2)]]
    assert tv.wkt_lines("MULTILINESTRING Z ((13.1 59.3 10, 13.2 59.4 11), (14 60, 14.1 60))") == [
        [(59.3, 13.1), (59.4, 13.2)], [(60.0, 14.0), (60.0, 14.1)]]
    assert tv.wkt_point("POINT (13.43 59.37)") == (59.37, 13.43)


def test_nvdb_road_keys():
    assert tv.road_key({"Huvudnummer": 18, "Europaväg": 0}) == "E18"
    assert tv.road_key({"Huvudnummer": 61, "Europaväg": -1}) == "61"
    assert tv.road_key({"Huvudnummer": 236}) == "236"
    assert tv.road_county("236", "S") == "S" and tv.road_county("61", "S") == ""
    assert tv.road_key({}) is None


def test_street_pairs_are_not_interchanges():
    assert tv.junction_names({"Trafikplatsnamn": "Bergviksmotet"}) == ["Bergviksmotet"]
    assert tv.junction_names({"Trafikplatsnamn": "Horsengatan/Mossgatan"}) == []


def test_old_and_deleted_rows_are_left_out():
    assert tv.still_valid({"Valid_To": "2030-01-01T00:00:00"}, "2026-11-27")
    assert not tv.still_valid({"Valid_To": "2020-01-01"}, "2026-11-27")
    assert not tv.still_valid({"Deleted": True}, "2026-11-27")


@pytest.mark.parametrize("name,aliases", [
    ("Bergvik", ["Bergvik", "Bergviksmotet"]), ("Våxnäs", ["Våxnäs", "Våxnäsmotet"]),
    ("Klara", ["Klara", "Klaramotet"]), ("Bergviksmotet", ["Bergviksmotet"]),
    ("Karlstad västra", ["Karlstad västra"]),
])
def test_junction_aliases(name, aliases):
    assert gz.junction_aliases(name) == aliases


def test_trafikverket_junctions_join_the_registry(tmp_path):
    jpath = tmp_path / "junctions.tsv.gz"
    with gzip.open(jpath, "wt", encoding="utf-8") as f:
        f.write("# test\n")
        f.write("Bergvik\tjunction\t59.37900\t13.43247\t\t31\n")    # OSM has Bergviksmotet
        f.write("Skutberget\tjunction\t59.36000\t13.40000\t\t30\n")
        f.write("Karlstad\tjunction\t59.38000\t13.50000\t\t\n")       # the city is there
    gpath = tmp_path / "gaz.tsv.gz"
    with gzip.open(gpath, "wt", encoding="utf-8") as f:
        for name, kind, lat, lon, county in PLACES:
            f.write(f"{name}\t{kind}\t{lat}\t{lon}\t{county}\t\n")
    reg = gz.Gazetteer.load(str(gpath), junctions_path=str(jpath))
    names = [p.name for p in reg.places if p.kind == "junction"]
    assert names.count("Bergviksmotet") == 1
    assert "Bergvik" in names and "Skutberget" in names and "Skutbergetsmotet" in names
    assert "Karlstad" not in names


@pytest.mark.parametrize("raw,name", [
    ("Kroksvik, östergående ", "Kroksvik"), ("Stolpen västergående", "Stolpen"),
    ("Nikkalakorsning mot Kalix", "Nikkalakorsning"), ("Falutorget Norra", "Falutorget"),
    ("Bodetta", "Bodetta"), ("Nyeds kyrka, södergående", "Nyeds kyrka"),
])
def test_camera_names(raw, name):
    assert tv.spot_name(raw) == name


@pytest.mark.parametrize("text,road", [
    ("E4", "E4"), ("61", "61"), ("720", ""), ("Kameran är placerad på E45 vid Falutorget", "E45"),
    ("Kameran sitter på väg 63 vid Ökna", "63"), ("", ""),
])
def test_camera_roads(text, road):
    assert tv.spot_road(text) == road
