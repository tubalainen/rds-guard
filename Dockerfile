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

# Upgrade first so the image picks up Debian security fixes that are newer
# than the base image snapshot.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        rtl-sdr \
        librtlsdr0 \
        airspy \
        libairspy0 \
        libusb-1.0-0 \
        libliquid1 \
        libsndfile1 \
        python3 \
        python3-pip \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy redsea binary from builder
COPY --from=builder /usr/local/bin/redsea /usr/local/bin/redsea

# Python packages: requests, numpy, PyYAML
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# The satellite: receives, records and reports (everything else is the
# aggregator's, in its own image)
COPY config.py event_store.py rds_guard.py pipeline.py channelizer.py \
     audio_tee.py audio_recorder.py uplink.py receivers.py dongles.py \
     satellite_health.py gain_tuner.py sdr.py /app/
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

VOLUME /data
ENV DATA_DIR=/data

ARG BUILD_VERSION=dev
ENV BUILD_VERSION=${BUILD_VERSION}

WORKDIR /app

# No web port: the aggregator shows this satellite's state. Docker asks the
# health file the satellite writes every 10 s.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python3", "-m", "satellite_health"]

ENTRYPOINT ["/app/entrypoint.sh"]
