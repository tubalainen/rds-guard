"""Radio pipeline: one per receiver (RTL-SDR dongle or Airspy).

    rtl_sdr (IQ, 2.394 MS/s)     -> Channelizer thread
    or airspy_rx (IQ, 3/2.5 MS/s)
                                  |- pipe 0 -> AudioTee -> redsea -> callback[0]
                                  |- pipe 1 -> AudioTee -> redsea -> callback[1]
                                  '- ...

The same chain for one station as for four: a single station is captured
off-centre (away from the dongle's DC spike), so the gain calibration
measures exactly what runs. The dongle is found by its serial number each
time the pipeline starts. A pipeline that fails starts again after a pause;
it can also be paused, while its dongle is calibrated.

No FIFOs, no shell pipes: subprocess.Popen with stdout=PIPE. Each process's
stderr goes to the log.
"""

import logging
import os
import subprocess
import threading
import time

import config
import dongle_check
import dongles
import sdr

log = logging.getLogger("rds-guard")

RESTART_SEC = 10


class PipelineStatus:
    """Thread-safe health of one receiver's pipeline."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = "not_started"  # not_started | starting | running | paused | stopped | error
        self.error_message = None
        self.started_at = None

    def _set(self, state, error=None):
        with self.lock:
            self.state = state
            self.error_message = error
            if state == "running":
                self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    def set_starting(self):
        self._set("starting")

    def set_running(self):
        self._set("running")

    def set_paused(self, why=None):
        self._set("paused", why)

    def set_stopped(self, message=None):
        self._set("stopped", message)

    def set_error(self, message):
        self._set("error", message)

    def set_note(self, message):
        """A warning shown with the state, without changing it."""
        with self.lock:
            self.error_message = message

    def snapshot(self):
        with self.lock:
            return {"state": self.state, "error": self.error_message,
                    "started_at": self.started_at}


def rtl_sdr_cmd(center_hz, device_index, gain, ppm):
    return sdr.RtlSdr(device_index).stream_cmd(center_hz, gain, ppm)


def redsea_cmd():
    cmd = ["redsea", "-r", "171k", "-t", "%Y-%m-%dT%H:%M:%S%f"]
    if config.REDSEA_SHOW_PARTIAL:
        cmd.append("-p")
    return cmd + ["-E"]


# airspy_rx says this every second
_QUIET = ("Streaming at",)


def _stderr_reader(stream, prefix):
    try:
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", errors="replace").rstrip()
            if text and text.startswith(_QUIET):
                log.debug("[%s] %s", prefix, text)
            elif text:
                log.info("[%s] %s", prefix, text)
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _read_redsea_output(stdout, on_line_callback, stop_event):
    try:
        for raw_line in iter(stdout.readline, b""):
            if stop_event.is_set():
                break
            on_line_callback(raw_line)
    except Exception:
        log.exception("Error reading redsea output")


class _NoopRecorder:
    is_recording = False

    def feed(self, chunk):
        pass


def _terminate(proc, name):
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            log.warning("%s did not exit after SIGTERM, killing it", name)
            proc.kill()
            proc.wait(timeout=2)
    except Exception as e:
        log.warning("Error stopping %s: %s", name, e)


class ReceiverPipeline:
    """Runs one receiver's chain, and keeps it running.

    stations: [{"frequency": "103.5M", "callback": fn(bytes line),
                "recorder": AudioRecorder or None}] in the receiver's order.
    settings: callable(radio) -> (gain, ppm) used at each start (radio: the
    sdr.RtlSdr or sdr.Airspy found for the serial number).
    """

    def __init__(self, receiver, stations, settings):
        self.receiver = receiver
        self.stations = stations
        self._settings = settings
        self.status = PipelineStatus()
        self._run_stop = threading.Event()     # ends the current run
        self._paused = threading.Event()
        self._resumed = threading.Event()
        self._idle = threading.Event()         # nothing holds the dongle
        self._idle.set()
        self.gain = None
        self.ppm = None
        self.model = None
        self.check = None                      # (level, message) of the dongle check

    def _on_check(self, radio, iq, nbytes, elapsed):
        """The dongle check's verdict on this run's first seconds."""
        level, msg, _heard = dongle_check.judge(
            iq, nbytes, elapsed, radio, self.receiver.freqs_hz, self.receiver.center_hz,
            gain=self.gain)
        self.check = (level, msg)
        rid = self.receiver.id
        if level == "error":
            log.error("[%s] dongle check: %s", rid, msg)
        elif level == "warning":
            log.warning("[%s] dongle check: %s", rid, msg)
            self.status.set_note(msg[:256])
        else:
            log.info("[%s] dongle check: %s", rid, msg)

    # -- control ------------------------------------------------------------

    def pause(self, why="calibrating", timeout=15):
        """Stop the chain and keep it stopped until resume(): the dongle is
        free (e.g. for the gain calibration). True once it is free."""
        self._resumed.clear()
        self._paused.set()
        self._run_stop.set()
        self.status.set_paused(why)
        return self._idle.wait(timeout)

    def resume(self):
        self._paused.clear()
        self._resumed.set()

    # -- running ------------------------------------------------------------

    def run(self, stop_event):
        """Supervisor loop (own thread) until stop_event."""
        threading.Thread(target=lambda: (stop_event.wait(), self._run_stop.set(),
                                         self._resumed.set()),
                         daemon=True).start()
        while not stop_event.is_set():
            if self._paused.is_set():
                self._resumed.wait()
                continue
            self._run_stop.clear()
            ok = self._run_once(stop_event)
            if stop_event.is_set():
                break
            if self._paused.is_set():
                continue
            if not ok:
                stop_event.wait(RESTART_SEC)
        self.status.set_stopped("Shutdown requested")

    def _run_once(self, stop_event):
        from audio_tee import AudioTee
        from channelizer import Channelizer

        rid = self.receiver.id
        rtl = None
        redseas = []
        threads = []
        self._idle.clear()
        self.check = None
        try:
            self.status.set_starting()
            radio = sdr.open(self.receiver.serial, getattr(self.receiver, "stages", None))
            self.model = radio.model
            self.gain, self.ppm = self._settings(radio)
            if not radio.has_ppm:
                self.ppm = 0
            cmd = radio.stream_cmd(self.receiver.center_hz, self.gain, self.ppm)
            radio.prepare()
            log.info("[%s] starting (%s, gain %s): %s on %s", rid, radio.model,
                     radio.describe_gain(self.gain), " ".join(cmd),
                     ", ".join(self.receiver.frequencies))
            rtl = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            threading.Thread(target=_stderr_reader, args=(rtl.stderr, f"{radio.tool} {rid}"),
                             daemon=True).start()
            # The verdict is worked out on its own thread, so that the
            # channelizer never waits for it
            probe = dongle_check.Probe(radio.fs, lambda iq, n, el: threading.Thread(
                target=self._on_check, args=(radio, iq, n, el), daemon=True).start())
            chan = Channelizer(rtl.stdout, self.receiver.freqs_hz, self.receiver.center_hz,
                               radio.fs, radio.fmt, probe=probe)
            chan.start()
            for fd, st in zip(chan.pipe_read_fds, self.stations):
                rs = subprocess.Popen(redsea_cmd(), stdin=subprocess.PIPE,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                redseas.append(rs)
                threading.Thread(target=_stderr_reader,
                                 args=(rs.stderr, f"redsea {st['frequency']}"),
                                 daemon=True).start()
                tee = AudioTee(os.fdopen(fd, "rb"), rs.stdin,
                               st.get("recorder") or _NoopRecorder())
                for target, args in ((tee.run, ()),
                                     (_read_redsea_output,
                                      (rs.stdout, st["callback"], self._run_stop))):
                    t = threading.Thread(target=target, args=args, daemon=True)
                    t.start()
                    threads.append(t)
            self.status.set_running()
            # Until the run is ended (pause, shutdown), the capture exits or
            # the dongle check finds it does not really receive
            while (rtl.poll() is None and not self._run_stop.wait(1)
                   and not (self.check and self.check[0] == "error")):
                pass
            if self._run_stop.is_set():
                return True
            if self.check and self.check[0] == "error":
                self.status.set_error(self.check[1])
                return False
            msg = f"{radio.tool} exited with code {rtl.returncode}"
            log.error("[%s] %s", rid, msg)
            self.status.set_error(msg)
            return False
        except dongles.DongleError as e:
            log.error("[%s] %s", rid, e)
            self.status.set_error(str(e))
            return False
        except FileNotFoundError as e:
            msg = f"Binary not found: {e.filename}"
            log.error("[%s] %s", rid, msg)
            self.status.set_error(msg)
            return False
        except Exception as e:
            log.exception("[%s] pipeline error", rid)
            self.status.set_error(str(e))
            return False
        finally:
            _terminate(rtl, "the capture")
            for rs in redseas:
                _terminate(rs, "redsea")
            for t in threads:
                t.join(timeout=3)
            self._idle.set()
