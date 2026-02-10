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
#  Stage 2: Slim runtime image
# ================================================================
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
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

# Copy application files
COPY config.py /app/config.py
COPY event_store.py /app/event_store.py
COPY web_server.py /app/web_server.py
COPY pipeline.py /app/pipeline.py
COPY rds_guard.py /app/rds_guard.py
COPY audio_tee.py /app/audio_tee.py
COPY audio_recorder.py /app/audio_recorder.py
COPY transcriber.py /app/transcriber.py
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Copy static web UI files
COPY static/ /app/static/

# Data volume for SQLite database
VOLUME /data

WORKDIR /app

EXPOSE 8022

ENTRYPOINT ["/app/entrypoint.sh"]
