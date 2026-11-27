"""aggregator.retranscribe: transcribe the stored recordings again."""

from aggregator import config, retranscribe

UID_A = "a0000000-0000-4000-8000-000000000001"
UID_B = "b0000000-0000-4000-8000-000000000002"


class FakeTranscriber:
    _last_engine = "fake"

    def __init__(self):
        self.calls = []

    def transcribe(self, path, hints=None):
        self.calls.append(path)
        if path.endswith(f"{UID_B}.ogg"):
            raise RuntimeError("boom")
        return "E18 vid Bergviksmotet, en olycka."


def _event(store, node, uid, start):
    store.upsert_event(node["id"], uid, {
        "rev": 1, "type": "traffic", "state": "end", "pi": f"0x{uid[:4]}",
        "station_ps": "P4", "long_ps": None, "frequency": "103.5M",
        "prog_type": None, "radiotext": [], "started_at": start,
        "ended_at": start, "duration_sec": 60, "sent_at": start})
    store.set_event_audio(node["id"], uid, f"{uid}.ogg", None, 10)
    store.choose_audio(store.get_event(uid)["id"], None, 0)
    store.set_transcription(uid, "gammal text", "done")


def test_dry_run_then_transcribe(agg_store, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(config, "TRANSCRIPTION_PLACE_NAMES", False)
    node, _ = agg_store.create_node("docker01")
    audio = tmp_path / "audio"
    audio.mkdir(exist_ok=True)
    for uid in (UID_A, UID_B):
        (audio / f"{uid}.ogg").write_bytes(b"OggS")
    _event(agg_store, node, UID_A, "2026-11-20T10:00:00Z")
    _event(agg_store, node, UID_B, "2026-11-21T10:00:00Z")

    tr = FakeTranscriber()
    res = retranscribe.main(["--engine", "local"], transcriber=tr)
    assert res == {"done": 0, "failed": 0, "todo": 2} and tr.calls == []
    assert "Nothing changed" in capsys.readouterr().err

    res = retranscribe.main(["--yes", "--engine", "local"], transcriber=tr)
    assert res == {"done": 1, "failed": 1, "todo": 2}
    err = capsys.readouterr().err
    assert "1/2 a0000000" in err and "FAILED boom" in err
    assert agg_store.get_event(UID_A)["transcription"] == "E18 vid Bergviksmotet, en olycka."
    assert agg_store.get_event(UID_B)["transcription"] == "gammal text"

    res = retranscribe.main(["--yes", "--engine", "local", "--since", "2026-11-21",
                             "--node", "docker01"], transcriber=FakeTranscriber())
    assert res["todo"] == 1
    assert retranscribe.main(["--node", "nobody", "--engine", "local"])["todo"] == 0
