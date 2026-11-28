#!/usr/bin/env python3
"""End-to-end scenarios against a running satellite + aggregator whose
Airspy is tools/e2e/fake_airspy_rx (see tools/e2e/README.md).

    python3 tools/e2e/scenarios.py --control run/control.json \
        --api http://127.0.0.1:18080 [scenario ...]

Each scenario sets what the simulated stations send (the control file),
waits in real time and then checks the aggregator's API. It prints what it
does as it goes, and a PASS/FAIL line per check.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request

BASE = {"102.8": {"pi": "0xE224", "ps": "P4 SR", "ta": False, "rt": "",
                  "pty": "Phone-in", "silent": False, "corrupt_ta": 0},
        "103.5": {"pi": "0xE224", "ps": "P4 SR", "ta": False, "rt": "",
                  "pty": "Phone-in", "silent": False, "corrupt_ta": 0}}

args = None
results = []
_stations = {}


def say(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def set_station(freq, **fields):
    _stations.setdefault(freq, dict(BASE[freq])).update(fields)
    with open(args.control + ".tmp", "w") as f:
        json.dump({"stations": _stations}, f)
    import os
    os.replace(args.control + ".tmp", args.control)
    say(f"  {freq}: " + ", ".join(f"{k}={v}" for k, v in fields.items()))


def reset():
    _stations.clear()
    for f in BASE:
        _stations[f] = dict(BASE[f])
    set_station("102.8")
    set_station("103.5")


def wait(sec, why=""):
    say(f"  wait {sec}s {why}")
    time.sleep(sec)


def api(path):
    with urllib.request.urlopen(args.api + path, timeout=10) as r:
        return json.loads(r.read())


def events_since(t0):
    evs = api("/api/v1/events?limit=100")["events"]
    return [e for e in evs if (e.get("started_at") or "") >= t0]


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    say(f"  {'PASS' if ok else 'FAIL'} {name}" + (f" — {detail}" if detail else ""))


def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def brief(e):
    return {k: e.get(k) for k in ("type", "state", "frequency", "pi", "duration_sec",
                                  "started_at", "ended_at", "radiotext", "has_audio",
                                  "audio_url", "receptions", "station_ps")}


def settle(sec=20):
    """Give the aggregator time to get the end and the recording."""
    wait(sec, "(uplink, recording)")


# --- scenarios -----------------------------------------------------------------

def s_full():
    """A whole announcement on 102.8 with RadioText."""
    t0 = utc_now()
    set_station("102.8", ta=True, rt="Olycka på E18 vid Örebro, ett körfält avstängt")
    wait(40, "(announcement on air)")
    set_station("102.8", ta=False, rt="")
    settle()
    evs = events_since(t0)
    check("one event", len(evs) == 1, json.dumps([brief(e) for e in evs], ensure_ascii=False))
    if evs:
        e = evs[0]
        check("traffic on 102.8", e.get("type") == "traffic" and e.get("frequency") == "102.8M")
        check("ended", e.get("state") == "end", e.get("state"))
        d = e.get("duration_sec") or 0
        check("duration about 40 s", 35 <= d <= 46, f"{d}s")
        check("RadioText kept, with å/ä/ö", "Olycka på E18 vid Örebro, ett körfält avstängt"
              in json.dumps(e, ensure_ascii=False), json.dumps(e.get("radiotext"),
                                                               ensure_ascii=False))
        url = e.get("audio_url")
        size = 0
        if url:
            with urllib.request.urlopen(args.api + url, timeout=10) as r:
                size = len(r.read())
        check("recording delivered (about 40 s of Ogg)", size > 40 * 2000, f"{size} bytes")
    return evs


def s_blip():
    """Single corrupted groups: TA on in one group, off in one group."""
    t0 = utc_now()
    for _ in range(3):
        _stations["102.8"]["corrupt_ta"] += 1
        set_station("102.8", corrupt_ta=_stations["102.8"]["corrupt_ta"])
        wait(4)
    settle(5)
    check("blips make no event", events_since(t0) == [],
          json.dumps([brief(e) for e in events_since(t0)], ensure_ascii=False))
    t0 = utc_now()
    set_station("102.8", ta=True)
    wait(15)
    _stations["102.8"]["corrupt_ta"] += 1
    set_station("102.8", corrupt_ta=_stations["102.8"]["corrupt_ta"])   # one TA off
    wait(15)
    set_station("102.8", ta=False)
    settle()
    evs = events_since(t0)
    check("one TA-off group does not split", len(evs) == 1 and 26 <= (evs[0].get("duration_sec") or 0) <= 36,
          json.dumps([brief(e) for e in evs], ensure_ascii=False))


def s_pi_switch():
    """P4 switches PI at the start, in the middle and at the end."""
    for when in ("start", "middle", "end"):
        t0 = utc_now()
        say(f" PI switch at the {when}")
        set_station("103.5", pi="0xE224")
        wait(5)
        if when == "start":
            set_station("103.5", pi="0xE824", ta=True)
        else:
            set_station("103.5", ta=True)
        wait(15)
        if when == "middle":
            set_station("103.5", pi="0xE824")
        wait(15)
        if when == "end":
            set_station("103.5", pi="0xE824", ta=False)
        else:
            set_station("103.5", pi="0xE224", ta=False)
        settle()
        evs = events_since(t0)
        check(f"PI switch at the {when}: one ended event",
              len(evs) == 1 and evs[0].get("state") == "end"
              and 26 <= (evs[0].get("duration_sec") or 0) <= 36,
              json.dumps([brief(e) for e in evs], ensure_ascii=False))
        set_station("103.5", pi="0xE224")


def s_both():
    """Both stations announce at once on the shared PI 0xE224."""
    t0 = utc_now()
    set_station("102.8", ta=True, rt="Örebro: halka på E20")
    set_station("103.5", ta=True, rt="Värmland: älg på väg 63")
    wait(30)
    set_station("102.8", ta=False, rt="")
    set_station("103.5", ta=False, rt="")
    settle()
    evs = events_since(t0)
    freqs = sorted(e.get("frequency") for e in evs)
    check("two events, one per station", freqs == ["102.8M", "103.5M"],
          json.dumps([brief(e) for e in evs], ensure_ascii=False))


def s_silent():
    """102.8 goes silent in the middle of an announcement."""
    t0 = utc_now()
    set_station("102.8", ta=True)
    wait(20)
    set_station("102.8", silent=True)
    wait(200, "(longer than the satellite's 180 s)")
    evs = events_since(t0)
    check("ended by silence", len(evs) == 1 and evs[0].get("state") == "end",
          json.dumps([brief(e) for e in evs], ensure_ascii=False))
    if evs:
        d = evs[0].get("duration_sec") or 0
        check("ends when last heard (about 20 s)", 15 <= d <= 30, f"{d}s")
    set_station("102.8", silent=False, ta=False)
    wait(15)


def s_alarm():
    """PTY Alarm (VMA) on 103.5."""
    t0 = utc_now()
    set_station("103.5", pty="Alarm")
    wait(30)
    set_station("103.5", pty="Phone-in")
    settle()
    evs = events_since(t0)
    check("one emergency event", len(evs) == 1 and evs[0].get("type") == "emergency"
          and evs[0].get("state") == "end",
          json.dumps([brief(e) for e in evs], ensure_ascii=False))


def s_aggregator_down():
    """The aggregator is down during an announcement, then back."""
    t0 = utc_now()
    subprocess.run(["docker", "stop", args.aggregator], check=True, capture_output=True)
    say("  aggregator stopped")
    set_station("102.8", ta=True, rt="Trafikolycka E18")
    wait(30)
    set_station("102.8", ta=False, rt="")
    wait(10)
    subprocess.run(["docker", "start", args.aggregator], check=True, capture_output=True)
    say("  aggregator started")
    wait(60, "(the uplink retries)")
    evs = events_since(t0)
    check("delivered after the outage", len(evs) == 1 and evs[0].get("state") == "end",
          json.dumps([brief(e) for e in evs], ensure_ascii=False))


def s_satellite_restart():
    """The satellite restarts in the middle of an announcement."""
    t0 = utc_now()
    set_station("103.5", ta=True)
    wait(20)
    subprocess.run(["docker", "restart", args.satellite], check=True, capture_output=True)
    say("  satellite restarted")
    wait(20)
    set_station("103.5", ta=False)
    settle()
    evs = events_since(t0)
    open_ = [e for e in evs if e.get("state") != "end"]
    check("nothing left open after a restart", evs and not open_,
          json.dumps([brief(e) for e in evs], ensure_ascii=False))


def s_long():
    """An announcement longer than MAX_RECORDING_SEC."""
    t0 = utc_now()
    set_station("102.8", ta=True)
    wait(args.long_sec)
    set_station("102.8", ta=False)
    settle(30)
    evs = events_since(t0)
    check("a long announcement ends with its full length",
          len(evs) == 1 and evs[0].get("state") == "end"
          and (evs[0].get("duration_sec") or 0) >= args.long_sec - 5,
          json.dumps([brief(e) for e in evs], ensure_ascii=False))


SCENARIOS = {"full": s_full, "blip": s_blip, "pi": s_pi_switch, "both": s_both,
             "alarm": s_alarm, "silent": s_silent, "aggregator": s_aggregator_down,
             "restart": s_satellite_restart, "long": s_long}


def main():
    global args
    p = argparse.ArgumentParser()
    p.add_argument("--control", required=True)
    p.add_argument("--api", default="http://127.0.0.1:18080")
    p.add_argument("--satellite", default="e2e-rds-guard")
    p.add_argument("--aggregator", default="e2e-aggregator")
    p.add_argument("--long-sec", type=int, default=90)
    p.add_argument("scenarios", nargs="*", default=list(SCENARIOS))
    args = p.parse_args()
    reset()
    wait(10, "(stations settle)")
    for name in args.scenarios:
        say(f"== {name}: {SCENARIOS[name].__doc__}")
        try:
            SCENARIOS[name]()
        except Exception as e:
            check(f"{name} ran", False, repr(e))
        reset()
        wait(10, "(between scenarios)")
    failed = [r for r in results if not r[1]]
    say(f"{len(results) - len(failed)} of {len(results)} checks passed")
    for name, _, detail in failed:
        say(f"  FAILED {name}: {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
