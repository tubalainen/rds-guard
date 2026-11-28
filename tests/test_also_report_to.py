"""ALSO_REPORT_TO (issue #106): one satellite, several aggregators. Each
has its own queue, identity and approval; JOIN_KEY, UPLINK_TOKEN and the
commands stay with the satellite's own aggregator."""

import sqlite3

import uplink as up
from test_satellite import _run
from test_uplink import FakeHttp, FakeResponse, _traffic_event

FRIEND = "https://rds.friend.example"


class Answer(FakeResponse):
    def __init__(self, status, body=None):
        super().__init__(status)
        self._body = body or {}

    def json(self):
        return self._body


def _links(store, tmp_path, own_token="tok-own"):
    """The own aggregator and a friend's, as the satellite starts them."""
    own_http, friend_http = FakeHttp(), FakeHttp()
    own = up.Uplink("https://agg.example", own_token, tmp_path, node_name="Kalle",
                    http=own_http, join_key="hemlig")
    friend = up.Uplink(FRIEND, "", tmp_path, node_name="Kalle", http=friend_http,
                       target=FRIEND)
    store.enable_outbox(lambda: [u.notify() for u in (own, friend)],
                        targets=[store.PRIMARY, FRIEND])
    return own, friend


def _drain(u):
    outcomes = []
    while (o := u.process_one()) is not None:
        outcomes.append(o)
        if o not in (up.OK, up.DROP):
            break
    return outcomes


def test_each_aggregator_gets_every_event_and_its_audio(store, tmp_path):
    own, friend = _links(store, tmp_path)
    event_id = _traffic_event(store, tmp_path)
    assert store.outbox_depth(store.PRIMARY) == store.outbox_depth(FRIEND) > 0
    assert _drain(own) == [up.OK, up.OK]
    assert store.outbox_depth(store.PRIMARY) == 0
    assert store.outbox_depth(FRIEND) > 0           # the friend's is still queued
    assert _drain(friend) == [up.OK, up.OK]
    uid = store.get_event(event_id)["event_uid"]
    for u, base in ((own, "https://agg.example"), (friend, FRIEND)):
        urls = [c["url"] for c in u._http.calls]
        assert urls == [f"{base}/api/v1/ingest/events/{uid}",
                        f"{base}/api/v1/ingest/events/{uid}/audio"]
    assert store.outbox_depth() == 0


def test_one_aggregator_down_does_not_hold_up_the_other(store, tmp_path):
    own, friend = _links(store, tmp_path)
    _traffic_event(store, tmp_path)
    friend._http.answers = [OSError("unreachable")]
    assert _drain(friend) == [up.RETRY]
    assert _drain(own) == [up.OK, up.OK]
    assert store.outbox_depth(store.PRIMARY) == 0 and store.outbox_depth(FRIEND) > 0


def test_an_event_is_kept_until_every_aggregator_has_it(store, tmp_path):
    own, _ = _links(store, tmp_path)
    event_id = _traffic_event(store, tmp_path)
    _drain(own)
    conn = store._conn()
    conn.execute("UPDATE events SET created_at = '2000-01-01T00:00:00'")
    conn.commit()
    store.purge(7, str(tmp_path))
    assert store.get_event(event_id) is not None     # the friend has not had it
    assert (tmp_path / f"{event_id}.ogg").exists()


def test_each_aggregator_has_its_own_identity(store, tmp_path):
    """The friend's aggregator never learns the token the own one knows the
    node by, nor JOIN_KEY; the own one's identity keeps its settings."""
    own, friend = _links(store, tmp_path, own_token="")
    friend._http.answers = [FakeResponse(202)]
    own._http.answers = [FakeResponse(202)]
    assert own.join_once() == "pending" and friend.join_once() == "pending"
    own_call, friend_call = own._http.calls[0], friend._http.calls[0]
    assert own_call["json"]["join_key"] == "hemlig"
    assert "join_key" not in friend_call["json"]
    assert friend_call["url"] == f"{FRIEND}/api/v1/ingest/join"
    assert own_call["json"]["join_id"] != friend_call["json"]["join_id"]
    assert own_call["headers"]["Authorization"] != friend_call["headers"]["Authorization"]
    assert store.get_setting("uplink_token")             # as before ALSO_REPORT_TO
    assert store.get_setting(f"uplink_token@{FRIEND}")
    # Approved by the friend only: the own aggregator still waits
    friend._http.answers = [FakeResponse(200)]
    assert friend.join_once() == "approved"
    assert store.get_setting(f"uplink_joined@{FRIEND}") == "1"
    assert store.get_setting("uplink_joined") is None
    again = up.Uplink(FRIEND, "", tmp_path, http=FakeHttp(), target=FRIEND)
    assert again._ready.is_set() and again._join_id == friend._join_id


def test_only_the_own_aggregators_commands_are_run(store, tmp_path):
    own, friend = _links(store, tmp_path)
    ran = []
    own.on_commands = ran.extend                  # as the satellite sets it
    command = {"commands": [{"type": "recalibrate", "id": 1, "receiver": "tak"}]}
    friend._http.answers = [Answer(200, command)]
    friend._ready.set()
    assert friend.send_heartbeat() == up.OK
    assert ran == []
    own._http.answers = [Answer(200, command)]
    assert own.send_heartbeat() == up.OK
    assert ran == command["commands"]


def test_heartbeat_queue_depth_is_per_aggregator(store, tmp_path):
    own, friend = _links(store, tmp_path)
    _traffic_event(store, tmp_path)
    _drain(own)
    friend._ready.set()
    own.send_heartbeat()
    friend.send_heartbeat()
    assert own._http.calls[-1]["json"]["queue_depth"] == 0
    assert friend._http.calls[-1]["json"]["queue_depth"] > 0


def test_an_aggregator_taken_out_of_also_report_to_loses_its_queue(store, tmp_path):
    _links(store, tmp_path)
    _traffic_event(store, tmp_path)
    assert store.outbox_depth(FRIEND) > 0
    store.enable_outbox(targets=[store.PRIMARY])      # next start, without it
    assert store.outbox_depth(FRIEND) == 0 and store.outbox_depth(store.PRIMARY) > 0


def test_an_older_database_gets_the_target_column(store, tmp_path):
    """A queue from before ALSO_REPORT_TO belongs to the own aggregator."""
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT "
                "NOT NULL, event_id INTEGER, event_uid TEXT NOT NULL, created_at TEXT "
                "NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')))")
    old.execute("INSERT INTO outbox (kind, event_id, event_uid) VALUES ('delete', NULL, 'u1')")
    old.commit()
    old.close()
    store._local.conn = None
    store.init_db(str(path))
    assert store.outbox_head()["event_uid"] == "u1"
    assert store.outbox_head(target=FRIEND) is None


def test_also_report_to_setting():
    code = "import config; print(config.ALSO_REPORT_TO, config.problems())"
    out = _run(code, AGGREGATOR_URL="http://aggregator:8081",
               ALSO_REPORT_TO=" https://rds.example.com/ , http://aggregator:8081,"
                              "https://rds.example.com")
    assert out.stdout.strip() == "['https://rds.example.com'] []"
    out = _run(code, AGGREGATOR_URL="http://aggregator:8081", ALSO_REPORT_TO="rds.example.com")
    assert "ALSO_REPORT_TO: each address must start with http" in out.stdout
