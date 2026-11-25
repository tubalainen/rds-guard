"""Transcription engine — speech-to-text with local or remote backend.

Supports two modes selected by TRANSCRIPTION_ENGINE config:
  - "local"  → built-in faster-whisper (CPU, default)
  - "remote" → external Whisper ASR server via /asr HTTP endpoint
  - "none"   → disabled (returns None transcriber)
"""

import logging
import queue
import threading
import time

log = logging.getLogger("rds-guard")

# KBLab's KB-Whisper (National Library of Sweden) is fine-tuned on 50 000+
# hours of Swedish speech.  At the same size and CPU cost as OpenAI's "small"
# it cuts the Swedish word error rate from ~20-26 % to ~6-7 %, so it is the
# default whenever the transcription language is Swedish.
SWEDISH_DEFAULT_MODEL = "KBLab/kb-whisper-small"
GENERIC_DEFAULT_MODEL = "small"


def resolve_model(model_size, language):
    """Pick the local model: explicit setting wins, else a language default."""
    if model_size:
        return model_size
    if (language or "").lower() == "sv":
        return SWEDISH_DEFAULT_MODEL
    return GENERIC_DEFAULT_MODEL


def create_transcriber(engine, language, model_size=None, device=None,
                       remote_url=None, remote_timeout=120,
                       initial_prompt=None, beam_size=5, cpu_threads=0):
    """Factory: create a Transcriber based on engine type.

    Returns None if engine is "none" (transcription disabled).
    """
    if engine == "none":
        log.info("Transcription disabled (TRANSCRIPTION_ENGINE=none)")
        return None

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
    )

    if engine == "remote":
        if not remote_url:
            log.error("TRANSCRIPTION_ENGINE=remote but WHISPER_REMOTE_URL "
                      "is empty — transcription will fail")
        else:
            log.info("Transcription engine: remote (%s)", remote_url)
    else:
        log.info("Transcription engine: local (model=%s, device=%s)",
                 t.model_size, device)

    return t


class Transcriber:
    """Speech-to-text engine with async job queue."""

    def __init__(self, engine, language, model_size=None, device=None,
                 remote_url=None, remote_timeout=120,
                 initial_prompt=None, beam_size=5, cpu_threads=0):
        self._engine = engine
        self._language = language
        self._model_size = resolve_model(model_size, language)
        self._device = device or "cpu"
        self._initial_prompt = initial_prompt or None
        self._beam_size = max(1, int(beam_size or 1))
        self._cpu_threads = max(0, int(cpu_threads or 0))
        self._remote_url = remote_url
        self._remote_timeout = remote_timeout
        self._queue = queue.Queue()
        self._model = None  # lazy-loaded for local engine
        self._thread = None

    @property
    def model_size(self):
        return self._model_size

    def start(self):
        """Start the worker thread.

        The local model is loaded by the worker right away instead of on the
        first job, so the first alert is not delayed by a model download.
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
        if self._engine != "remote":
            try:
                self._load_local_model()
            except Exception as e:
                # Retried on the first job; keep the worker alive
                log.error("Could not preload Whisper model: %s", e)
        while True:
            item = self._queue.get()
            if item is None:
                break  # Shutdown sentinel
            audio_path, event_id, callback = item
            try:
                t0 = time.monotonic()
                if self._engine == "remote":
                    text = self._transcribe_remote(audio_path)
                else:
                    if self._model is None:
                        self._load_local_model()
                    text = self._transcribe_local(audio_path)
                duration_sec = round(time.monotonic() - t0, 1)
                log.info("Transcription complete for event %d (%d chars, %.1fs)",
                         event_id, len(text) if text else 0, duration_sec)
                callback(event_id, text, None, duration_sec)
            except Exception as e:
                log.error("Transcription failed for event %s: %s",
                          event_id, e)
                callback(event_id, None, e, None)

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
            files = {"audio_file": (audio_path.name, f, "audio/wav")}
            resp = requests.post(
                url,
                params=params,
                files=files,
                timeout=self._remote_timeout,
            )

        resp.raise_for_status()
        result = resp.json()
        return result.get("text", "").strip()
