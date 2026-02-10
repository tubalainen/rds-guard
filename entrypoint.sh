#!/bin/bash
# Ensure audio directory exists
mkdir -p "${AUDIO_DIR:-/data/audio}"
exec python3 -u /app/rds_guard.py
