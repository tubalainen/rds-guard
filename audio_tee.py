"""Audio Tee — PCM stream splitter for the rtl_fm → redsea pipeline.

Replaces the direct rtl_fm.stdout → redsea.stdin pipe.  Reads raw PCM
chunks from rtl_fm, forwards every chunk to redsea, and — when recording
is active — simultaneously feeds the audio recorder.
"""

import logging

log = logging.getLogger("rds-guard")


class AudioTee:
    """Tees the rtl_fm PCM stream to both redsea and the audio recorder.

    Thread-safe: run() is called from a dedicated thread.  The recorder's
    is_recording flag and feed() method are protected by their own lock.
    """

    def __init__(self, rtl_stdout, redsea_stdin, recorder):
        self._src = rtl_stdout        # rtl_fm stdout (binary stream)
        self._dst = redsea_stdin       # redsea stdin (binary stream)
        self._recorder = recorder      # AudioRecorder instance
        self._chunk_size = 8192        # ~24 ms at 171 kHz 16-bit mono

    def run(self):
        """Main loop — reads rtl_fm, writes redsea + recorder.

        Blocks until rtl_fm's stdout closes (EOF) or a write error
        occurs (redsea died).  Designed to run in its own thread.
        """
        try:
            while True:
                chunk = self._src.read(self._chunk_size)
                if not chunk:
                    break  # EOF — rtl_fm exited
                try:
                    self._dst.write(chunk)
                    self._dst.flush()
                except (BrokenPipeError, OSError):
                    log.warning("AudioTee: redsea stdin broken, stopping")
                    break
                if self._recorder.is_recording:
                    self._recorder.feed(chunk)
        except Exception:
            log.exception("AudioTee: unexpected error in read loop")
        finally:
            try:
                self._dst.close()
            except Exception:
                pass
            log.info("AudioTee: stream ended")
