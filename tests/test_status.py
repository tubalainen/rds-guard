"""status.station_list normalises both single- and multi-station shapes."""

import status


def test_single_station_shape():
    snap = {"frequency": "103.3M", "groups_per_sec": 11.2, "groups_total": 50,
            "station": {"pi": "0x9E04", "ps": "P4 Sthlm", "tp": True,
                        "ta": False, "radiotext": "x"}}
    out = status.station_list(snap)
    assert out == [{"frequency": "103.3M", "pi": "0x9E04", "ps": "P4 Sthlm",
                    "long_ps": None, "prog_type": None, "tp": True,
                    "ta": False, "groups_per_sec": 11.2, "groups_total": 50}]


def test_single_station_not_locked_yet():
    out = status.station_list({"frequency": "103.3M", "station": None})
    assert out[0]["frequency"] == "103.3M"
    assert out[0]["pi"] is None


def test_multi_station_shape_drops_extra_fields():
    snap = {"stations": [{"frequency": "103.5M", "pi": "0xC404",
                          "radiotext": "not forwarded", "groups_per_sec": 1}]}
    out = status.station_list(snap)
    assert out[0]["pi"] == "0xC404"
    assert "radiotext" not in out[0]
