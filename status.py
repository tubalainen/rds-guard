"""Decoder status snapshot — shared by /api/status and the uplink heartbeat."""

import time

import config


def build_status():
    """Return the status dict served by GET /api/status.

    Single-station mode keeps the original flat shape (`frequency` +
    `station`); multi-station mode returns a `stations[]` array.
    """
    import rds_guard as rg
    from pipeline import pipeline_status

    snap = rg.stats.snapshot()
    snap["mqtt_connected"] = rg.mqtt_connected.is_set()
    snap["pipeline"] = pipeline_status.snapshot()
    snap["version"] = config.BUILD_VERSION
    snap["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    si = rg.station_info.snapshot()

    if config.MULTI_STATION:
        # Multi-station: return a stations[] array keyed by configured frequency
        stations = []
        for freq in config.STATION_FREQS:
            pi = rg._freq_to_pi.get(freq)
            info = si.get(pi, {}) if pi else {}
            st_stats = rg._station_stats.get(freq)
            st_snap = (st_stats.snapshot() if st_stats
                       else {"groups_per_sec": 0.0, "groups_total": 0})
            stations.append({
                "frequency": freq,
                "pi": pi,
                "ps": info.get("ps"),
                "long_ps": info.get("long_ps"),
                "prog_type": info.get("prog_type"),
                "tp": info.get("tp"),
                "ta": info.get("ta"),
                "radiotext": info.get("radiotext"),
                "groups_per_sec": st_snap["groups_per_sec"],
                "groups_total": st_snap["groups_total"],
            })
        snap["stations"] = stations
    else:
        # Single-station: original flat structure
        snap["frequency"] = config.FM_FREQUENCY
        snap["station"] = next(iter(si.values())) if si else None

    uplink = rg.uplink
    snap["uplink"] = uplink.snapshot() if uplink else {"enabled": False}

    return snap


def station_list(snap):
    """Normalise a build_status() dict to a list of per-station dicts.

    The uplink heartbeat always reports a list, whichever mode is running.
    """
    if "stations" in snap:
        source = snap["stations"]
    else:
        st = snap.get("station") or {}
        source = [{
            "frequency": snap.get("frequency"),
            "pi": st.get("pi"),
            "ps": st.get("ps"),
            "long_ps": st.get("long_ps"),
            "prog_type": st.get("prog_type"),
            "tp": st.get("tp"),
            "ta": st.get("ta"),
            "groups_per_sec": snap.get("groups_per_sec"),
            "groups_total": snap.get("groups_total"),
        }]
    keys = ("frequency", "pi", "ps", "long_ps", "prog_type", "tp", "ta",
            "groups_per_sec", "groups_total")
    return [{k: s.get(k) for k in keys} for s in source]
