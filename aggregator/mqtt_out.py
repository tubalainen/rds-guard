"""MQTT publisher for the aggregator (optional)."""

import json
import logging
import re
import threading
import unicodedata

from . import config

log = logging.getLogger("rds-aggregator")


def topic_slug(name):
    """Make a node name safe for use as one MQTT topic level."""
    ascii_name = unicodedata.normalize("NFKD", name or "").encode(
        "ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9_-]+", "-", ascii_name.lower()).strip("-")
    return slug or "node"


class MqttOut:
    """Thin wrapper around paho: background connect, publish-if-connected."""

    def __init__(self):
        self._client = None
        self._connected = threading.Event()
        self.prefix = config.MQTT_TOPIC_PREFIX.rstrip("/")

    @property
    def connected(self):
        return self._connected.is_set()

    def start(self):
        if not config.MQTT_HOST:
            log.warning("MQTT enabled but MQTT_HOST is empty — skipping MQTT")
            return
        threading.Thread(target=self._connect, daemon=True,
                         name="mqtt-connect").start()

    def _connect(self):
        import paho.mqtt.client as mqtt

        def on_connect(client, userdata, flags, reason_code, properties):
            if reason_code == 0:
                log.info("MQTT connected to %s:%s", config.MQTT_HOST,
                         config.MQTT_PORT)
                self._connected.set()
                client.publish(f"{self.prefix}/aggregator/status",
                               json.dumps({"online": True}), qos=1, retain=True)
            else:
                log.error("MQTT connection refused: reason_code=%s", reason_code)
                self._connected.clear()

        def on_disconnect(client, userdata, flags, reason_code, properties):
            if reason_code != 0:
                log.warning("MQTT lost connection (%s) — reconnecting...",
                            reason_code)
            self._connected.clear()

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                             client_id=config.MQTT_CLIENT_ID,
                             protocol=mqtt.MQTTv311)
        client.on_connect = on_connect
        client.on_disconnect = on_disconnect
        if config.MQTT_USER:
            client.username_pw_set(config.MQTT_USER, config.MQTT_PASSWORD)
        client.will_set(f"{self.prefix}/aggregator/status",
                        json.dumps({"online": False}), qos=1, retain=True)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        log.info("MQTT connecting to %s:%s ...", config.MQTT_HOST,
                 config.MQTT_PORT)
        try:
            client.connect_async(config.MQTT_HOST, config.MQTT_PORT,
                                 keepalive=60)
            client.loop_start()
            self._client = client
        except Exception as e:
            log.error("MQTT setup failed: %s", e)

    def publish(self, topic, payload, qos=1, retain=False):
        """Publish JSON under the prefix. Returns False if not connected."""
        client = self._client
        if client is None or not self.connected:
            return False
        try:
            info = client.publish(f"{self.prefix}/{topic}",
                                  json.dumps(payload, ensure_ascii=False),
                                  qos=qos, retain=retain)
            return info.rc == 0
        except Exception:
            log.debug("MQTT publish failed", exc_info=True)
            return False

    def stop(self):
        client = self._client
        if client is not None:
            try:
                client.publish(f"{self.prefix}/aggregator/status",
                               json.dumps({"online": False}), qos=1,
                               retain=True)
                client.loop_stop()
                client.disconnect()
            except Exception:
                pass
