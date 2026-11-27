#!/bin/bash
# The satellite's folders on the data volume
mkdir -p "${DATA_DIR:-/data}/spool"
exec python3 -u /app/rds_guard.py
