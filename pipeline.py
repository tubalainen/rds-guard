"""Radio pipeline manager — spawns and monitors rtl_fm + redsea subprocesses.

Single-station (FM_FREQUENCY):
    rtl_fm (stdout=PCM) → AudioTee → redsea (stdout=ndjson) → Python callback

Multi-station (FM_FREQUENCIES with 2-4 entries):
    rtl_sdr (stdout=IQ) → Channelizer thread
                              ├─ pipe 0 → AudioTee → redsea → callback[0]
                              ├─ pipe 1 → AudioTee → redsea → callback[1]
                              └─ ...

No FIFOs, no shell pipes — just subprocess.Popen with stdout=PIPE.
Each process's stderr is captured by a dedicated reader thread and
logged to Python logging, making all output visible in docker logs.
"""

import logging
import os
import re
import subprocess
import threading
import time

import config

log = logging.getLogger("rds-guard")


# ---------------------------------------------------------------------------
# Pipeline status — thread-safe state exposed to web UI via /api/status
# ---------------------------------------------------------------------------

class PipelineStatus:
    """Thread-safe pipeline health status."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = "not_started"  # not_started | starting | running | stopped | error
        self.error_message = None
        self.rtl_fm_pid = None
        self.redsea_pid = None
        self.started_at = None

    def set_starting(self):
        with self.lock:
            self.state = "starting"
            self.error_message = None

    def set_running(self, rtl_pid, redsea_pid):
        with self.lock:
            self.state = "running"
            self.error_message = None
            self.rtl_fm_pid = rtl_pid
            self.redsea_pid = redsea_pid
            self.started_at = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    def set_stopped(self, message=None):
        with self.lock:
            self.state = "stopped"
            self.error_message = message
            self.rtl_fm_pid = None
            self.redsea_pid = None

    def set_error(self, message):
        with self.lock:
            self.state = "error"
            self.error_message = message
            self.rtl_fm_pid = None
            self.redsea_pid = None

    def snapshot(self):
        with self.lock:
            return {
                "state": self.state,
                "error": self.error_message,
                "rtl_fm_pid": self.rtl_fm_pid,
                "redsea_pid": self.redsea_pid,
                "started_at": self.started_at,
            }


# Module-level singleton — importable by web_server and rds_guard
pipeline_status = PipelineStatus()


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------

def _build_rtl_fm_cmd():
    """Build the rtl_fm command array from config (single-station path)."""
    device_index = _resolve_device_serial()
    cmd = [
        "rtl_fm",
        "-M", "fm",
        "-l", "0",
        "-A", "std",
        "-p", str(config.PPM_CORRECTION),
        "-s", "171k",
        "-g", str(config.RTL_GAIN),
        "-F", "9",
        "-d", str(device_index),
        "-f", str(config.FM_FREQUENCY),
    ]
    return cmd


def _build_rtl_sdr_cmd(center_freq_hz: int, device_index) -> list:
    """Build the rtl_sdr command array for wideband IQ capture (multi-station)."""
    return [
        "rtl_sdr",
        "-f", str(center_freq_hz),
        "-s", str(config.RTL_SAMPLE_RATE),
        "-g", str(config.RTL_GAIN),
        "-p", str(config.PPM_CORRECTION),
        "-d", str(device_index),
        "-",   # write IQ to stdout
    ]


def _build_redsea_cmd():
    """Build the redsea command array from config."""
    cmd = [
        "redsea",
        "-r", "171k",
        "-t", "%Y-%m-%dT%H:%M:%S%f",
    ]
    if config.REDSEA_SHOW_PARTIAL:
        cmd.append("-p")
    if config.REDSEA_SHOW_RAW:
        cmd.append("-R")
    cmd.append("-E")
    return cmd


def _resolve_device_serial():
    """Resolve RTL_DEVICE_SERIAL to a device index via rtl_test.

    Returns the device index (string).  Falls back to RTL_DEVICE_INDEX
    if no serial is configured or lookup fails.
    """
    serial = config.RTL_DEVICE_SERIAL
    if not serial:
        return config.RTL_DEVICE_INDEX

    log.info("Resolving RTL-SDR serial '%s' to device index...", serial)
    try:
        result = subprocess.run(
            ["rtl_test"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # rtl_test outputs to stderr:
        #   0:  Realtek, RTL2838UHIDIR, SN: 00000001
        output = result.stdout + result.stderr
        pattern = rf"^\s*(\d+):.*SN:\s*{re.escape(serial)}"
        match = re.search(pattern, output, re.MULTILINE | re.IGNORECASE)
        if match:
            index = match.group(1)
            log.info("Resolved serial '%s' → device index %s", serial, index)
            return index
        else:
            log.error("No RTL-SDR device found with serial '%s'", serial)
            log.error("rtl_test output:\n%s", output.strip())
            return config.RTL_DEVICE_INDEX  # fall back
    except FileNotFoundError:
        log.error("rtl_test not found — cannot resolve device serial")
        return config.RTL_DEVICE_INDEX
    except subprocess.TimeoutExpired:
        log.error("rtl_test timed out — cannot resolve device serial")
        return config.RTL_DEVICE_INDEX
    except Exception as e:
        log.error("Error resolving device serial: %s", e)
        return config.RTL_DEVICE_INDEX


# ---------------------------------------------------------------------------
# Stderr reader thread — captures subprocess output for docker logs
# ---------------------------------------------------------------------------

def _stderr_reader(stream, prefix):
    """Read a subprocess stderr stream line by line and log each line.

    Runs in a daemon thread.  Exits when the stream closes (process dies).
    """
    try:
        for raw_line in iter(stream.readline, b""):
            text = raw_line.decode("utf-8", errors="replace").rstrip()
            if text:
                log.info("[%s] %s", prefix, text)
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Pipeline runner — main function, designed to run in a thread
# ---------------------------------------------------------------------------

def run_pipeline(on_line_callback, status, stop_event, recorder=None):
    """Spawn rtl_fm | redsea and feed redsea's JSON output to the callback.

    Args:
        on_line_callback: Called with each bytes line from redsea's stdout.
        status: PipelineStatus instance to update.
        stop_event: threading.Event — set to request shutdown.
        recorder: AudioRecorder instance (optional).  If provided, an
                  AudioTee sits between rtl_fm and redsea to capture audio.

    This function blocks until the pipeline exits or stop_event is set.
    It does NOT auto-restart.  Docker's restart policy handles that.
    """
    rtl_proc = None
    redsea_proc = None

    try:
        status.set_starting()

        rtl_cmd = _build_rtl_fm_cmd()
        redsea_cmd = _build_redsea_cmd()

        log.info("Pipeline starting...")
        log.info("  rtl_fm:  %s", " ".join(rtl_cmd))
        log.info("  redsea:  %s", " ".join(redsea_cmd))

        # Spawn rtl_fm: stdout → pipe (read by Python or AudioTee)
        rtl_proc = subprocess.Popen(
            rtl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        log.info("rtl_fm started (PID: %d)", rtl_proc.pid)

        # Spawn redsea: stdin ← PIPE (fed by AudioTee or direct),
        # stdout → pipe to Python, stderr → captured
        redsea_proc = subprocess.Popen(
            redsea_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        log.info("redsea started (PID: %d)", redsea_proc.pid)

        # Start stderr reader threads for both processes
        rtl_stderr_thread = threading.Thread(
            target=_stderr_reader,
            args=(rtl_proc.stderr, "rtl_fm"),
            daemon=True,
        )
        rtl_stderr_thread.start()

        redsea_stderr_thread = threading.Thread(
            target=_stderr_reader,
            args=(redsea_proc.stderr, "redsea"),
            daemon=True,
        )
        redsea_stderr_thread.start()

        status.set_running(rtl_proc.pid, redsea_proc.pid)
        log.info("Pipeline running — reading RDS data...")

        # Start a watchdog thread that kills subprocesses when stop_event is set.
        def _shutdown_watchdog():
            stop_event.wait()
            log.info("Shutdown requested — killing pipeline subprocesses...")
            _terminate_process(rtl_proc, "rtl_fm")
            _terminate_process(redsea_proc, "redsea")

        watchdog = threading.Thread(target=_shutdown_watchdog, daemon=True)
        watchdog.start()

        # AudioTee: Python sits between rtl_fm and redsea to capture audio
        from audio_tee import AudioTee

        if recorder is None:
            # Create a no-op recorder stub so AudioTee still works
            from audio_recorder import AudioRecorder
            recorder = _NoopRecorder()

        tee = AudioTee(rtl_proc.stdout, redsea_proc.stdin, recorder)

        # Run the redsea JSON reader in a separate thread
        redsea_reader = threading.Thread(
            target=_read_redsea_output,
            args=(redsea_proc.stdout, on_line_callback, stop_event),
            daemon=True,
        )
        redsea_reader.start()

        # Run AudioTee in this thread (blocks until rtl_fm EOF or error)
        tee.run()

        # Wait for redsea reader to finish
        redsea_reader.join(timeout=5)

    except FileNotFoundError as e:
        msg = f"Binary not found: {e.filename}"
        log.error("Pipeline error: %s", msg)
        status.set_error(msg)
        return

    except Exception as e:
        msg = str(e)
        log.error("Pipeline error: %s", msg)
        status.set_error(msg)
        return

    finally:
        # Clean up child processes
        _terminate_process(rtl_proc, "rtl_fm")
        _terminate_process(redsea_proc, "redsea")

    # Determine exit reason
    if stop_event.is_set():
        log.info("Pipeline stopped (shutdown requested)")
        status.set_stopped("Shutdown requested")
    else:
        # Pipeline died on its own — figure out why
        rtl_code = rtl_proc.returncode if rtl_proc else None
        redsea_code = redsea_proc.returncode if redsea_proc else None

        if rtl_code is not None and rtl_code != 0:
            msg = f"rtl_fm exited with code {rtl_code}"
            log.error("Pipeline failed: %s", msg)
            status.set_error(msg)
        elif redsea_code is not None and redsea_code != 0:
            msg = f"redsea exited with code {redsea_code}"
            log.error("Pipeline failed: %s", msg)
            status.set_error(msg)
        else:
            log.warning("Pipeline ended unexpectedly")
            status.set_stopped("Pipeline ended")


def run_pipeline_multi(station_configs, on_line_callbacks, status, stop_event):
    """Wideband IQ path: rtl_sdr → Channelizer → N × (AudioTee + redsea).

    Args:
        station_configs: list of dicts with keys:
            frequency (str): frequency string, e.g. "103.5M"
            freq_hz (int):   frequency in Hz
            recorder:        AudioRecorder instance for this station
        on_line_callbacks: list of callables, one per station.  Each is called
            with a bytes line from its redsea process's stdout.
        status: PipelineStatus instance to update.
        stop_event: threading.Event — set to request shutdown.

    Blocks until all subprocesses exit or stop_event is set.
    """
    from channelizer import Channelizer
    from audio_tee import AudioTee

    rtl_proc = None
    redsea_procs = []
    tee_threads = []
    reader_threads = []

    try:
        status.set_starting()

        device_index = _resolve_device_serial()
        center_freq_hz = config.RTL_CENTER_FREQ_HZ
        rtl_cmd = _build_rtl_sdr_cmd(center_freq_hz, device_index)
        redsea_cmd = _build_redsea_cmd()

        log.info("Pipeline starting (multi-station: %d stations, centre %s Hz)",
                 len(station_configs), center_freq_hz)
        log.info("  rtl_sdr:  %s", " ".join(rtl_cmd))
        log.info("  redsea:   %s", " ".join(redsea_cmd))
        log.info("  stations: %s",
                 ", ".join(sc["frequency"] for sc in station_configs))

        # Spawn rtl_sdr
        rtl_proc = subprocess.Popen(
            rtl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        log.info("rtl_sdr started (PID: %d)", rtl_proc.pid)

        # Stderr reader for rtl_sdr
        threading.Thread(
            target=_stderr_reader,
            args=(rtl_proc.stderr, "rtl_sdr"),
            daemon=True,
        ).start()

        # Start Channelizer: parses IQ and writes PCM to per-station pipes
        freq_hz_list = [sc["freq_hz"] for sc in station_configs]
        channelizer = Channelizer(rtl_proc.stdout, freq_hz_list, center_freq_hz)
        channelizer.start()
        pipe_fds = channelizer.pipe_read_fds

        # Spawn one redsea per station, wired to the channelizer pipe
        for i, sc in enumerate(station_configs):
            redsea_proc = subprocess.Popen(
                redsea_cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            redsea_procs.append(redsea_proc)
            log.info("redsea[%d] started (PID: %d) for %s",
                     i, redsea_proc.pid, sc["frequency"])

            # Stderr reader for this redsea
            threading.Thread(
                target=_stderr_reader,
                args=(redsea_proc.stderr, f"redsea[{sc['frequency']}]"),
                daemon=True,
            ).start()

            # AudioTee: pipe_r → redsea.stdin + recorder
            pipe_r_file = os.fdopen(pipe_fds[i], "rb")
            recorder = sc.get("recorder") or _NoopRecorder()
            tee = AudioTee(pipe_r_file, redsea_proc.stdin, recorder)

            tee_t = threading.Thread(target=tee.run, daemon=True)
            tee_t.start()
            tee_threads.append(tee_t)

            # Reader thread: redsea stdout → station callback
            callback = on_line_callbacks[i]
            reader_t = threading.Thread(
                target=_read_redsea_output,
                args=(redsea_proc.stdout, callback, stop_event),
                daemon=True,
            )
            reader_t.start()
            reader_threads.append(reader_t)

        # Collect PIDs for the status object (use first redsea as representative)
        redsea_pid0 = redsea_procs[0].pid if redsea_procs else None
        status.set_running(rtl_proc.pid, redsea_pid0)
        log.info("Multi-station pipeline running — %d decoders active",
                 len(redsea_procs))

        # Shutdown watchdog
        def _shutdown_watchdog():
            stop_event.wait()
            log.info("Shutdown requested — killing multi-station pipeline...")
            _terminate_process(rtl_proc, "rtl_sdr")
            for j, rp in enumerate(redsea_procs):
                _terminate_process(rp, f"redsea[{j}]")

        threading.Thread(target=_shutdown_watchdog, daemon=True).start()

        # Block until rtl_sdr exits (Channelizer will then reach EOF and stop)
        rtl_proc.wait()

        # Wait for tee threads and reader threads to finish
        for t in tee_threads + reader_threads:
            t.join(timeout=5)

    except FileNotFoundError as e:
        msg = f"Binary not found: {e.filename}"
        log.error("Pipeline error: %s", msg)
        status.set_error(msg)
        return

    except Exception as e:
        msg = str(e)
        log.error("Pipeline error: %s", msg)
        status.set_error(msg)
        return

    finally:
        _terminate_process(rtl_proc, "rtl_sdr")
        for j, rp in enumerate(redsea_procs):
            _terminate_process(rp, f"redsea[{j}]")

    if stop_event.is_set():
        log.info("Multi-station pipeline stopped (shutdown requested)")
        status.set_stopped("Shutdown requested")
    else:
        rtl_code = rtl_proc.returncode if rtl_proc else None
        if rtl_code is not None and rtl_code != 0:
            msg = f"rtl_sdr exited with code {rtl_code}"
            log.error("Pipeline failed: %s", msg)
            status.set_error(msg)
        else:
            log.warning("Multi-station pipeline ended unexpectedly")
            status.set_stopped("Pipeline ended")


def _read_redsea_output(stdout, on_line_callback, stop_event):
    """Read redsea's stdout line by line and call the callback.

    Runs in a dedicated thread.  Exits on EOF or when stop_event is set.
    """
    try:
        for raw_line in iter(stdout.readline, b""):
            if stop_event.is_set():
                break
            on_line_callback(raw_line)
    except Exception:
        log.exception("Error reading redsea output")


class _NoopRecorder:
    """Stub recorder when no real recorder is configured."""
    is_recording = False

    def feed(self, chunk):
        pass


def _terminate_process(proc, name):
    """Terminate a subprocess gracefully, then force-kill if needed."""
    if proc is None:
        return
    if proc.poll() is not None:
        # Already exited
        return
    try:
        log.info("Terminating %s (PID: %d)...", name, proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            log.warning("%s did not exit after SIGTERM, sending SIGKILL", name)
            proc.kill()
            proc.wait(timeout=2)
    except Exception as e:
        log.warning("Error terminating %s: %s", name, e)


