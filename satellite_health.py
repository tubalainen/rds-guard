"""The satellite's health, for Docker (it has no web port to ask).

The satellite writes a small JSON file every 10 seconds (write()); Docker's
HEALTHCHECK runs `python3 -m satellite_health`, which exits 0 when the file
is fresh and at least one receiver is running, and prints why not otherwise.
"""

import json
import os
import sys
import time

MAX_AGE_SEC = 60


def write(path, snapshot):
    snapshot = dict(snapshot, written_at=time.time())
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def check(path, now=None):
    """(healthy, message)."""
    now = time.time() if now is None else now
    try:
        with open(path, encoding="utf-8") as f:
            snap = json.load(f)
    except FileNotFoundError:
        return False, "no health file yet (starting?)"
    except (OSError, ValueError) as e:
        return False, f"health file unreadable: {e}"
    age = now - float(snap.get("written_at") or 0)
    if age > MAX_AGE_SEC:
        return False, f"health file is {age:.0f} s old: the satellite has stopped"
    recs = snap.get("receivers") or []
    # A receiver paused while its gain is calibrated is fine too
    running = [r for r in recs if r.get("state") in ("running", "paused")]
    if not running:
        why = (snap.get("pipeline") or {}).get("error") or "no receiver is running"
        return False, why
    stations = [s for r in running for s in r.get("stations") or []]
    rate = sum(s.get("groups_per_sec") or 0 for s in stations)
    return True, (f"{len(running)}/{len(recs)} receivers running, "
                  f"{len(stations)} stations, {rate:.1f} grp/s")


def main():
    import config
    ok, msg = check(config.HEALTH_FILE)
    print(msg)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
