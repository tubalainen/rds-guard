# RDS Guard aggregator — collects events from RDS Guard satellites.
# Build from the repository root (it reuses transcriber.py):
#   docker build -f aggregator/Dockerfile -t rds-guard-aggregator .
# ================================================================
#  Model stage: KB-Whisper small, "strict" style, for faster-whisper
# ================================================================
# KBLab only publishes the CTranslate2 file for the default style, so the
# strict (most verbatim) one is converted here.  Runs on the build host's
# architecture (the output is portable); torch never reaches the runtime image.
FROM --platform=$BUILDPLATFORM python:3.12-slim-bookworm AS model
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu \
        torch \
    && pip install --no-cache-dir "transformers>=4.40,<5" "ctranslate2>=4.5,<5" huggingface_hub
COPY tools/convert_kb_whisper.py /tmp/convert_kb_whisper.py
# Commit of the "strict" tag, pinned for reproducible builds
ARG KB_WHISPER_REVISION=066ef166dd25b4b27039517ca77af30c1c10688a
RUN python3 /tmp/convert_kb_whisper.py KBLab/kb-whisper-small \
        "$KB_WHISPER_REVISION" /opt/models/kb-whisper-small-strict int8 \
    && rm -rf /root/.cache

FROM python:3.12-slim-bookworm

# Upgrade first so the image picks up Debian security fixes that are newer
# than the base image snapshot.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

COPY aggregator/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Bundled Swedish Whisper model (see the model stage above)
COPY --from=model /opt/models/ /opt/models/

# Shared with the satellite: Whisper transcription (local or remote) and
# the web UI's base theme
COPY transcriber.py /app/transcriber.py
COPY static/css/style.css /app/static/css/style.css
# Code and UI only — never .env, compose files or local data
COPY aggregator/*.py /app/aggregator/
COPY aggregator/static/ /app/aggregator/static/

# Runs as an unprivileged user: the ingest API faces the internet
RUN useradd --system --uid 10001 --home-dir /data rdsagg \
    && mkdir -p /data/audio /data/models \
    && chown -R rdsagg:rdsagg /data
USER rdsagg

ENV DATA_DIR=/data \
    HF_HOME=/data/models \
    PYTHONUNBUFFERED=1
VOLUME /data

ARG BUILD_VERSION=dev
ENV BUILD_VERSION=${BUILD_VERSION}

WORKDIR /app
EXPOSE 8080 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/v1/status', timeout=4)"]

CMD ["python3", "-m", "aggregator.app"]
