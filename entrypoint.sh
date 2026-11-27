#!/bin/bash
# The satellite's folders on the data volume
mkdir -p "${DATA_DIR:-/data}/spool"
# A command (e.g. python3 -m gain_tuner --serial …) runs instead of the satellite
if [ "$#" -gt 0 ]; then
    exec "$@"
fi
exec python3 -u /app/rds_guard.py
