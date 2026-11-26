"""Notification rules: validation, matching, message and history preview."""

import pytest

import notify_rules as nr

ZONE = nr.get_zone("Europe/Stockholm")


def rule(**kw):
    base = {"name": "Pendling", "event_types": ["traffic"]}
    base.update(kw)
    return nr.validate(base)


def event(**kw):
    ev = {"type": "traffic", "pi": "E203", "station_ps": "P4 Sthlm",
          "radiotext": ["Olycka E4 norr Häggvik"], "transcription": None,
          # Tuesday 24 Nov 2026, 07:48 in Stockholm (UTC+1)
          "started_at": "2026-11-24T06:48:00", "duration_sec": 72}
    ev.update(kw)
    return ev


# -- validation -------------------------------------------------------------

def test_validate_fills_defaults():
    r = rule()
    assert r["enabled"] is True
    assert r["topic"] == "pendling"
    assert r["priority"] == "normal"
    assert r["keywords"] == r["stations"] == r["days"] == r["windows"] == []


def test_validate_cleans_lists():
    r = rule(keywords=["  E4 ", "e4", "", "Essingeleden"], stations=["e203"],
             days=[4, 0, 0], windows=[{"from": "06:00", "to": "09:00"}])
    assert r["keywords"] == ["E4", "Essingeleden"]
    assert r["stations"] == ["E203"]
    assert r["days"] == [0, 4]


def test_all_seven_days_means_any_day():
    assert rule(days=list(range(7)))["days"] == []


def test_slugify_swedish_name():
    assert nr.slugify("Halka på väg 73!") == "halka-pa-vag-73"


@pytest.mark.parametrize("bad", [
    {"name": "", "event_types": ["traffic"]},
    {"name": "x", "event_types": []},
    {"name": "x", "event_types": ["eon"]},
    {"name": "x", "event_types": [["traffic"]]},
    {"name": "x", "event_types": ["traffic"], "stations": ["XYZ"]},
    {"name": "x", "event_types": ["traffic"], "days": [7]},
    {"name": "x", "event_types": ["traffic"], "days": [True]},
    {"name": "x", "event_types": ["traffic"], "windows": [{"from": "6", "to": "9"}]},
    {"name": "x", "event_types": ["traffic"], "windows": [{"from": "06:00", "to": "06:00"}]},
    {"name": "x", "event_types": ["traffic"], "priority": "urgent"},
    {"name": "x", "event_types": ["traffic"], "topic": "a/b"},
    {"name": "x", "event_types": ["traffic"], "keywords": "E4"},
    "not a dict",
])
def test_validate_rejects(bad):
    with pytest.raises(nr.RuleError):
        nr.validate(bad)


# -- matching ---------------------------------------------------------------

def test_type_and_station():
    assert nr.matches(rule(), event(), ZONE)
    assert not nr.matches(rule(), event(type="emergency"), ZONE)
    assert not nr.matches(rule(stations=["E204"]), event(), ZONE)
    assert nr.matches(rule(stations=["e203"]), event(), ZONE)
    assert nr.matches(rule(stations=["0xE203"]), event(pi="0xe203"), ZONE)
    assert nr.matches(rule(stations=["E203"]), event(pi="0xE203"), ZONE)


def test_disabled_rule_never_matches():
    assert not nr.matches(rule(enabled=False), event(), ZONE)


def test_keywords_search_radiotext_and_transcription():
    r = rule(keywords=["essingeleden"])
    assert not nr.matches(r, event(), ZONE)
    assert nr.matches(r, event(transcription="Kö på Essingeleden norrut"), ZONE)
    assert nr.matched_keywords(rule(keywords=["e4", "Kista"]), event()) == ["e4"]


def test_days_and_windows_use_local_time():
    # 06:48 UTC is 07:48 in Stockholm
    assert nr.matches(rule(windows=[{"from": "07:00", "to": "08:00"}]), event(), ZONE)
    assert not nr.matches(rule(windows=[{"from": "06:00", "to": "07:00"}]), event(), ZONE)
    assert nr.matches(rule(days=[1]), event(), ZONE)       # Tuesday
    assert not nr.matches(rule(days=[5, 6]), event(), ZONE)


def test_window_over_midnight():
    r = rule(windows=[{"from": "22:00", "to": "06:00"}])
    assert nr.matches(r, event(started_at="2026-11-24T22:30:00"), ZONE)   # 23:30
    assert nr.matches(r, event(started_at="2026-11-24T03:00:00"), ZONE)   # 04:00
    assert not nr.matches(r, event(), ZONE)


def test_node_filter():
    r = rule(nodes=["Kalle"])
    assert nr.matches(r, event(node_name="kalle"), ZONE)
    assert not nr.matches(r, event(node_name="Lisa"), ZONE)


# -- message ----------------------------------------------------------------

def test_payload():
    r = dict(rule(keywords=["E4"], priority="high"), id=3)
    p = nr.build_payload(event(transcription="Olycka på E4.  Kö till Kista."),
                         r, 42, public_url="https://rds.example/",
                         audio_path="/api/audio/42.ogg")
    assert p["title"] == "Trafik: Olycka E4 norr Häggvik"
    assert p["message"] == "Olycka på E4. Kö till Kista."
    assert p["priority"] == "high"
    assert p["rule"] == {"id": 3, "name": "Pendling"}
    assert p["matched_keywords"] == ["E4"]
    assert p["url"] == "https://rds.example/#/event/42"
    assert p["audio_url"] == "https://rds.example/api/audio/42.ogg"


def test_payload_without_text_or_public_url():
    p = nr.build_payload(event(type="emergency", radiotext=[]), rule(), 7)
    assert p["title"].startswith("VMA: Viktigt meddelande")
    assert p["url"] is None and p["audio_url"] is None


def test_long_message_is_shortened():
    p = nr.build_payload(event(transcription="ord " * 300), rule(), 1)
    assert len(p["message"]) <= nr.MAX_MESSAGE + 1
    assert p["message"].endswith("…")


# -- preview ----------------------------------------------------------------

def test_preview_counts_per_local_day():
    events = [
        event(radiotext=["Kö Essingeleden"]),
        event(radiotext=["Halka väg 73"]),
        event(started_at="2026-11-23T23:30:00", radiotext=["Essingeleden"]),  # 24 Nov local
        event(started_at="2026-11-22T10:00:00", radiotext=["Essingeleden"]),
    ]
    res = nr.preview(rule(keywords=["Essingeleden"], enabled=False), events, ZONE)
    assert res["total"] == 4 and res["matched"] == 3
    assert res["days"] == [
        {"date": "2026-11-22", "total": 1, "matched": 1},
        {"date": "2026-11-24", "total": 3, "matched": 2},
    ]
    assert len(res["samples"]) == 3
