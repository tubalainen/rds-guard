"""Import the history of a standalone RDS Guard into this aggregator.

    docker compose exec aggregator python3 -m aggregator.import_satellite \\
        /import/events.db /import/audio --node "docker01"

Reads a satellite's database (events.db) and audio folder, and adds to this
aggregator, as events of one node:

  - the traffic announcements and emergency broadcasts, with their audio
    (OGG), transcriptions and how they were placed on the map
  - the map's incidents, area warnings and Trafikverket links of those
    messages (only of messages that are imported)
  - the push log of those messages, and the push rules (see below)

Nothing is transcribed or placed again, and nothing is alerted or pushed:
the messages were broadcast long ago.

Safe to run more than once, also next to an uplink that already delivered
some of the events: every event has a stable id (its uplink uid, or one
derived from the satellite's row), and ids the aggregator already has are
skipped together with everything attached to them.

Push rules: if the aggregator still has only its default rules, they are
replaced by the satellite's; otherwise only rules it does not have yet are
added.

The source is only read.  Prints one line per event while it works.
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
import uuid
from pathlib import Path

import notify_rules

from . import config, nodes, store

# Fixed namespace: the same satellite row always gets the same uid
_UID_NAMESPACE = uuid.UUID("5b0c3b1e-6f0a-4d43-9a57-2f1d1c6f7a10")

_TYPES = ("traffic", "emergency")
_UNFINISHED = ("recording", "saving", "queued", "transcribing")


def say(msg):
    print(msg, file=sys.stderr, flush=True)


def z(ts):
    """'2026-11-24T08:15:02' (satellite) -> '2026-11-24T08:15:02Z'."""
    if not ts:
        return None
    return str(ts).replace(" ", "T")[:19] + "Z"


def event_uid(row):
    """The event's uplink uid, or a stable one derived from the row."""
    uid = (row["event_uid"] or "").strip().lower() if "event_uid" in row.keys() else ""
    if uid:
        return uid
    key = f"{row['id']}|{row['pi']}|{row['started_at'] or row['created_at']}"
    return str(uuid.uuid5(_UID_NAMESPACE, key))


def open_source(path):
    """Open the satellite database read-only."""
    if not os.path.isfile(path):
        raise SystemExit(f"No such database: {path}")
    uri = Path(path).resolve().as_uri()
    try:
        conn = sqlite3.connect(uri + "?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM events LIMIT 1").fetchall()
    except sqlite3.OperationalError:
        # A read-only mount cannot hold SQLite's lock files: read the file
        # as it is (stop the satellite first for a consistent copy)
        conn = sqlite3.connect(uri + "?immutable=1", uri=True)
        conn.execute("SELECT 1 FROM events LIMIT 1").fetchall()
        say("Note: the database is read as the file stands (read-only mount). "
            "Events the running satellite has not written to the file yet are "
            "not seen; run the import again later, or stop the satellite first.")
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _json(text, default):
    try:
        value = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _transcription_status(row):
    if row.get("transcription"):
        return "done"
    status = row.get("transcription_status")
    if not row.get("audio_path"):
        return None
    if status in _UNFINISHED or status is None:
        # Never finished on the satellite.  Not 'queued': the aggregator
        # would transcribe it at its next start.
        return "error"
    return status


class Importer:
    def __init__(self, src, audio_dir, node, dry_run=False):
        self.src = src
        self.audio_dir = Path(audio_dir) if audio_dir else None
        self.node = node
        self.dry_run = dry_run
        self.src_tables = _tables(src)
        self.uid_of = {}          # satellite event id -> uid (imported only)
        self.cutoff = store.iso_ago(config.EVENT_RETENTION_DAYS * 86400)
        self.counts = {"events": 0, "skipped": 0, "too_old": 0, "audio": 0,
                       "audio_missing": 0,
                       "incidents": 0, "areas": 0, "notify_log": 0, "rules": 0,
                       "trv": 0}

    # -- events --------------------------------------------------------------

    def import_events(self):
        cols = _columns(self.src, "events")
        rows = [dict(r) for r in self.src.execute("SELECT * FROM events ORDER BY id")]
        rows = [r for r in rows if r.get("type") in _TYPES]
        total = len(rows)
        say(f"{total} traffic announcements and emergency broadcasts in the satellite's database")
        conn = store._conn()
        for i, row in enumerate(rows, 1):
            for c in ("event_uid", "audio_path", "transcription", "transcription_status",
                      "transcription_duration_sec", "transcription_corrected",
                      "geo_status", "geo_engine"):
                if c not in cols:
                    row[c] = None
            uid = event_uid(_RowView(row))
            when = row.get("started_at") or row.get("created_at")
            label = f"{i}/{total} #{row['id']} {when or '?'} {row.get('station_ps') or row['pi']}"
            if conn.execute("SELECT 1 FROM events WHERE event_uid = ?", (uid,)).fetchone():
                self.counts["skipped"] += 1
                say(f"{label}: already here ({uid}), skipped")
                continue
            received = z(row.get("created_at")) or z(when)
            if received and received < self.cutoff:
                # The aggregator's retention would delete it within the hour
                self.counts["too_old"] += 1
                say(f"{label}: older than the aggregator keeps events "
                    f"({config.EVENT_RETENTION_DAYS} days), skipped")
                continue
            note = self._insert_event(conn, row, uid)
            self.uid_of[row["id"]] = uid
            self.counts["events"] += 1
            if not self.dry_run:
                conn.commit()     # one event at a time: the aggregator keeps running
            say(f"{label}: imported as {uid}{note}")

    def _insert_event(self, conn, row, uid):
        data = _json(row.get("data"), {})
        station = data.get("station") if isinstance(data.get("station"), dict) else {}
        radiotext = _json(row.get("radiotext"), [])
        started, ended = z(row.get("started_at")), z(row.get("ended_at"))
        received = z(row.get("created_at")) or started or store.now_iso()

        audio_name, note = self._copy_audio(row, uid)
        status = _transcription_status(dict(row, audio_path=audio_name))
        if self.dry_run:
            return note
        conn.execute(
            "INSERT INTO events (event_uid, node_id, rev, type, state, pi, "
            "station_ps, long_ps, frequency, prog_type, radiotext, started_at, "
            "ended_at, duration_sec, sent_at, received_at, updated_at, "
            "end_received_at, audio_path, audio_received_at, transcription, "
            "transcription_status, transcription_duration_sec, "
            "transcription_corrected, geo_status, geo_engine, alert_status, "
            "alert_published_at) VALUES "
            "(?, ?, 0, ?, 'end', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
            "?, ?, ?, ?, ?, 'imported', ?)",
            (uid, self.node["id"], row["type"], row["pi"], row.get("station_ps"),
             station.get("long_ps"), row.get("frequency"),
             data.get("prog_type") or None,
             json.dumps([str(t) for t in radiotext], ensure_ascii=False),
             started, ended, row.get("duration_sec"), ended or started,
             received, store.now_iso(), ended or received, audio_name,
             (ended or received) if audio_name else None,
             row.get("transcription"), status,
             row.get("transcription_duration_sec"),
             row.get("transcription_corrected"), row.get("geo_status"),
             row.get("geo_engine"), store.now_iso()))
        return note

    def _copy_audio(self, row, uid):
        """Copy the event's OGG into the aggregator's audio folder."""
        if not row.get("audio_path"):
            return None, ""
        name = os.path.basename(row["audio_path"])
        if self.audio_dir is None:
            self.counts["audio_missing"] += 1
            return None, ", audio not imported (no audio folder given)"
        src = self.audio_dir / name
        if not name.lower().endswith(".ogg"):
            ogg = src.with_suffix(".ogg")
            src = ogg if ogg.exists() else src
        try:
            with open(src, "rb") as f:
                ok = f.read(4) == b"OggS"
        except OSError:
            self.counts["audio_missing"] += 1
            return None, f", audio {name} missing"
        if not ok:
            self.counts["audio_missing"] += 1
            return None, f", audio {src.name} is not OGG, skipped"
        dest_name = f"{uid}.ogg"
        if not self.dry_run:
            Path(config.AUDIO_DIR).mkdir(parents=True, exist_ok=True)
            dest = Path(config.AUDIO_DIR) / dest_name
            tmp = dest.with_suffix(".ogg.tmp")
            shutil.copyfile(src, tmp)
            os.replace(tmp, dest)
        self.counts["audio"] += 1
        return dest_name, ", with audio"

    # -- the map -------------------------------------------------------------

    def import_map(self):
        if "incidents" not in self.src_tables or not self.uid_of:
            return
        conn = store._conn()
        inc_cols = _columns(self.src, "incidents")
        have_cols = _columns(conn, "incidents")
        cols = [c for c in inc_cols if c in have_cols and c != "id"]
        # Messages already on the aggregator's map (an earlier import whose
        # events were deleted since): never add their incidents twice
        placed = {r[0] for r in conn.execute(
            "SELECT event_key FROM incident_events UNION "
            "SELECT event_key FROM area_warnings")}
        links = {}
        for r in self.src.execute("SELECT * FROM incident_events"):
            r = dict(r)
            try:
                event_id = int(r["event_key"])
            except (TypeError, ValueError):
                continue
            uid = self.uid_of.get(event_id)
            if uid is not None and uid not in placed:
                links.setdefault(r["incident_id"], []).append(dict(r, event_key=uid))
        trv_ids = set()
        for inc_id, inc_links in sorted(links.items()):
            inc = dict(self.src.execute("SELECT * FROM incidents WHERE id = ?",
                                        (inc_id,)).fetchone())
            for c in ("first_seen", "last_seen"):
                inc[c] = z(inc.get(c))
            if inc.get("trv_id"):
                trv_ids.add(inc["trv_id"])
            self.counts["incidents"] += 1
            say(f"incident {inc.get('label')}: {len(inc_links)} message(s)")
            if self.dry_run:
                continue
            cur = conn.execute(
                f"INSERT INTO incidents ({', '.join(cols)}) VALUES "
                f"({', '.join('?' * len(cols))})", [inc.get(c) for c in cols])
            new_id = cur.lastrowid
            for link in inc_links:
                conn.execute(
                    "INSERT OR IGNORE INTO incident_events (incident_id, event_key, "
                    "seen_at, kind, evidence) VALUES (?, ?, ?, ?, ?)",
                    (new_id, link["event_key"], z(link.get("seen_at")),
                     link.get("kind"), link.get("evidence")))
        if "area_warnings" in self.src_tables:
            for r in self.src.execute("SELECT * FROM area_warnings ORDER BY id"):
                r = dict(r)
                try:
                    uid = self.uid_of.get(int(r["event_key"]))
                except (TypeError, ValueError):
                    uid = None
                if uid is None or uid in placed:
                    continue
                self.counts["areas"] += 1
                if not self.dry_run:
                    conn.execute(
                        "INSERT INTO area_warnings (event_key, seen_at, area, road, "
                        "what, evidence, engine) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (uid, z(r.get("seen_at")), r.get("area"), r.get("road"),
                         r.get("what"), r.get("evidence"), r.get("engine")))
        self._import_trv(conn, trv_ids)
        if not self.dry_run:
            conn.commit()

    def _import_trv(self, conn, ids):
        """The Trafikverket deviations the imported incidents link to."""
        if not ids or "trv_deviations" not in self.src_tables:
            return
        src_cols = _columns(self.src, "trv_deviations")
        cols = [c for c in _columns(conn, "trv_deviations") if c in src_cols]
        for dev_id in sorted(ids):
            if conn.execute("SELECT 1 FROM trv_deviations WHERE id = ?",
                            (dev_id,)).fetchone():
                continue
            row = self.src.execute("SELECT * FROM trv_deviations WHERE id = ?",
                                   (dev_id,)).fetchone()
            if row is None:
                continue
            row = dict(row)
            for c in ("first_seen", "last_seen", "changed_at"):
                if c in row:
                    row[c] = z(row[c])
            self.counts["trv"] += 1
            if self.dry_run:
                continue
            conn.execute(
                f"INSERT INTO trv_deviations ({', '.join(cols)}) VALUES "
                f"({', '.join('?' * len(cols))})", [row.get(c) for c in cols])
            if "trv_deviation_versions" in self.src_tables:
                for v in self.src.execute(
                        "SELECT * FROM trv_deviation_versions WHERE deviation_id = ? "
                        "ORDER BY id", (dev_id,)):
                    conn.execute(
                        "INSERT INTO trv_deviation_versions (deviation_id, seen_at, "
                        "version_time, changes) VALUES (?, ?, ?, ?)",
                        (dev_id, z(v["seen_at"]), v["version_time"], v["changes"]))

    # -- push rules and log --------------------------------------------------

    def import_notify(self):
        conn = store._conn()
        if "notify_log" in self.src_tables and self.uid_of:
            for r in self.src.execute("SELECT * FROM notify_log ORDER BY id"):
                r = dict(r)
                uid = self.uid_of.get(r.get("event_id"))
                if uid is None or r.get("test"):
                    continue
                self.counts["notify_log"] += 1
                if not self.dry_run:
                    conn.execute(
                        "INSERT INTO notify_log (event_uid, rule_id, rule_name, topic, "
                        "title, status, test, created_at) VALUES (?, NULL, ?, ?, ?, ?, 0, ?)",
                        (uid, r.get("rule_name"), r.get("topic"), r.get("title"),
                         r.get("status"), z(r.get("created_at"))))
        if "notify_rules" not in self.src_tables:
            return
        theirs = [_json(r["data"], {}) for r in
                  self.src.execute("SELECT data FROM notify_rules ORDER BY id")]
        theirs = [r for r in theirs if r]
        ours = store.list_rules()
        ours_keys = [_rule_key(r) for r in ours]
        seeded = bool(conn.execute(
            "SELECT 1 FROM settings WHERE key = 'notify_seeded'").fetchone())
        defaults = sorted(_rule_key(r) for r in notify_rules.DEFAULT_RULES)
        only_defaults = not ours or sorted(ours_keys) == defaults
        if only_defaults and theirs:
            say(f"push rules: the satellite's {len(theirs)} rules replace the "
                f"aggregator's {len(ours)} default rules")
            if not self.dry_run:
                for r in ours:
                    store.delete_rule(r["id"])
            ours_keys = []
        for rule in theirs:
            key = _rule_key(rule)
            if key in ours_keys:
                continue
            try:
                clean = notify_rules.validate(_strip(rule))
            except (ValueError, TypeError) as e:
                say(f"push rule {rule.get('name')!r} skipped: {e}")
                continue
            self.counts["rules"] += 1
            say(f"push rule {clean.get('name')!r} added")
            if not self.dry_run:
                store.create_rule(clean)
            ours_keys.append(key)
        if theirs and not seeded and not self.dry_run:
            # The aggregator must not add its default rules next to these
            # at its next start
            conn.execute("INSERT OR IGNORE INTO settings (key, value) "
                         "VALUES ('notify_seeded', '1')")
        if not self.dry_run:
            conn.commit()

    def run(self):
        self.import_events()
        self.import_map()
        self.import_notify()
        c = self.counts
        say("")
        say(("Dry run, nothing written: " if self.dry_run else "Done: ")
            + f"{c['events']} events imported ({c['audio']} with audio"
            + (f", {c['audio_missing']} without their audio file" if c["audio_missing"] else "")
            + f"), {c['skipped']} already here"
            + (f", {c['too_old']} older than the {config.EVENT_RETENTION_DAYS} days the "
               "aggregator keeps (raise AGGREGATOR_RETENTION_DAYS to bring them)"
               if c["too_old"] else "")
            + f"; {c['incidents']} map incidents, "
            f"{c['areas']} area warnings, {c['trv']} Trafikverket deviations, "
            f"{c['notify_log']} push log entries, {c['rules']} push rules")
        return c


class _RowView:
    """dict -> object with keys() and [] (like sqlite3.Row)."""

    def __init__(self, d):
        self._d = d

    def keys(self):
        return self._d.keys()

    def __getitem__(self, k):
        return self._d.get(k)


def _strip(rule):
    return {k: v for k, v in (rule or {}).items() if k not in ("id", "created_at", "updated_at")}


def _rule_key(rule):
    """A rule's content, comparable whether stored raw or validated."""
    rule = _strip(rule)
    try:
        rule = notify_rules.validate(rule)
    except (ValueError, TypeError):
        pass
    return json.dumps(rule, sort_keys=True, ensure_ascii=False)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m aggregator.import_satellite",
        description="Import a standalone RDS Guard's history into this aggregator.")
    parser.add_argument("database", help="the satellite's events.db")
    parser.add_argument("audio", nargs="?", help="the satellite's audio folder")
    parser.add_argument("--node", required=True,
                        help="node the events belong to (created if it does not exist)")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be imported, write nothing")
    args = parser.parse_args(argv)

    from . import privileges
    src = open_source(args.database)       # as root: the mount may be root's
    privileges.drop_privileges()
    privileges.require_writable()
    Path(config.AUDIO_DIR).mkdir(parents=True, exist_ok=True)
    store.init_db(config.DB_PATH)

    name = nodes.clean_name(args.node)
    node = store.get_node_by_name(name)
    token = None
    if node is None:
        if args.dry_run:
            node = {"id": -1, "name": name}
            say(f"Node '{name}' does not exist; it would be created")
        else:
            node, token = store.create_node(name)
            say(f"Created node '{name}'")
    Importer(src, args.audio, node, dry_run=args.dry_run).run()
    if token:
        say("")
        say("To let the satellite report to this node, add these lines to its .env "
            "and restart it (the token is shown only now):")
        print(nodes.setup_snippet(name, token))


if __name__ == "__main__":
    main()
