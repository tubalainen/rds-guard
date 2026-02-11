"""Audio Recorder — captures FM audio during RDS events.

Records raw PCM from the AudioTee, then saves to WAV + OGG via ffmpeg
and enqueues transcription.  Thread-safe: feed() is called from the
AudioTee thread, start/stop from the pipeline callback thread.
"""

import io
import logging
import os
import subprocess
import threading
import time
from pathlib import Path

import event_store

log = logging.getLogger("rds-guard")


class AudioRecorder:
    """Records FM audio during active RDS events."""

    # Raw PCM parameters (must match rtl_fm output)
    SAMPLE_RATE = 171_000   # Hz (from rtl_fm -s 171k)
    SAMPLE_WIDTH = 2        # bytes (signed 16-bit)
    CHANNELS = 1            # mono

    # Recording limits
    MIN_DURATION_SEC = 10   # discard recordings shorter than 10s

    def __init__(self, audio_dir, transcriber, on_transcription_complete,
                 max_duration_sec=600):
        self._audio_dir = Path(audio_dir)
        self._audio_dir.mkdir(parents=True, exist_ok=True)
        self._transcriber = transcriber
        self._on_transcription_complete = on_transcription_complete
        self._max_duration_sec = max_duration_sec
        self._lock = threading.Lock()
        self._recording = False
        self._buffer = None
        self._event_id = None
        self._started_at = None

    @property
    def is_recording(self):
        return self._recording

    def start(self, event_id):
        """Begin recording for the given event."""
        with self._lock:
            # If already recording (shouldn't happen), finalize the old one
            if self._recording:
                log.warning("Recording already active for event %s, "
                            "finalizing before starting event %s",
                            self._event_id, event_id)
                self._finalize()
            self._recording = True
            self._buffer = io.BytesIO()
            self._event_id = event_id
            self._started_at = time.time()
            log.info("Recording started for event %d", event_id)

    def feed(self, chunk):
        """Append a PCM chunk (called from AudioTee thread)."""
        with self._lock:
            if not self._recording or self._buffer is None:
                return
            # Safety: enforce max duration
            elapsed = time.time() - self._started_at
            if elapsed > self._max_duration_sec:
                log.warning("Recording hit max duration (%ds), finalizing",
                            self._max_duration_sec)
                self._finalize()
                return
            self._buffer.write(chunk)

    def stop(self):
        """Stop recording and finalize.

        Returns True if a valid recording was captured (will be saved
        and transcribed), False if recording was discarded (too short
        or not active).
        """
        with self._lock:
            if not self._recording:
                return False
            return self._finalize()

    def _finalize(self):
        """Write WAV, enqueue transcription.  Must hold self._lock.

        Returns True if recording is valid and will be saved, False
        if discarded.
        """
        self._recording = False
        raw_pcm = self._buffer.getvalue()
        self._buffer = None
        event_id = self._event_id
        self._event_id = None
        elapsed = time.time() - self._started_at

        if elapsed < self.MIN_DURATION_SEC or len(raw_pcm) == 0:
            log.info("Recording too short (%.1fs), discarding", elapsed)
            return False

        log.info("Recording stopped for event %d (%.1fs, %d bytes)",
                 event_id, elapsed, len(raw_pcm))

        # Save in background thread to avoid blocking the tee
        threading.Thread(
            target=self._save_and_transcribe,
            args=(event_id, raw_pcm, elapsed),
            daemon=True,
        ).start()
        return True

    def _save_and_transcribe(self, event_id, raw_pcm, duration):
        """Downsample, save WAV + OGG, run transcription."""
        try:
            wav_path = self._audio_dir / f"{event_id}.wav"
            ogg_path = self._audio_dir / f"{event_id}.ogg"

            # 1. Convert raw PCM to 16 kHz WAV via ffmpeg
            self._ffmpeg_convert(raw_pcm, wav_path, output_format="wav")

            # 2. Convert raw PCM to OGG/Opus via ffmpeg
            self._ffmpeg_convert(raw_pcm, ogg_path, output_format="ogg")

            # 3. Update event with audio path
            audio_rel = f"{event_id}.ogg"
            event_store.update_event_audio(event_id, audio_rel)

            log.info("Audio saved for event %d: %s + %s",
                     event_id, wav_path.name, ogg_path.name)

            # 4. Enqueue transcription (if transcriber is available)
            if self._transcriber is not None:
                event_store.update_event_transcription_status(
                    event_id, "transcribing")
                self._transcriber.enqueue(
                    wav_path, event_id, self._on_transcription_complete)
            else:
                # Transcription disabled
                event_store.update_event_transcription_status(
                    event_id, None)

        except Exception:
            log.exception("Failed to save audio for event %d", event_id)
            event_store.update_event_transcription_status(event_id, "error")

    def _ffmpeg_convert(self, raw_pcm, output_path, output_format):
        """Convert raw PCM to a file via ffmpeg subprocess."""
        cmd = [
            "ffmpeg", "-y",
            "-f", "s16le",
            "-ar", str(self.SAMPLE_RATE),
            "-ac", str(self.CHANNELS),
            "-i", "pipe:0",
            "-ar", "16000",
        ]
        if output_format == "ogg":
            cmd.extend(["-c:a", "libopus", "-b:a", "48k"])
        cmd.append(str(output_path))

        proc = subprocess.run(
            cmd,
            input=raw_pcm,
            capture_output=True,
            timeout=60,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"ffmpeg failed (rc={proc.returncode}): {stderr}")
