"""Geolocation: place-name registry, rules engine, resolver and incidents."""

import gzip
import json
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

    def read(self, text, radiotext, location, candidates, open_incidents, gaz=None):
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


# ---------------------------------------------------------------- older messages

def test_an_older_message_joins_but_does_not_overwrite(db):
    """Placed afterwards: it adds to the incident without changing what the
    later message said, and moves first_seen back."""
    [a] = merge(db, [pl(59.379, 13.43247, 500, cleared=True, what="Öppet igen")],
                2, "2026-11-26T11:00:00")
    [b] = geo.merge_placements(db, [pl(59.3791, 13.4326, 500, what="Olycka")], 1,
                               "2026-11-26T09:30:00", "2026-11-25T21:30:00", "rules")
    assert a == b
    [inc] = geo.list_incidents(db)
    assert inc["status"] == "cleared" and inc["what"] == "Öppet igen"
    assert inc["first_seen"] == "2026-11-26T09:30:00"
    assert inc["last_seen"] == "2026-11-26T11:00:00"
    assert sorted(inc["events"]) == ["1", "2"]


def test_an_older_message_long_before_is_its_own_incident(db):
    merge(db, [pl(59.379, 13.43247, 500)], 2, "2026-11-26T11:00:00")
    geo.merge_placements(db, [pl(59.379, 13.43247, 500)], 1,
                         "2026-11-20T08:00:00", "2026-11-19T20:00:00", "rules")
    assert len(geo.list_incidents(db)) == 2


def test_open_incidents_at_a_past_time(db):
    merge(db, [pl(59.379, 13.43247, 500)], 1, "2026-11-26T10:00:00")
    assert geo.open_incidents(db, "2026-11-25T20:00:00", "2026-11-26T09:00:00") == []
    assert len(geo.open_incidents(db, "2026-11-25T20:00:00", "2026-11-26T10:30:00")) == 1


def test_shift_keeps_the_timestamp_form():
    assert geo._shift_iso("2026-11-26T23:00:00Z", 2) == "2026-11-27T01:00:00Z"
    assert geo._shift_iso("2026-11-26T23:00:00", -24) == "2026-11-25T23:00:00"


# ---------------------------------------------------------------- places added by hand

def test_a_place_added_by_hand_wins_over_others_with_its_name(tmp_path):
    path = tmp_path / "gaz.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("Ölme\tvillage\t59.36209\t14.00995\tS\t\n"
                "Stolpen\tlocality\t59.36980\t14.01170\tS\t\n"      # replaced
                "Stolpen\tisolated_dwelling\t59.44863\t13.99557\tS\t\n"
                "Stolpen\tisolated_dwelling\t59.51357\t14.30688\tS\t\n")
    local = tmp_path / "local.tsv"
    local.write_text("# comment\n"
                     "Stolpen\tlandmark\t59.36987\t14.01174\tS\tvid E18\n"
                     "broken line\n", encoding="utf-8")
    reg = gz.Gazetteer.load(str(path), local_path=str(local))
    stolpen = reg.exact("Stolpen")
    assert len(stolpen) == 3                          # the OSM twin is gone
    assert [p.local for p in stolpen].count(True) == 1
    assert reg.search("Stolpen", limit=1)[0].place.local
    [p] = place("Olycka på E18 vid Stolpen.", reg, center=(59.38, 13.5))
    assert (p.lat, p.lon, p.radius_m) == (59.36987, 14.01174, 600)


def test_bundled_hand_made_places_are_valid():
    places = gz.read_local(gz.LOCAL_PATH)
    assert places
    for p in places:
        assert p.kind in gz.KIND_RADIUS_M and p.county in gz.COUNTIES


# ---------------------------------------------------------------- kinds (v0.7)

def test_rules_give_each_part_a_kind_and_its_words():
    ms = geo.extract_rules(
        "Olycka på E18 vid Bergviksmotet. Sedan fortfarande köer på riksväg 61 "
        "vid Skattkärr. Det är mycket halt på många vägar i Värmlands län.")
    assert [m.kind for m in ms] == ["new", "repeat", "area_warning"]
    assert ms[0].evidence == "Olycka på E18 vid Bergviksmotet."
    assert "halt" in ms[2].evidence


def test_area_warnings_are_not_placed(reg):
    r = geo.Geolocator(None).locate(
        "Mycket halt läge på många vägar, runt Grums och Kil.", reg, KARLSTAD)
    assert r.placements == [] and [m.kind for m in r.areas] == ["area_warning"]


def test_an_id_picks_one_of_several_places_with_a_name(tmp_path):
    path = tmp_path / "gaz.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("Ölme\tvillage\t59.36209\t14.00995\tS\t\n"
                "Stolpen\tisolated_dwelling\t59.44863\t13.99557\tS\t\n"
                "Stolpen\tlocality\t59.36987\t14.01174\tS\t\n")
    reg = gz.Gazetteer.load(str(path))
    wanted = next(p for p in reg.exact("Stolpen") if p.kind == "locality")
    [m] = geo.mentions_from_items(
        [{"kind": "new", "road": "E18", "town": "p2", "places": [],
          "evidence": "vid Stolpen", "incident_id": None}],
        {1: reg.exact("Ölme")[0], 2: wanted}, ("Olycka på E18 vid Stolpen.",))
    p = geo.resolve(m, reg, (59.38, 13.5))
    assert (p.lat, p.lon) == wanted.point and p.label == "E18 Stolpen"
    assert m.evidence == "vid Stolpen"


def test_describe_place_says_where_it_is(reg):
    p = reg.exact("Skattkärr")[0]
    assert geo.describe_place(p, reg) == "Skattkärr (tätort, Värmlands län)"


def test_evidence_must_stand_in_the_text():
    text = "Olycka på E18 vid Väse. Älg på riksväg 61 vid Kil."
    assert geo.check_evidence("Älg på riksväg 61 vid Kil.", text) == "Älg på riksväg 61 vid Kil."
    assert geo.check_evidence("Älg på riksväg 61 nära Kil", text) == "Älg på riksväg 61 vid Kil."
    assert geo.check_evidence("Något helt annat här", text) == ""


class FakeClient:
    def __init__(self, data):
        self.data, self.sent = data, None
        self.messages = self

    def create(self, **kw):
        self.sent = kw
        block = type("B", (), {"type": "text", "text": json.dumps(self.data)})
        return type("R", (), {"stop_reason": "end_turn", "content": [block],
                              "usage": None})


def test_claude_reader_sends_ids_and_known_incidents(reg):
    reader = object.__new__(geo.ClaudeReader)
    reader._client = FakeClient({"corrected_text": "Olycka på E18 vid Skattkärr.",
                                 "items": [{
        "kind": "update", "incident_id": 7, "evidence": "Olycka på E18 vid Skattkärr.",
        "when": "now", "confidence": "high", "road": "E18", "town": None,
        "between": None, "places": ["p1"], "toward": None, "coming_from": None,
        "both_directions": False, "area": None, "what": "Olycka"}]})
    reader.model, reader.last_usage = "m", None
    known = [{"id": 7, "road": "E18", "label": "E18 Skattkärr", "what": "Olycka",
              "status": "active", "first_seen": "a", "last_seen": "b",
              "last_evidence": "Olycka vid Skattkärr"}]
    corrected, [m] = reader.read("Olycka på E18 vid Skatscher.", [], "Karlstad",
                                 reg.exact("Skattkärr"), known, gaz=reg)
    payload = json.loads(reader._client.sent["messages"][0]["content"])
    assert payload["real_places"][0].startswith("p1: Skattkärr (tätort")
    assert payload["known_incidents"][0]["last_said"] == "Olycka vid Skattkärr"
    assert (m.kind, m.updates_incident, m.places) == ("update", 7, ["Skattkärr"])
    assert "skattkärr" in m.pinned


def test_a_repeat_counts_but_changes_nothing(db):
    [a] = merge(db, [pl(59.379, 13.43247, 500, what="Krock, avstängt")], 1,
                "2026-11-26T10:00:00")
    [b] = merge(db, [pl(59.379, 13.43247, 500, what="Köer", kind="repeat",
                        evidence="Fortfarande köer")], 2, "2026-11-26T10:30:00")
    assert a == b
    [inc] = geo.list_incidents(db, "2026-11-26T00:00:00")
    assert inc["what"] == "Krock, avstängt" and inc["last_seen"] == "2026-11-26T10:30:00"
    [ev] = geo.incidents_for_events(db, ["2"])["2"]
    assert (ev["kind"], ev["evidence"]) == ("repeat", "Fortfarande köer")


def test_an_id_links_to_yesterday_but_a_place_alone_does_not(db):
    [a] = merge(db, [pl(59.379, 13.43247, 500)], 1, "2026-11-25T08:00:00")
    since = "2026-11-25T22:00:00"   # 12 hours before the next message
    [b] = geo.merge_placements(db, [pl(59.379, 13.43247, 500)], 2,
                               "2026-11-26T10:00:00", since, "rules")
    [c] = geo.merge_placements(db, [pl(59.379, 13.43247, 500, kind="repeat",
                                       updates_incident=a)], 3,
                               "2026-11-26T10:05:00", since, "claude")
    assert b != a and c == a


def test_known_incidents_include_cleared_and_the_last_words(db):
    merge(db, [pl(59.379, 13.43247, 500, evidence="Krock vid Bergviksmotet")], 1,
          "2026-11-26T10:00:00")
    merge(db, [pl(59.379, 13.43247, 500, kind="cleared", cleared=True,
                  evidence="Öppet igen")], 2, "2026-11-26T11:00:00")
    [inc] = geo.open_incidents(db, "2026-11-26T00:00:00")
    assert inc["status"] == "cleared" and inc["last_evidence"] == "Öppet igen"


def test_old_incident_tables_get_the_new_columns():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE incident_events (incident_id INTEGER, "
                 "event_key TEXT, seen_at TEXT, PRIMARY KEY (incident_id, event_key))")
    geo.init_tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(incident_events)")}
    assert {"kind", "evidence"} <= cols


def test_road_numbers_with_a_suffix():
    assert geo.norm_road("E45an är avstängd") == "E45"
    assert geo.norm_road("riksväg 61-an") == "Rv 61"


def test_a_stretch_of_the_same_road_joins_a_precise_incident(db):
    [a] = merge(db, [pl(59.18151, 13.01479, 1500, road="E45")], 1, "2026-11-13T11:52:00")
    [b] = merge(db, [pl(59.24241, 13.01527, 13210, road="E45")], 2, "2026-11-13T12:04:00")
    [c] = merge(db, [pl(59.24241, 13.01527, 13210, road="Rv 61")], 3, "2026-11-13T12:05:00")
    assert a == b and c != a
