"""Geolocation: place-name registry, rules engine, resolver and incidents."""

import gzip
import sqlite3

import pytest

import gazetteer as gz
import geolocate as geo

# A few real places around Karlstad (OpenStreetMap), plus decoys that sound
# alike or share a name
PLACES = [
    ("Karlstad", "city", 59.38091, 13.50276, "S"),
    ("Karlstad", "isolated_dwelling", 58.84667, 11.978, "O"),
    ("Bergviksmotet", "junction", 59.379, 13.43247, "S"),
    ("Våxnäsmotet", "junction", 59.38152, 13.46373, "S"),
    ("Klaramotet", "junction", 59.38377, 13.48295, "S"),
    ("Grums", "town", 59.35189, 13.10568, "S"),
    ("Gökhöjden", "hamlet", 59.37288, 13.17012, "S"),
    ("Väse", "village", 59.38448, 13.85288, "S"),
    ("Skattkärr", "town", 59.41338, 13.67437, "S"),
    ("Kil", "town", 59.50206, 13.31754, "S"),
    ("Hynboholm", "hamlet", 59.4631, 13.37471, "S"),
    ("Karlskoga", "town", 59.32917, 14.51769, "T"),
    ("Villingsberg", "hamlet", 59.28262, 14.69122, "T"),
    ("Billingsberg", "isolated_dwelling", 59.53004, 12.78152, "S"),
    ("Kristinehamn", "town", 59.31136, 14.11176, "S"),
    ("Övre Kvarnmotet", "junction", 59.31684, 14.15089, "S"),
    ("Töcksfors", "village", 59.50932, 11.84252, "S"),
    ("Årjäng", "town", 59.39279, 12.13523, "S"),
    ("Brunnsrondellen", "roundabout", 59.24895, 15.16839, "T"),
    ("Örebro", "city", 59.27473, 15.21512, "T"),
]
KARLSTAD = (59.38091, 13.50276)


@pytest.fixture
def reg(tmp_path):
    path = tmp_path / "gaz.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("# test registry\n")
        for name, kind, lat, lon, county in PLACES:
            f.write(f"{name}\t{kind}\t{lat}\t{lon}\t{county}\t\n")
    return gz.Gazetteer.load(str(path))


def place(text, reg, center=KARLSTAD):
    """All placements the rules engine finds in a message."""
    return geo.Geolocator(None).locate(text, reg, center).placements


# ---------------------------------------------------------------- registry

@pytest.mark.parametrize("heard,real", [
    ("Kallskoga", "Karlskoga"), ("Voxnäs motet", "Våxnäsmotet"),
    ("Willingsberg", "Villingsberg"), ("Töksfors", "Töcksfors"),
    ("Årgäng", "Årjäng"), ("Jökhöjden", "Gökhöjden"), ("klara motet", "Klaramotet"),
])
def test_misheard_names_sound_alike(heard, real):
    assert gz.sound_key(heard) == gz.sound_key(real)


def test_search_corrects_a_misheard_name(reg):
    best = reg.search("Skatscher", near=KARLSTAD, max_km=200)[0]
    assert best.place.name == "Skattkärr" and not best.exact


def test_names_with_the_same_ending_must_differ_before_it(reg):
    # "Grumsrondellen" is not in the registry; "Brunnsrondellen" only rhymes
    assert reg.search("Grumsrondellen") == []


def test_short_names_must_sound_exactly_alike(reg):
    assert [m.place.name for m in reg.search("Kiel")] == ["Kil"]


@pytest.mark.parametrize("region", ["Värmlands län", "Värmland", "S", "SE-S"])
def test_node_location_uses_the_county(reg, region):
    p = reg.locate(f"Karlstad, {region}")
    assert (p.name, p.kind, p.county) == ("Karlstad", "city", "S")


def test_node_location_not_found(reg):
    assert reg.locate("Atlantis, Värmlands län") is None


def test_word_list_puts_earlier_names_first(reg):
    words = reg.word_list(KARLSTAD, limit=5, history=["Bergviksmotet"])
    assert words[0] == "Bergviksmotet"
    assert "Karlstad" in words and len(words) == 5


# ---------------------------------------------------------------- rules

def test_rules_find_road_place_direction():
    [m] = geo.extract_rules(
        "E18 i Karlstad är höjd med Bergviksmotet och i riktning mot Grums "
        "är det fortsatt långa köer.")
    assert m.road == "E18"
    assert m.town == "Karlstad"
    assert "Bergviksmotet" in m.places
    assert m.toward == "Grums"
    assert not m.both_directions and not m.cleared


def test_rules_split_a_message_with_two_incidents():
    ms = geo.extract_rules(
        "Börja på E18 mellan Årgäng och Töksfors, strax innan Töksfors. Där är "
        "det en viltolycka. Vidare till Riksväg 26 i Kristinehamn. Där är det kö.")
    assert [m.road for m in ms] == ["E18", "Rv 26"]
    assert ms[0].between == ("Årgäng", "Töksfors")


def test_rules_see_cleared_and_both_directions():
    [m] = geo.extract_rules("E18 vid Bergviksmotet. Vägen är avstängd i båda "
                            "riktningarna. Nu är det öppet igen.")
    assert m.both_directions and m.cleared


@pytest.mark.parametrize("text,road", [
    ("Riksdags 61 mellan Arvika", "Rv 61"), ("länsväg 734", "Lv 734"),
    ("E 18 vid", "E18"), ("riksväg 26 när", "Rv 26"),
])
def test_road_numbers(text, road):
    assert geo.norm_road(text) == road


# ---------------------------------------------------------------- resolver

def test_junction_gives_a_small_circle_and_direction(reg):
    [p] = place("E18 i Karlstad i höjd med Bergviksmotet i riktning mot Grums. "
                "En olycka med lastbil.", reg)
    assert p.label == "E18 Bergviksmotet"
    assert (p.lat, p.lon) == (59.379, 13.43247)
    assert p.radius_m == 500
    assert 240 <= p.bearing <= 280          # Grums is to the west
    assert p.what.startswith("En olycka")


def test_misheard_landmark_widens_the_circle(reg):
    [p] = place("E18 mellan WC och Karlstad, i höjd med Skatscher. En olycka.", reg)
    assert p.label == "E18 Skattkärr"
    assert p.radius_m == 4500               # town 3 km x 1.5 for the sound match


def test_stretch_between_two_places(reg):
    [p] = place("E18 mellan Årgäng och Töksfors. Viltolycka med älg.", reg)
    assert p.label == "E18 Årjäng–Töcksfors"
    assert p.level == "på en sträcka"


def test_misheard_town_loses_to_a_clear_landmark(reg):
    [p] = place("E18 på Skoga Örebro. Det har varit i höjd med Willingsberg. "
                "Långa köer.", reg)
    assert p.label == "E18 Villingsberg"


def test_both_directions_has_no_bearing(reg):
    [p] = place("E18 vid Bergviksmotet i riktning mot Grums, avstängd i båda "
                "riktningar.", reg)
    assert p.both_directions and p.bearing is None


def test_nothing_known_is_not_placed(reg):
    assert place("Det ser bra ut på vägarna. Tipsa oss på 020 999 444.", reg) == []


def test_no_center_no_placement(reg):
    assert geo.Geolocator(None).locate("E18 vid Bergviksmotet", reg, None).placements == []


# ---------------------------------------------------------------- Claude engine

class FakeReader:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def read(self, text, radiotext, location, candidates, open_incidents):
        self.calls.append((text, [c.name for c in candidates], open_incidents))
        if self.fail:
            raise RuntimeError("API down")
        return ("E18 vid Skattkärr, olycka.", [geo.Mention(
            road="E18", places=["Skattkärr"], toward="Karlstad", what="Olycka")])


def test_claude_result_is_resolved_locally(reg):
    reader = FakeReader()
    r = geo.Geolocator(reader).locate("E18 vid Skatscher, olycka.", reg, KARLSTAD,
                                      "Karlstad, Värmlands län")
    assert r.engine == "claude"
    assert r.corrected_text == "E18 vid Skattkärr, olycka."
    assert [p.label for p in r.placements] == ["E18 Skattkärr"]
    assert "Skattkärr" in reader.calls[0][1]      # grounded in real names


def test_claude_failure_falls_back_to_rules(reg):
    r = geo.Geolocator(FakeReader(fail=True)).locate(
        "E18 i Karlstad i höjd med Bergviksmotet.", reg, KARLSTAD)
    assert r.engine == "rules" and "API down" in r.error
    assert r.corrected_text is None
    assert [p.label for p in r.placements] == ["E18 Bergviksmotet"]


def test_claude_without_key_runs_as_rules():
    assert geo.create_geolocator("claude", api_key="").engine == "rules"
    assert geo.create_geolocator("none") is None


# ---------------------------------------------------------------- incidents

@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    geo.init_tables(conn)
    return conn


def pl(lat, lon, radius, road="E18", **kw):
    return geo.Placement(lat=lat, lon=lon, radius_m=radius, road=road,
                         label=kw.pop("label", "x"), **kw)


def merge(db, placements, key, now):
    return geo.merge_placements(db, placements, key, now, "2026-11-26T00:00:00", "rules")


def test_repeated_messages_become_one_incident(db):
    a = merge(db, [pl(59.379, 13.43247, 500)], 1, "2026-11-26T10:00:00")
    b = merge(db, [pl(59.3791, 13.4326, 500, cleared=True)], 2, "2026-11-26T11:00:00")
    assert a == b
    [inc] = geo.list_incidents(db, "2026-11-26T00:00:00")
    assert inc["status"] == "cleared" and inc["events"] == ["2", "1"]


def test_nearby_junctions_stay_apart(db):
    merge(db, [pl(59.379, 13.43247, 500, label="Bergviksmotet")], 1, "2026-11-26T10:00:00")
    merge(db, [pl(59.38377, 13.48295, 500, label="Klaramotet")], 2, "2026-11-26T10:05:00")
    assert len(geo.list_incidents(db, "2026-11-26T00:00:00")) == 2


def test_a_town_level_mention_does_not_move_a_precise_pin(db):
    [i] = merge(db, [pl(59.379, 13.43247, 500, label="E18 Bergviksmotet")], 1,
                "2026-11-26T10:00:00")
    [j] = merge(db, [pl(59.38091, 13.50276, 5000, label="E18 Karlstad")], 2,
                "2026-11-26T10:10:00")
    assert i == j
    [inc] = geo.list_incidents(db, "2026-11-26T00:00:00")
    assert inc["label"] == "E18 Bergviksmotet" and inc["radius_m"] == 500


def test_other_roads_are_other_incidents(db):
    merge(db, [pl(59.379, 13.43247, 500)], 1, "2026-11-26T10:00:00")
    merge(db, [pl(59.379, 13.43247, 500, road="Rv 61")], 2, "2026-11-26T10:00:00")
    assert len(geo.list_incidents(db, "2026-11-26T00:00:00")) == 2


def test_old_incidents_are_purged(db):
    merge(db, [pl(59.379, 13.43247, 500)], 1, "2026-10-01T10:00:00")
    geo.purge_incidents(db, "2026-11-01T00:00:00")
    assert geo.list_incidents(db, "2026-01-01T00:00:00") == []
