"""Transcribe the stored recordings again, oldest first.

Runs inside the RDS Guard container, with the same engine and settings as
the running receiver (TRANSCRIPTION_ENGINE, ElevenLabs key, fallback, the
local place names as keyterms/prompt). Each message's transcription is
replaced; the map is not touched. Afterwards, rebuild the map with
Admin → Karta → Bygg om kartan, which places every message again from the
new texts.

    docker compose exec rds-guard python3 /app/tools/retranscribe.py          # what it would do
    docker compose exec rds-guard python3 /app/tools/retranscribe.py --yes    # do it
    … --since 2026-11-20   only messages from that day on
    … --limit 5            only the 5 oldest (to try it)
    … --engine local       KB-Whisper on the CPU instead (free, slower)

Cost: ElevenLabs is about 2–3 öre per traffic announcement (it counts from
your plan's credits); local is free. It prints a line per message as it goes.
"""

import argparse
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import config  # noqa: E402
import event_store  # noqa: E402

ORE_PER_MESSAGE = (2, 3)        # ElevenLabs, per traffic announcement


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def messages(db, since=None, limit=None):
    """Stored messages with a recording, oldest first."""
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    sql = ("SELECT id, type, started_at, station_ps, audio_path, duration_sec, "
           "transcription FROM events WHERE audio_path IS NOT NULL "
           "AND audio_path != ''")
    args = []
    if since:
        sql += " AND started_at >= ?"
        args.append(since)
    sql += " ORDER BY started_at, id"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    rows = [dict(r) for r in conn.execute(sql, args)]
    conn.close()
    return rows


def audio_file(rel):
    path = rel if os.path.isabs(rel) else os.path.join(config.AUDIO_DIR, rel)
    return path if os.path.exists(path) else None


def make_transcriber(engine):
    from transcriber import create_transcriber
    return create_transcriber(
        engine=engine,
        language=config.TRANSCRIPTION_LANGUAGE,
        model_size=config.TRANSCRIPTION_MODEL,
        device=config.TRANSCRIPTION_DEVICE,
        remote_url=config.WHISPER_REMOTE_URL,
        remote_timeout=config.WHISPER_REMOTE_TIMEOUT,
        initial_prompt=config.TRANSCRIPTION_PROMPT,
        beam_size=config.TRANSCRIPTION_BEAM_SIZE,
        cpu_threads=config.TRANSCRIPTION_CPU_THREADS,
        elevenlabs_api_key=config.ELEVENLABS_API_KEY,
        elevenlabs_model=config.ELEVENLABS_MODEL,
        elevenlabs_keyterms=config.ELEVENLABS_KEYTERMS,
        elevenlabs_timeout=config.ELEVENLABS_TIMEOUT,
        fallback=config.TRANSCRIPTION_FALLBACK,
    )


def place_name_hints():
    """The receiver's place names, as the running receiver sends them."""
    if not (config.TRANSCRIPTION_PLACE_NAMES and config.NODE_LOCATION):
        return None
    import geo_service
    svc = geo_service.create_service("rules",
                                     place_names=config.TRANSCRIPTION_PLACE_NAMES)
    node = svc.locate_node(config.NODE_LOCATION)
    if node is None:
        return None
    return svc.hints(node.point, event_store.recent_place_names())


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--yes", action="store_true", help="transcribe (else only show what it would do)")
    ap.add_argument("--since", help="only messages from this date on (YYYY-MM-DD)")
    ap.add_argument("--limit", type=int, help="only this many, oldest first")
    ap.add_argument("--engine", choices=("elevenlabs", "local", "remote"),
                    default=config.TRANSCRIPTION_ENGINE)
    ap.add_argument("--db", default=os.environ.get("DB_PATH", event_store._DB_PATH))
    args = ap.parse_args()

    event_store.init_db(args.db)
    rows = messages(args.db, args.since, args.limit)
    found = [(r, audio_file(r["audio_path"])) for r in rows]
    missing = [r for r, f in found if f is None]
    todo = [(r, f) for r, f in found if f]
    engine = args.engine
    if engine == "elevenlabs" and not config.ELEVENLABS_API_KEY:
        engine = "local"
    minutes = sum((r["duration_sec"] or 0) for r, _ in todo) / 60
    log(f"{len(todo)} messages with a recording ({minutes:.0f} min of audio)"
        + (f", {len(missing)} whose audio file is gone" if missing else ""))
    if engine == "elevenlabs":
        lo, hi = (len(todo) * o / 100 for o in ORE_PER_MESSAGE)
        log(f"Engine: ElevenLabs {config.ELEVENLABS_MODEL}, about {lo:.0f}–{hi:.0f} kr "
            f"({ORE_PER_MESSAGE[0]}–{ORE_PER_MESSAGE[1]} öre each, from your credits)")
    else:
        log(f"Engine: {engine} (free)")
    if not args.yes:
        log("Nothing changed. Run again with --yes to transcribe them.")
        return

    t = make_transcriber(engine)
    if t is None:
        raise SystemExit("Transcription is off (TRANSCRIPTION_ENGINE=none)")
    log("Loading place names …")
    hints = place_name_hints()
    log(f"{len(hints.get('keyterms', [])) if hints else 0} place names as keyterms")

    done = failed = 0
    t_all = time.time()
    for n, (r, path) in enumerate(todo, 1):
        t0 = time.time()
        head = f"{n}/{len(todo)} ev{r['id']} {r['started_at'][:16]} {r['type']}"
        try:
            text = t.transcribe(path, hints)
        except Exception as e:  # noqa: BLE001 - report and go on
            failed += 1
            log(f"{head}: FAILED {e}")
            continue
        text = (text or "").strip()
        took = time.time() - t0
        event_store.update_event_transcription(r["id"], text or None,
                                               status="done" if text else "error",
                                               duration_sec=round(took, 1))
        done += 1
        old = (r["transcription"] or "").strip()
        same = " (same)" if old == text else ""
        log(f"{head}: {getattr(t, '_last_engine', engine)} {took:.1f}s{same}: "
            f"{text[:90]}{'…' if len(text) > 90 else ''}")
    log(f"Done: {done} transcribed, {failed} failed, in {(time.time() - t_all) / 60:.1f} min. "
        "Now rebuild the map: Admin → Karta → Bygg om kartan.")


if __name__ == "__main__":
    main()
