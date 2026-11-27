"""tools/retranscribe.py: transcribe stored recordings again."""

import os
import sys

import event_store

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
import retranscribe  # noqa: E402


class FakeTranscriber:
    _last_engine = "fake"

    def __init__(self):
        self.calls = []

    def transcribe(self, path, hints=None):
        self.calls.append(path)
        if path.endswith("bad.ogg"):
            raise RuntimeError("boom")
        return "E18 vid Bergviksmotet, en olycka."


def setup(tmp_path, monkeypatch):
    db = str(tmp_path / "events.db")          # the store fixture's database
    audio = tmp_path / "audio"
    audio.mkdir()
    (audio / "a.ogg").write_bytes(b"x")
    (audio / "bad.ogg").write_bytes(b"x")
    ids = []
    for name, when in (("a.ogg", "2026-11-20T08:00:00"), ("bad.ogg", "2026-11-21T08:00:00"),
                       ("gone.ogg", "2026-11-22T08:00:00")):
        i = event_store.insert_event("traffic", "warning", "end", "0x9E0A", {},
                                     started_at=when)
        event_store.update_event_audio(i, name)
        event_store.update_event_transcription(i, "gammal text")
        ids.append(i)
    monkeypatch.setattr(retranscribe.config, "AUDIO_DIR", str(audio))
    monkeypatch.setattr(retranscribe.config, "TRANSCRIPTION_PLACE_NAMES", False)
    fake = FakeTranscriber()
    monkeypatch.setattr(retranscribe, "make_transcriber", lambda engine: fake)
    return db, ids, fake


def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["retranscribe.py", *argv])
    retranscribe.main()


def test_without_yes_nothing_changes(store, tmp_path, monkeypatch, capsys):
    db, ids, fake = setup(tmp_path, monkeypatch)
    run(monkeypatch, "--db", db, "--engine", "local")
    assert fake.calls == []
    assert event_store.get_event(ids[0])["transcription"] == "gammal text"
    err = capsys.readouterr().err
    assert "2 messages with a recording" in err and "1 whose audio file is gone" in err


def test_yes_replaces_the_texts_and_goes_on_after_a_failure(store, tmp_path, monkeypatch, capsys):
    db, ids, fake = setup(tmp_path, monkeypatch)
    run(monkeypatch, "--db", db, "--engine", "local", "--yes")
    assert len(fake.calls) == 2
    assert event_store.get_event(ids[0])["transcription"].startswith("E18 vid")
    assert event_store.get_event(ids[1])["transcription"] == "gammal text"
    err = capsys.readouterr().err
    assert "1/2 ev" in err and "FAILED boom" in err and "1 transcribed, 1 failed" in err


def test_since_and_limit(store, tmp_path, monkeypatch):
    db, ids, fake = setup(tmp_path, monkeypatch)
    assert [r["id"] for r in retranscribe.messages(db, since="2026-11-21")] == ids[1:]
    assert [r["id"] for r in retranscribe.messages(db, limit=1)] == ids[:1]
