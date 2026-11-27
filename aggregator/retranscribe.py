"""Transcribe the aggregator's stored recordings again, oldest first.

Runs in the aggregator's container, with its engine and settings
(TRANSCRIPTION_ENGINE, the ElevenLabs key, the fallback, and the place names
around the node that heard each message as keyterms/prompt). Each message's
transcription is replaced; the map is not touched. Afterwards, rebuild the
map with Admin -> Karta -> Bygg om kartan, which places every message again
from the new texts.

    docker compose exec aggregator python3 -m aggregator.retranscribe          # what it would do
    docker compose exec aggregator python3 -m aggregator.retranscribe --yes    # do it
    … --since 2026-11-20   only messages from that day on
    … --limit 5            only the 5 oldest (to try it)
    … --engine local       KB-Whisper on the CPU instead (free, slower)
    … --node docker01      only messages that node heard

Cost: ElevenLabs is about 2-3 öre per traffic announcement (it counts from
your plan's credits); local is free. It prints a line per message as it goes.
"""

import argparse
import sys
import time
from pathlib import Path

from . import config, store

ORE_PER_MESSAGE = (2, 3)        # ElevenLabs, per traffic announcement


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def messages(since=None, limit=None, node=None):
    """Stored broadcasts with a recording, oldest first."""
    sql = (store._EVENT_SELECT + " WHERE e.audio_path IS NOT NULL AND e.audio_path != ''")
    args = []
    if since:
        sql += " AND COALESCE(e.started_at, e.received_at) >= ?"
        args.append(since)
    if node:
        sql += (" AND EXISTS (SELECT 1 FROM receptions rn JOIN nodes nn ON "
                "nn.id = rn.node_id WHERE rn.event_id = e.id AND nn.name = ?)")
        args.append(node)
    sql += " ORDER BY COALESCE(e.started_at, e.received_at), e.id"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    return [store._event_dict(r) for r in store._conn().execute(sql, args)]


def audio_file(name):
    path = Path(config.AUDIO_DIR) / Path(name).name
    return str(path) if path.exists() else None


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


class Hints:
    """The place names around the node that heard a message (as the
    running aggregator gives the transcriber), one lookup per node."""

    def __init__(self):
        self._svc = None
        self._by_node = {}

    def for_event(self, row):
        if not config.TRANSCRIPTION_PLACE_NAMES:
            return None
        node_id = row.get("node_id")
        if node_id not in self._by_node:
            location = store.node_location(store.get_node(node_id))
            hints = None
            if location:
                if self._svc is None:
                    import geo_service
                    self._svc = geo_service.create_service("rules", place_names=True)
                place = self._svc.locate_node(location)
                if place is not None:
                    hints = self._svc.hints(place.point,
                                            store.recent_place_names(place.point))
            self._by_node[node_id] = hints
        return self._by_node[node_id]


def run(args, transcriber=None):
    rows = messages(args.since, args.limit, args.node)
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
        return {"done": 0, "failed": 0, "todo": len(todo)}

    t = transcriber or make_transcriber(engine)
    if t is None:
        raise SystemExit("Transcription is off (TRANSCRIPTION_ENGINE=none)")
    hints = Hints()
    done = failed = 0
    t_all = time.time()
    for n, (r, path) in enumerate(todo, 1):
        t0 = time.time()
        head = (f"{n}/{len(todo)} {r['event_uid'][:8]} "
                f"{(r['started_at'] or r['received_at'] or '')[:16]} {r['type']} "
                f"{r.get('node_name') or ''}")
        try:
            text = t.transcribe(path, hints.for_event(r))
        except Exception as e:  # noqa: BLE001 - report and go on
            failed += 1
            log(f"{head}: FAILED {e}")
            continue
        text = (text or "").strip()
        took = time.time() - t0
        store.set_transcription(r["event_uid"], text or None,
                                "done" if text else "error", round(took, 1))
        done += 1
        old = (r["transcription"] or "").strip()
        same = " (same)" if old == text else ""
        log(f"{head}: {getattr(t, '_last_engine', engine)} {took:.1f}s{same}: "
            f"{text[:90]}{'…' if len(text) > 90 else ''}")
    log(f"Done: {done} transcribed, {failed} failed, in {(time.time() - t_all) / 60:.1f} min. "
        "Now rebuild the map: Admin → Karta → Bygg om kartan.")
    return {"done": done, "failed": failed, "todo": len(todo)}


def main(argv=None, transcriber=None):
    ap = argparse.ArgumentParser(prog="python3 -m aggregator.retranscribe",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("--yes", action="store_true", help="transcribe (else only show what it would do)")
    ap.add_argument("--since", help="only messages from this date on (YYYY-MM-DD)")
    ap.add_argument("--limit", type=int, help="only this many, oldest first")
    ap.add_argument("--node", help="only messages this node heard")
    ap.add_argument("--engine", choices=("elevenlabs", "local", "remote"),
                    default=config.TRANSCRIPTION_ENGINE if config.TRANSCRIPTION_ENGINE in
                    ("elevenlabs", "local", "remote") else "local")
    args = ap.parse_args(argv)
    store.init_db(config.DB_PATH)
    return run(args, transcriber)


if __name__ == "__main__":
    from . import privileges
    privileges.drop_privileges()
    main()
