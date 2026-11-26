"""Transcription engine — speech-to-text with local or cloud backends.

Engines, selected by TRANSCRIPTION_ENGINE:
  - "elevenlabs" → ElevenLabs Scribe speech-to-text API (default), with
                   local faster-whisper as fallback when the API fails.
                   Without ELEVENLABS_API_KEY it runs as "local".
  - "local"      → built-in faster-whisper (CPU)
  - "remote"     → external Whisper ASR server via /asr HTTP endpoint
  - "none"       → disabled (returns None transcriber)
"""

import logging
import os
import queue
import threading
import time
from pathlib import Path

log = logging.getLogger("rds-guard")

# KBLab's KB-Whisper (National Library of Sweden) is fine-tuned on 50 000+
# hours of Swedish speech.  At the same size and CPU cost as OpenAI's "small"
# it cuts the Swedish word error rate from ~20-26 % to ~6-7 %, so it is the
# default whenever the transcription language is Swedish.
SWEDISH_DEFAULT_MODEL = "KBLab/kb-whisper-small"
GENERIC_DEFAULT_MODEL = "small"
# The Docker images ship KB-Whisper small in its "strict" (most verbatim)
# style, converted at build time: KBLab only publishes the CTranslate2 file
# for the default style.  Used when present, else the hub model above.
BUNDLED_SWEDISH_MODEL = os.environ.get(
    "BUNDLED_SWEDISH_MODEL", "/opt/models/kb-whisper-small-strict")

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/speech-to-text"
# Free account lookup used at startup to confirm the key works
ELEVENLABS_ACCOUNT_URL = "https://api.elevenlabs.io/v1/user/subscription"
# ElevenLabs keyterm limits: at most 50 characters and 5 words per term
_KEYTERM_FORBIDDEN = set("<>{}[]\\")
_KEYTERM_MAX = 1000


def resolve_model(model_size, language):
    """Pick the local model: explicit setting wins, else a language default."""
    if model_size:
        return model_size
    if (language or "").lower() == "sv":
        if (Path(BUNDLED_SWEDISH_MODEL) / "model.bin").is_file():
            return BUNDLED_SWEDISH_MODEL
        return SWEDISH_DEFAULT_MODEL
    return GENERIC_DEFAULT_MODEL


def parse_keyterms(raw):
    """Split a comma-separated keyterm list, dropping terms ElevenLabs rejects."""
    terms = []
    for term in (raw or "").split(","):
        term = " ".join(term.split())
        if not term:
            continue
        if (len(term) > 50 or len(term.split()) > 5
                or _KEYTERM_FORBIDDEN & set(term)):
            log.warning("Ignoring ElevenLabs keyterm %r (max 50 chars, "
                        "5 words, no <>{}[]\\)", term)
            continue
        if term not in terms:
            terms.append(term)
    return terms[:_KEYTERM_MAX]


def _error_detail(resp):
    """Short reason from an ElevenLabs error response."""
    try:
        detail = resp.json().get("detail", "")
    except (ValueError, AttributeError):
        detail = ""
    if isinstance(detail, dict):
        detail = detail.get("status") or detail.get("message") or ""
    return str(detail or resp.text or "")[:200]


def create_transcriber(engine, language, model_size=None, device=None,
                       remote_url=None, remote_timeout=120,
                       initial_prompt=None, beam_size=5, cpu_threads=0,
                       elevenlabs_api_key=None, elevenlabs_model="scribe_v3",
                       elevenlabs_keyterms="", elevenlabs_timeout=60,
                       fallback="local"):
    """Factory: create a Transcriber based on engine type.

    Returns None if engine is "none" (transcription disabled).
    """
    if engine == "none":
        log.info("Transcription disabled (TRANSCRIPTION_ENGINE=none)")
        return None

    if engine == "elevenlabs" and not (elevenlabs_api_key or "").strip():
        log.info("ElevenLabs: no ELEVENLABS_API_KEY set — transcribing "
                 "locally instead")
        engine = "local"

    t = Transcriber(
        engine=engine,
        language=language,
        model_size=model_size,
        device=device,
        remote_url=remote_url,
        remote_timeout=remote_timeout,
        initial_prompt=initial_prompt,
        beam_size=beam_size,
        cpu_threads=cpu_threads,
        elevenlabs_api_key=elevenlabs_api_key,
        elevenlabs_model=elevenlabs_model,
        elevenlabs_keyterms=elevenlabs_keyterms,
        elevenlabs_timeout=elevenlabs_timeout,
        fallback=fallback,
    )

    if engine == "remote":
        if not remote_url:
            log.error("TRANSCRIPTION_ENGINE=remote but WHISPER_REMOTE_URL "
                      "is empty — transcription will fail")
        else:
            log.info("Transcription engine: remote (%s)", remote_url)
    elif engine == "elevenlabs":
        log.info("Transcription engine: elevenlabs (model=%s, %d keyterms, "
                 "fallback=%s%s)", t.elevenlabs_model, len(t.keyterms),
                 t.fallback,
                 f" {t.model_size}" if t.fallback == "local" else "")
    else:
        log.info("Transcription engine: local (model=%s, device=%s)",
                 t.model_size, t.device)

    return t


class Transcriber:
    """Speech-to-text engine with async job queue."""

    def __init__(self, engine, language, model_size=None, device=None,
                 remote_url=None, remote_timeout=120,
                 initial_prompt=None, beam_size=5, cpu_threads=0,
                 elevenlabs_api_key=None, elevenlabs_model="scribe_v3",
                 elevenlabs_keyterms="", elevenlabs_timeout=60,
                 fallback="local"):
        self._engine = engine
        self._language = language
        self._model_size = resolve_model(model_size, language)
        self._device = device or "cpu"
        self._initial_prompt = initial_prompt or None
        self._beam_size = max(1, int(beam_size or 1))
        self._cpu_threads = max(0, int(cpu_threads or 0))
        self._remote_url = remote_url
        self._remote_timeout = remote_timeout
        self._el_api_key = (elevenlabs_api_key or "").strip()
        self._el_model = (elevenlabs_model or "scribe_v3").strip()
        self._el_keyterms = (elevenlabs_keyterms
                             if isinstance(elevenlabs_keyterms, list)
                             else parse_keyterms(elevenlabs_keyterms))
        self._el_timeout = elevenlabs_timeout
        self._fallback = "none" if (fallback or "").lower() == "none" \
            else "local"
        self._queue = queue.Queue()
        self._model = None  # lazy-loaded for local engine
        self._last_engine = None  # what served the latest transcription
        # Result of the startup check: unchecked, ok, auth_failed,
        # unreachable or error
        self.elevenlabs_status = "unchecked"
        self._thread = None

    @property
    def model_size(self):
        return self._model_size

    @property
    def engine(self):
        return self._engine

    @property
    def device(self):
        return self._device

    @property
    def elevenlabs_model(self):
        return self._el_model

    @property
    def keyterms(self):
        return list(self._el_keyterms)

    @property
    def fallback(self):
        return self._fallback

    def start(self):
        """Start the worker thread.

        For the local engine the model is loaded by the worker right away
        instead of on the first job, so the first alert is not delayed by a
        model download.  The elevenlabs engine loads its fallback model only
        when it is first needed, to keep RAM free on small hosts.
        """
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="transcriber")
        self._thread.start()
        log.info("Transcriber worker thread started")

    def enqueue(self, audio_path, event_id, callback):
        """Add a transcription job to the queue.

        callback(event_id, text, error, duration_sec) is called when done.
        """
        self._queue.put((audio_path, event_id, callback))

    def shutdown(self):
        """Signal the worker to stop."""
        self._queue.put(None)

    def _run(self):
        """Worker loop — processes transcription jobs."""
        if self._engine == "local":
            try:
                self._load_local_model()
            except Exception as e:
                # Retried on the first job; keep the worker alive
                log.error("Could not preload Whisper model: %s", e)
        elif self._engine == "elevenlabs":
            self.check_elevenlabs()
        while True:
            item = self._queue.get()
            if item is None:
                break  # Shutdown sentinel
            audio_path, event_id, callback = item
            try:
                t0 = time.monotonic()
                text = self.transcribe(Path(audio_path))
                duration_sec = round(time.monotonic() - t0, 1)
                log.info("Transcription complete for event %d via %s "
                         "(%d chars, %.1fs)", event_id, self._last_engine,
                         len(text) if text else 0, duration_sec)
                callback(event_id, text, None, duration_sec)
            except Exception as e:
                log.error("Transcription failed for event %s: %s",
                          event_id, e)
                callback(event_id, None, e, None)

    def transcribe(self, audio_path):
        """Transcribe one file synchronously with the configured engine."""
        if self._engine == "remote":
            self._last_engine = f"remote ASR ({self._remote_url})"
            return self._transcribe_remote(audio_path)
        if self._engine == "elevenlabs":
            try:
                text = self._transcribe_elevenlabs(audio_path)
                self._last_engine = f"ElevenLabs {self._el_model}"
                return text
            except Exception as e:
                if self._fallback != "local":
                    raise
                log.warning("ElevenLabs transcription failed (%s) — "
                            "falling back to local %s", e, self._model_size)
        if self._model is None:
            self._load_local_model()
        self._last_engine = (f"local {self._model_size}"
                             + (" (fallback)" if self._engine == "elevenlabs"
                                else ""))
        return self._transcribe_local(audio_path)

    def check_elevenlabs(self):
        """Confirm at startup that ElevenLabs answers and accepts the key.

        Reads the account's subscription (no credits used) and writes the
        outcome to the log, so `docker logs` shows whether transcription
        will go through ElevenLabs or the local fallback.  Returns the
        status, also kept in self.elevenlabs_status.
        """
        import requests

        fallback = (f"clips will be transcribed locally ({self._model_size})"
                    if self._fallback == "local"
                    else "transcriptions will fail (TRANSCRIPTION_FALLBACK=none)")
        try:
            resp = requests.get(ELEVENLABS_ACCOUNT_URL,
                                headers={"xi-api-key": self._el_api_key},
                                timeout=min(15, self._el_timeout or 15))
        except Exception as e:
            log.warning("ElevenLabs: could not reach api.elevenlabs.io (%s) — "
                        "%s until it answers", e, fallback)
            self.elevenlabs_status = "unreachable"
            return self.elevenlabs_status

        detail = _error_detail(resp)
        if resp.status_code == 200:
            try:
                sub = resp.json()
            except ValueError:
                sub = {}
            used, limit = sub.get("character_count"), sub.get("character_limit")
            credits = ""
            if isinstance(used, int) and isinstance(limit, int) and limit:
                credits = (", " + f"{used:,} of {limit:,}".replace(",", " ")
                           + " credits used")
                if used >= 0.9 * limit:
                    log.warning("ElevenLabs: %d%% of this period's credits are "
                                "used; when they run out, %s",
                                round(100 * used / limit), fallback)
            log.info("ElevenLabs: connected and authenticated (plan %s%s) — "
                     "transcribing with %s, fallback %s",
                     sub.get("tier", "unknown"), credits, self._el_model,
                     self._fallback)
            self.elevenlabs_status = "ok"
        elif resp.status_code in (401, 403) and "permission" in detail.lower():
            # A restricted key without "User: read" access still works for
            # speech-to-text; the first transcription will confirm it.
            log.info("ElevenLabs: API key accepted, but it may not read account "
                     "details (%s) — speech-to-text is confirmed by the first "
                     "transcription", detail)
            self.elevenlabs_status = "ok"
        elif resp.status_code in (401, 403):
            log.error("ElevenLabs: authentication failed (HTTP %d: %s) — check "
                      "ELEVENLABS_API_KEY; %s", resp.status_code, detail,
                      fallback)
            self.elevenlabs_status = "auth_failed"
        else:
            log.warning("ElevenLabs: unexpected answer (HTTP %d: %s) — %s if "
                        "transcription fails", resp.status_code, detail,
                        fallback)
            self.elevenlabs_status = "error"
        return self.elevenlabs_status

    def _load_local_model(self):
        """Load the faster-whisper model (one-time, may download)."""
        log.info("Loading faster-whisper model '%s' on %s "
                 "(first load may download the model)...",
                 self._model_size, self._device)
        from faster_whisper import WhisperModel
        compute = "int8" if self._device == "cpu" else "float16"
        self._model = WhisperModel(
            self._model_size,
            device=self._device,
            compute_type=compute,
            cpu_threads=self._cpu_threads,
        )
        log.info("Whisper model loaded successfully")

    def _transcribe_local(self, audio_path):
        """Run transcription locally via faster-whisper."""
        segments, info = self._model.transcribe(
            str(audio_path),
            language=self._language,
            beam_size=self._beam_size,
            # Each clip is short and self-contained; feeding the previous
            # window back as context mostly causes repetition loops and
            # hallucinated text (KBLab recommends turning it off).
            condition_on_previous_text=False,
            initial_prompt=self._initial_prompt,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
        )
        texts = [seg.text.strip() for seg in segments if seg.text.strip()]
        return " ".join(texts)

    def _transcribe_elevenlabs(self, audio_path):
        """Send audio to the ElevenLabs Scribe speech-to-text API."""
        import requests

        if not self._el_api_key:
            raise ValueError("ELEVENLABS_API_KEY is not configured")

        # A list of tuples so keyterms can repeat as one form field per term
        data = [
            ("model_id", self._el_model),
            ("tag_audio_events", "false"),
            ("timestamps_granularity", "none"),
            ("diarize", "false"),
            ("temperature", "0"),
        ]
        if self._language:
            data.append(("language_code", self._language))
        if self._el_model != "scribe_v2":
            # Road numbers and phone numbers as digits ("E18", "020-99 94 44");
            # scribe_v2 does not accept this option
            data.append(("numbers_format", "digits"))
        data.extend(("keyterms", term) for term in self._el_keyterms)

        with open(audio_path, "rb") as f:
            resp = requests.post(
                ELEVENLABS_URL,
                headers={"xi-api-key": self._el_api_key},
                data=data,
                files={"file": (Path(audio_path).name, f)},
                timeout=self._el_timeout,
            )
        if resp.status_code >= 400:
            raise RuntimeError(f"ElevenLabs HTTP {resp.status_code}: "
                               f"{resp.text[:300]}")
        return (resp.json().get("text") or "").strip()

    def _transcribe_remote(self, audio_path):
        """Send audio to remote Whisper ASR server via /asr endpoint."""
        import requests

        if not self._remote_url:
            raise ValueError("WHISPER_REMOTE_URL is not configured")

        url = f"{self._remote_url.rstrip('/')}/asr"
        params = {
            "encode": "true",
            "task": "transcribe",
            "language": self._language,
            "output": "json",
            "vad_filter": "true",
        }
        if self._initial_prompt:
            params["initial_prompt"] = self._initial_prompt

        with open(audio_path, "rb") as f:
            files = {"audio_file": (Path(audio_path).name, f, "audio/wav")}
            resp = requests.post(
                url,
                params=params,
                files=files,
                timeout=self._remote_timeout,
            )

        resp.raise_for_status()
        result = resp.json()
        return result.get("text", "").strip()
