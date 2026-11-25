"""Shared fixtures: a fresh SQLite event store per test."""

import pytest

import event_store


@pytest.fixture
def store(tmp_path):
    """Point event_store at a temporary database and reset module state."""
    event_store._local.conn = None
    event_store.disable_outbox()
    event_store.init_db(str(tmp_path / "events.db"))
    yield event_store
    conn = getattr(event_store._local, "conn", None)
    if conn is not None:
        conn.close()
    event_store._local.conn = None
    event_store.disable_outbox()
