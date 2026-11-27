# ================================================================
#  Stage 1: Build the RTL-SDR driver and redsea from source
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
        libusb-1.0-0-dev \
        libliquid-dev \
        libsndfile1-dev \
        nlohmann-json3-dev \
    && rm -rf /var/lib/apt/lists/*

# The RTL-SDR Blog driver instead of Debian's rtl-sdr: it supports the
# RTL-SDR Blog V4 (R828D tuner), which Debian's tunes wrongly, and every
# older dongle as before. Installed in /usr/local of this stage only.
RUN git clone --depth 1 --branch v1.3.6 https://github.com/rtlsdrblog/rtl-sdr-blog.git /tmp/rtl-sdr \
    && cmake -S /tmp/rtl-sdr -B /tmp/rtl-sdr/build -DCMAKE_BUILD_TYPE=Release \
         -DDETACH_KERNEL_DRIVER=ON -DINSTALL_UDEV_RULES=OFF -DCMAKE_INSTALL_PREFIX=/opt/rtl-sdr \
    && cmake --build /tmp/rtl-sdr/build -j"$(nproc)" \
    && cmake --install /tmp/rtl-sdr/build

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
# than the base image snapshot. airspy: Debian's airspy_rx and airspy_info
# (1.0.10) for the Airspy Mini and R2.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends \
        airspy \
        libairspy0 \
        libusb-1.0-0 \
        libliquid1 \
        libsndfile1 \
        python3 \
        python3-pip \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Copy redsea and the RTL-SDR driver and tools from the builder
COPY --from=builder /usr/local/bin/redsea /usr/local/bin/redsea
COPY --from=builder /opt/rtl-sdr/lib/librtlsdr.so* /usr/local/lib/
COPY --from=builder /opt/rtl-sdr/bin/rtl_sdr /opt/rtl-sdr/bin/rtl_test \
     /opt/rtl-sdr/bin/rtl_eeprom /opt/rtl-sdr/bin/rtl_biast /usr/local/bin/
RUN ldconfig && rtl_test -h 2>&1 | head -1

# Python packages: requests, numpy, PyYAML
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /app/requirements.txt

# The satellite: receives, records and reports (everything else is the
# aggregator's, in its own image)
COPY config.py event_store.py rds_guard.py pipeline.py channelizer.py \
     audio_tee.py audio_recorder.py uplink.py receivers.py dongles.py \
     satellite_health.py gain_tuner.py sdr.py /app/
# The I/Q and ppm check (python3 iq_side.py 103.5M --serial …), next to
# dongles.py and sdr.py, which it uses
COPY tools/iq_side.py /app/iq_side.py
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
