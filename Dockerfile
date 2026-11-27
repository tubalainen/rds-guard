# ================================================================
#  Stage 1: Build rtl-sdr tools and redsea from source
# ================================================================
FROM debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        meson \
        ninja-build \
        pkg-config \
        librtlsdr-dev \
        libusb-1.0-0-dev \
        libliquid-dev \
        libsndfile1-dev \
        nlohmann-json3-dev \
    && rm -rf /var/lib/apt/lists/*

# Build redsea from source
RUN git clone --depth 1 https://github.com/windytan/redsea.git /tmp/redsea \
    && cd /tmp/redsea \
    && meson setup build \
    && cd build \
    && ninja \
    && ninja install

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

# ================================================================
#  Stage 2: Slim runtime image
# ================================================================
FROM debian:bookworm-slim

# Upgrade first so the image picks up Debian security fixes that are newer
# than the base image snapshot.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        rtl-sdr \
        librtlsdr0 \
        libusb-1.0-0 \
        libliquid1 \
        libsndfile1 \
        python3 \
        python3-pip \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy redsea binary from builder
COPY --from=builder /usr/local/bin/redsea /usr/local/bin/redsea

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# Bundled Swedish Whisper model (see the model stage above)
COPY --from=model /opt/models/ /opt/models/

# Copy application files
COPY config.py /app/config.py
COPY event_store.py /app/event_store.py
COPY web_server.py /app/web_server.py
COPY pipeline.py /app/pipeline.py
COPY channelizer.py /app/channelizer.py
COPY rds_guard.py /app/rds_guard.py
COPY audio_tee.py /app/audio_tee.py
COPY audio_recorder.py /app/audio_recorder.py
COPY transcriber.py /app/transcriber.py
COPY status.py /app/status.py
COPY uplink.py /app/uplink.py
COPY notify_rules.py /app/notify_rules.py
COPY notifier.py /app/notifier.py
COPY admin_auth.py /app/admin_auth.py
# Map: geolocation of traffic messages and the bundled place-name registry
COPY gazetteer.py /app/gazetteer.py
COPY roads.py /app/roads.py
COPY geolocate.py /app/geolocate.py
COPY geo_service.py /app/geo_service.py
COPY trafikverket.py /app/trafikverket.py
COPY geodata/ /app/geodata/
COPY tools/measure_geolocation.py /app/tools/measure_geolocation.py
COPY tools/eval_geolocation.py /app/tools/eval_geolocation.py
COPY tools/geo_corpus.json /app/tools/geo_corpus.json
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Copy static web UI files
COPY static/ /app/static/

# Data volume for SQLite database
VOLUME /data

# Keep downloaded Whisper models on the data volume so they survive
# container recreation instead of being fetched again on every update
ENV HF_HOME=/data/models

ARG BUILD_VERSION=dev
ENV BUILD_VERSION=${BUILD_VERSION}

WORKDIR /app

EXPOSE 8022

ENTRYPOINT ["/app/entrypoint.sh"]
