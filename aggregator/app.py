"""RDS Guard aggregator — entry point.

Runs two HTTP servers in one event loop:
  - ingest API (INGEST_PORT, default 8081): satellites report here; this is
    the only part that is put on the internet, behind nginx + Cloudflare
  - web UI + read API + node admin (WEB_PORT, default 8080): LAN only
"""

import asyncio
import logging
import signal
import threading
from pathlib import Path

from aiohttp import web

from . import config, notifier, store
from .api import create_web_app
from .ingest import create_ingest_app
from .mqtt_out import MqttOut
from .pipeline import Pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("rds-aggregator")


async def _serve(app, port, label):
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("%s listening on 0.0.0.0:%d", label, port)
    return runner


async def main():
    log.info("════════════════════════════════════════════")
    log.info("  RDS Guard aggregator %s starting", config.BUILD_VERSION)
    log.info("════════════════════════════════════════════")
    log.info("  Public URL:  %s", config.PUBLIC_URL or "(not set)")
    log.info("  Transcribe:  %s", config.TRANSCRIPTION_ENGINE)
    log.info("  MQTT:        %s", "enabled" if config.MQTT_ENABLED else "disabled")
    log.info("  Retention:   %d days", config.EVENT_RETENTION_DAYS)
    Path(config.AUDIO_DIR).mkdir(parents=True, exist_ok=True)
    store.init_db(config.DB_PATH)
    notifier.init()

    # Central transcription (shared implementation with the satellite)
    from transcriber import create_transcriber
    transcriber = create_transcriber(
        engine=config.TRANSCRIPTION_ENGINE,
        language=config.TRANSCRIPTION_LANGUAGE,
        model_size=config.TRANSCRIPTION_MODEL,
        device=config.TRANSCRIPTION_DEVICE,
        remote_url=config.WHISPER_REMOTE_URL,
        remote_timeout=config.WHISPER_REMOTE_TIMEOUT,
        initial_prompt=config.TRANSCRIPTION_PROMPT,
        beam_size=config.TRANSCRIPTION_BEAM_SIZE,
        cpu_threads=config.TRANSCRIPTION_CPU_THREADS,
        elevenlabs_api_key=config.ELEVENLABS_API_KEY,
        elevenlabs_model=config.ELEVENLABS_MODEL,
        elevenlabs_keyterms=config.ELEVENLABS_KEYTERMS,
        elevenlabs_timeout=config.ELEVENLABS_TIMEOUT,
        fallback=config.TRANSCRIPTION_FALLBACK,
    )
    if transcriber:
        transcriber.start()

    mqtt = None
    if config.MQTT_ENABLED:
        mqtt = MqttOut()
        mqtt.start()

    pipeline = Pipeline(transcriber=transcriber, mqtt=mqtt)
    pipeline.requeue_pending()
    stop_threads = threading.Event()
    threading.Thread(target=pipeline.run, args=(stop_threads,),
                     daemon=True, name="sweeper").start()

    runners = [
        await _serve(create_ingest_app(pipeline), config.INGEST_PORT,
                     "Ingest API"),
        await _serve(create_web_app(pipeline, mqtt), config.WEB_PORT,
                     "Web UI"),
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("Aggregator running")
    await stop.wait()

    log.info("Shutting down...")
    stop_threads.set()
    for r in runners:
        await r.cleanup()
    if mqtt:
        mqtt.stop()


if __name__ == "__main__":
    asyncio.run(main())
