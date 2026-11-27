"""Audio recorder: captures a station's FM audio during a broadcast.

Collects raw PCM from the AudioTee and, when the broadcast ends, encodes it
to OGG/Opus in the spool folder and queues it for the aggregator, which
transcribes it. feed() is called from the AudioTee thread, start/stop from
the station's redsea reader thread.
"""

import io
import logging
import subprocess
import threading
import time
from pathlib import Path

import event_store

log = logging.getLogger("rds-guard")

# The longest recording; an Opus clip of this length stays well under the
# aggregator's 8 MiB limit
MAX_RECORDING_SEC = 1200


class AudioRecorder:
    """Records one station's audio during its broadcasts."""

    SAMPLE_RATE = 171_000   # Hz (the channelizer's PCM rate)
    SAMPLE_WIDTH = 2        # bytes (signed 16-bit)
    CHANNELS = 1

    MIN_DURATION_SEC = 10   # shorter recordings are discarded

    def __init__(self, spool_dir, max_duration_sec=600):
        self._dir = Path(spool_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_duration_sec = min(max_duration_sec, MAX_RECORDING_SEC)
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
            if self._recording:
                log.warning("Recording already active for event %s, finalizing "
                            "before starting event %s", self._event_id, event_id)
                self._finalize()
            self._recording = True
            self._buffer = io.BytesIO()
            self._event_id = event_id
            self._started_at = time.time()
            log.info("Recording started for event %d", event_id)

    def feed(self, chunk):
        """Append a PCM chunk (AudioTee thread)."""
        with self._lock:
            if not self._recording or self._buffer is None:
                return
            if time.time() - self._started_at > self._max_duration_sec:
                log.warning("Recording hit max duration (%ds), finalizing",
                            self._max_duration_sec)
                self._finalize()
                return
            self._buffer.write(chunk)

    def stop(self):
        """Stop and save. True if a recording will be saved."""
        with self._lock:
            if not self._recording:
                return False
            return self._finalize()

    def _finalize(self):
        """Must hold self._lock."""
        self._recording = False
        raw_pcm = self._buffer.getvalue()
        self._buffer = None
        event_id = self._event_id
        self._event_id = None
        elapsed = time.time() - self._started_at
        if elapsed < self.MIN_DURATION_SEC or not raw_pcm:
            log.info("Recording too short (%.1fs), discarding", elapsed)
            return False
        log.info("Recording stopped for event %d (%.1fs, %d bytes)",
                 event_id, elapsed, len(raw_pcm))
        threading.Thread(target=self._save, args=(event_id, raw_pcm),
                         daemon=True).start()
        return True

    def _save(self, event_id, raw_pcm):
        try:
            name = f"{event_id}.ogg"
            encode_ogg(raw_pcm, self._dir / name, self.SAMPLE_RATE)
            event_store.update_event_audio(event_id, name)
            log.info("Audio saved for event %d: %s", event_id, name)
        except Exception:
            log.exception("Failed to save audio for event %d", event_id)


def encode_ogg(raw_pcm, output_path, sample_rate):
    """Raw FM multiplex PCM -> speech-band OGG/Opus via ffmpeg."""
    tmp = Path(str(output_path) + ".tmp")
    cmd = [
        "ffmpeg", "-y", "-f", "s16le", "-ar", str(sample_rate), "-ac", "1",
        "-i", "pipe:0",
        # Undo the 50 µs pre-emphasis used in Europe and keep the speech
        # band, dropping hum and the 19 kHz pilot before downsampling
        "-af", "aemphasis=type=50fm:mode=reproduction,highpass=f=80,lowpass=f=7000",
        "-ar", "16000", "-c:a", "libopus", "-b:a", "48k", "-f", "ogg", str(tmp),
    ]
    proc = subprocess.run(cmd, input=raw_pcm, capture_output=True, timeout=120)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed (rc={proc.returncode}): "
                           f"{proc.stderr.decode('utf-8', 'replace')[:500]}")
    tmp.replace(output_path)
