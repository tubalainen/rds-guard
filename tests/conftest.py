"""Shared fixtures: the satellite's database, and a satellite database as it
was before v1.0 (for the aggregator's import and the satellite's move)."""

import json
import sqlite3
import time
import uuid

import pytest

import event_store
import geolocate
import notify_rules
import trafikverket


@pytest.fixture
def store(tmp_path):
    """The satellite's database on a temporary file."""
    event_store._local.conn = None
    event_store.disable_outbox()
    event_store.init_db(str(tmp_path / "satellite.db"))
    yield event_store
    conn = getattr(event_store._local, "conn", None)
    if conn is not None:
        conn.close()
    event_store._local.conn = None
    event_store.disable_outbox()


class LegacySatelliteDB:
    """events.db the way a satellite before v1.0 wrote it: events with
    their transcriptions and map, push rules and log, outbox, settings."""

    def __init__(self, path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
                severity TEXT NOT NULL, state TEXT NOT NULL, pi TEXT NOT NULL,
                station_ps TEXT, frequency TEXT, radiotext TEXT, data TEXT NOT NULL,
                started_at TEXT, ended_at TEXT, duration_sec INTEGER,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
                audio_path TEXT, transcription TEXT, transcription_status TEXT,
                transcription_duration_sec REAL, event_uid TEXT,
                transcription_corrected TEXT, geo_status TEXT, geo_engine TEXT);
            CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                event_id INTEGER, event_uid TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')));
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE notify_rules (id INTEGER PRIMARY KEY AUTOINCREMENT,
                data TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
                updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')));
            CREATE TABLE notify_log (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER,
                rule_id INTEGER, rule_name TEXT, topic TEXT, title TEXT,
                status TEXT NOT NULL, test INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')));
        """)
        geolocate.init_tables(self.conn)
        trafikverket.init_tables(self.conn)
        self.conn.commit()

    def _conn(self):
        return self.conn

    def insert_event(self, event_type, severity, state, pi, data_payload,
                     station_ps=None, frequency=None, radiotext=None,
                     started_at=None, ended_at=None, queued=False):
        uid = str(uuid.uuid4())
        cur = self.conn.execute(
            "INSERT INTO events (type, severity, state, pi, station_ps, frequency, "
            "radiotext, data, started_at, ended_at, event_uid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_type, severity, state, pi, station_ps, frequency,
             json.dumps(radiotext or []), json.dumps(data_payload), started_at,
             ended_at, uid))
        if queued:
            self.conn.execute("INSERT INTO outbox (kind, event_id, event_uid) "
                              "VALUES ('event', ?, ?)", (cur.lastrowid, uid))
        self.conn.commit()
        return cur.lastrowid

    def end_event(self, event_id, ended_at, duration_sec, radiotext_list=None):
        self.conn.execute("UPDATE events SET state = 'end', ended_at = ?, "
                          "duration_sec = ? WHERE id = ?", (ended_at, duration_sec, event_id))
        self.conn.commit()

    def update_event_audio(self, event_id, audio_path, queued=False):
        self.conn.execute("UPDATE events SET audio_path = ? WHERE id = ?",
                          (audio_path, event_id))
        if queued:
            uid = self.conn.execute("SELECT event_uid FROM events WHERE id = ?",
                                    (event_id,)).fetchone()[0]
            self.conn.execute("INSERT INTO outbox (kind, event_id, event_uid) "
                              "VALUES ('audio', ?, ?)", (event_id, uid))
        self.conn.commit()

    def update_event_transcription(self, event_id, text, status="done", duration_sec=None):
        self.conn.execute("UPDATE events SET transcription = ?, transcription_status = ?, "
                          "transcription_duration_sec = ? WHERE id = ?",
                          (text, status, duration_sec, event_id))
        self.conn.commit()

    def update_event_transcription_status(self, event_id, status):
        self.conn.execute("UPDATE events SET transcription_status = ? WHERE id = ?",
                          (status, event_id))
        self.conn.commit()

    def save_geolocation(self, event_id, result, status, at):
        ids = geolocate.merge_placements(self.conn, result.placements, event_id, at,
                                         "2000-01-01T00:00:00", result.engine)
        geolocate.save_areas(self.conn, result.areas, event_id, at, result.engine)
        self.conn.execute("UPDATE events SET geo_status = ?, geo_engine = ? WHERE id = ?",
                          (status, result.engine, event_id))
        self.conn.commit()
        return ids

    def log_notification(self, event_id, status, rule=None, topic=None, title=None):
        self.conn.execute("INSERT INTO notify_log (event_id, rule_id, rule_name, topic, "
                          "title, status) VALUES (?, ?, ?, ?, ?, ?)",
                          (event_id, (rule or {}).get("id"), (rule or {}).get("name"),
                           topic, title, status))
        self.conn.commit()

    def seed_rules_once(self, defaults):
        for rule in defaults:
            self.create_rule(rule)
        self.conn.execute("INSERT INTO settings (key, value) VALUES ('notify_seeded', '1')")
        self.conn.commit()

    def create_rule(self, rule):
        self.conn.execute("INSERT INTO notify_rules (data) VALUES (?)", (json.dumps(rule),))
        self.conn.commit()

    def set_setting(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                          (key, value))
        self.conn.commit()

    def close(self):
        self.conn.close()


@pytest.fixture
def legacy_db(tmp_path):
    db = LegacySatelliteDB(tmp_path / "events.db")
    yield db
    db.close()


def default_rules():
    return [notify_rules.validate(dict(r)) for r in notify_rules.DEFAULT_RULES]


# Keep the timestamp helpers importable for older tests
now_ts = lambda: time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())  # noqa: E731
