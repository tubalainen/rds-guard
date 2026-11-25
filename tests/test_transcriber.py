"""Transcriber: model selection, ElevenLabs request and fallback."""

import pytest

import transcriber
from transcriber import Transcriber, parse_keyterms, resolve_model


class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


@pytest.fixture
def clip(tmp_path):
    path = tmp_path / "42.wav"
    path.write_bytes(b"RIFF....WAVE")
    return path


@pytest.fixture
def no_bundle(tmp_path, monkeypatch):
    monkeypatch.setattr(transcriber, "BUNDLED_SWEDISH_MODEL",
                        str(tmp_path / "missing"))


def test_explicit_model_wins(no_bundle):
    assert resolve_model("large-v3", "sv") == "large-v3"


def test_swedish_uses_hub_model_without_bundle(no_bundle):
    assert resolve_model("", "sv") == "KBLab/kb-whisper-small"


def test_swedish_prefers_bundled_model(tmp_path, monkeypatch):
    bundle = tmp_path / "kb-strict"
    bundle.mkdir()
    (bundle / "model.bin").write_bytes(b"")
    monkeypatch.setattr(transcriber, "BUNDLED_SWEDISH_MODEL", str(bundle))
    assert resolve_model(None, "sv") == str(bundle)


def test_other_language_uses_generic_model(no_bundle):
    assert resolve_model("", "en") == "small"


def test_parse_keyterms_drops_invalid_and_duplicates():
    raw = " E18 , Karlstad,,E18, väg  222, a b c d e f, bad<term>, " + "x" * 51
    assert parse_keyterms(raw) == ["E18", "Karlstad", "väg 222"]


def make_el(**kw):
    args = dict(engine="elevenlabs", language="sv",
                elevenlabs_api_key="key", elevenlabs_model="scribe_v3",
                elevenlabs_keyterms="E18,Karlstad")
    args.update(kw)
    return Transcriber(**args)


def test_elevenlabs_request(clip, monkeypatch):
    sent = {}

    def fake_post(url, headers, data, files, timeout):
        sent.update(url=url, headers=headers, data=data, timeout=timeout)
        return FakeResponse(payload={"text": "  På E18 vid Karlstad.  "})

    monkeypatch.setattr("requests.post", fake_post)
    text = make_el().transcribe(clip)

    assert text == "På E18 vid Karlstad."
    assert sent["url"] == transcriber.ELEVENLABS_URL
    assert sent["headers"] == {"xi-api-key": "key"}
    fields = sent["data"]
    assert ("model_id", "scribe_v3") in fields
    assert ("language_code", "sv") in fields
    assert ("numbers_format", "digits") in fields
    assert [v for k, v in fields if k == "keyterms"] == ["E18", "Karlstad"]


def test_scribe_v2_omits_numbers_format(clip, monkeypatch):
    sent = {}

    def fake_post(url, headers, data, files, timeout):
        sent["data"] = data
        return FakeResponse(payload={"text": "ok"})

    monkeypatch.setattr("requests.post", fake_post)
    make_el(elevenlabs_model="scribe_v2").transcribe(clip)
    assert "numbers_format" not in dict(sent["data"])


def test_elevenlabs_error_falls_back_to_local(clip, monkeypatch):
    monkeypatch.setattr("requests.post",
                        lambda *a, **k: FakeResponse(503, text="busy"))
    t = make_el()
    monkeypatch.setattr(t, "_load_local_model",
                        lambda: setattr(t, "_model", object()))
    monkeypatch.setattr(t, "_transcribe_local", lambda path: "lokal text")
    assert t.transcribe(clip) == "lokal text"


def test_elevenlabs_error_without_fallback_raises(clip, monkeypatch):
    monkeypatch.setattr("requests.post",
                        lambda *a, **k: FakeResponse(401, text="bad key"))
    with pytest.raises(RuntimeError, match="401"):
        make_el(fallback="none").transcribe(clip)


def test_missing_key_uses_fallback(clip, monkeypatch):
    def no_call(*a, **k):
        raise AssertionError("API must not be called without a key")

    monkeypatch.setattr("requests.post", no_call)
    t = make_el(elevenlabs_api_key="")
    t._model = object()
    monkeypatch.setattr(t, "_transcribe_local", lambda path: "lokal")
    assert t.transcribe(clip) == "lokal"


def test_factory_without_key_runs_locally(no_bundle):
    t = transcriber.create_transcriber("elevenlabs", "sv",
                                       elevenlabs_api_key="")
    assert t.engine == "local"


def test_factory_with_key_uses_elevenlabs(no_bundle):
    t = transcriber.create_transcriber("elevenlabs", "sv",
                                       elevenlabs_api_key="key")
    assert t.engine == "elevenlabs"
    assert t.fallback == "local"
