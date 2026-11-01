"""Publish the retained summary."""

import json
import urllib.parse
from urllib.parse import unquote

import paho.mqtt.client as mqtt

TOPIC = "homy/ioniq/journeys"


def publish(url: str, payload: dict, topic: str = TOPIC) -> None:
    u = urllib.parse.urlparse(url)
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ev-journal")
    if u.username:
        c.username_pw_set(unquote(u.username), unquote(u.password or ""))
    c.connect(u.hostname, u.port or 1883)
    c.loop_start()
    try:
        info = c.publish(topic, json.dumps(payload), qos=1, retain=True)
        info.wait_for_publish(timeout=30)
        if not info.is_published():
            raise RuntimeError(f"MQTT publish to {topic} not confirmed")
    finally:
        c.disconnect()
        c.loop_stop()
