"""config.py when the aggregator runs in the same compose project."""

import json
import os
import subprocess
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _config(**env):
    clean = {k: v for k, v in os.environ.items()
             if not k.startswith(("UPLINK_", "MQTT_", "LOCAL_", "SATELLITE_"))}
    out = subprocess.run(
        [sys.executable, "-c",
         "import json, config; print(json.dumps({k: getattr(config, k) for k in "
         "('UPLINK_ENABLED', 'UPLINK_URL', 'LOCAL_PROCESSING', 'MQTT_ENABLED')}))"],
        cwd=ROOT, env={**clean, **env}, capture_output=True, text=True, check=True)
    return json.loads(out.stdout), out.stderr


COMPOSE = {"LOCAL_AGGREGATOR": "true", "UPLINK_URL": "http://aggregator:8081",
           "MQTT_ENABLED": "true"}


def test_without_a_token_it_runs_standalone_with_mqtt():
    cfg, err = _config(**COMPOSE)
    assert cfg == {"UPLINK_ENABLED": False, "UPLINK_URL": "",
                   "LOCAL_PROCESSING": True, "MQTT_ENABLED": True}
    assert "set UPLINK_TOKEN" in err


def test_with_a_token_it_only_records_and_hands_over_mqtt():
    cfg, err = _config(**COMPOSE, UPLINK_TOKEN="abc")
    assert cfg == {"UPLINK_ENABLED": True, "UPLINK_URL": "http://aggregator:8081",
                   "LOCAL_PROCESSING": False, "MQTT_ENABLED": False}
    assert "MQTT and push come from the aggregator" in err


def test_owner_can_keep_processing_and_mqtt():
    cfg, _ = _config(**COMPOSE, UPLINK_TOKEN="abc", LOCAL_PROCESSING="true",
                     SATELLITE_MQTT_ENABLED="true")
    assert cfg["LOCAL_PROCESSING"] is True and cfg["MQTT_ENABLED"] is True


def test_a_remote_satellite_is_unchanged():
    cfg, _ = _config(UPLINK_URL="https://rds.example.com", UPLINK_TOKEN="abc",
                     MQTT_ENABLED="true")
    assert cfg == {"UPLINK_ENABLED": True, "UPLINK_URL": "https://rds.example.com",
                   "LOCAL_PROCESSING": True, "MQTT_ENABLED": True}
