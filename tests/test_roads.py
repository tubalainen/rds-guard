"""The road network: the right same-named place, pins on the road."""

import gzip
import json

import pytest

import gazetteer as gz
import geolocate as geo
import roads

KARLSTAD = (59.38091, 13.50276)
# E18 roughly from Karlstad east past Ölme to Kristinehamn
E18 = [59.38, 13.50, 59.384, 13.85, 59.37, 14.00, 59.33, 14.10]


@pytest.fixture
def road_file(tmp_path):
    path = tmp_path / "roads.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump({"roads": {"E18": [E18], "Rv 61": [[59.5, 13.0, 59.6, 13.1]]}}, f)
    return str(path)


@pytest.fixture
def reg(tmp_path, road_file):
    path = tmp_path / "gaz.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("Karlstad\tcity\t59.38091\t13.50276\tS\t\n"
                "Ölme\tvillage\t59.36209\t14.00995\tS\t\n"
                "Stolpen\tisolated_dwelling\t59.44863\t13.99557\tS\t\n"
                "Stolpen\tisolated_dwelling\t59.36987\t14.01174\tS\t\n")
    reg = gz.Gazetteer.load(str(path))
    reg.roads = roads.Roads.load(road_file)
    return reg


def test_distance_and_nearest_point(road_file):
    net = roads.Roads.load(road_file)
    assert "E18" in net and len(net) == 2
    d, p = net.nearest("E18", (59.40, 13.85))
    assert 1.5 < d < 2.0 and abs(p[0] - 59.384) < 0.01
    assert net.distance("E18", (60.5, 13.0)) is None
    assert net.roads_near((59.371, 14.0)) == ["E18"]


def test_a_missing_file_or_empty_path_gives_no_roads(tmp_path):
    assert len(roads.Roads.load(str(tmp_path / "none.json.gz"))) == 0
    assert len(roads.Roads.load("")) == 0


def test_the_same_name_by_the_named_road_wins(reg):
    [p] = geo.Geolocator(None).locate("Olycka på E18 vid Stolpen.", reg, KARLSTAD).placements
    assert p.lat < 59.4                      # the Stolpen by E18, not 10 km north


def test_the_pin_is_put_on_the_road(reg):
    m = geo.Mention(road="E18", places=["Ölme"])
    p = geo.resolve(m, reg, KARLSTAD)
    d = reg.roads.distance("E18", (p.lat, p.lon))
    assert d is not None and d < 0.05


def test_candidates_say_which_named_road_they_are_by(reg):
    near = [p for p in reg.exact("Stolpen") if p.lat < 59.4][0]
    far = [p for p in reg.exact("Stolpen") if p.lat > 59.4][0]
    assert "vid E18" in geo.describe_place(near, reg, ["E18"])
    assert "vid E18" not in geo.describe_place(far, reg, ["E18"])
    assert geo.roads_in("På E18 och riksväg 61, sedan E18 igen") == ["E18", "Rv 61"]


def test_without_roads_placing_works_as_before(tmp_path):
    path = tmp_path / "gaz.tsv.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        f.write("Ölme\tvillage\t59.36209\t14.00995\tS\t\n")
    reg = gz.Gazetteer.load(str(path))
    p = geo.resolve(geo.Mention(road="E18", places=["Ölme"]), reg, KARLSTAD)
    assert (p.lat, p.lon) == (59.36209, 14.00995)
