"""Publish the retained summary."""

import json
import urllib.parse

import paho.mqtt.client as mqtt

TOPIC = "homy/ioniq/journeys"


def publish(url: str, payload: dict, topic: str = TOPIC) -> None:
    u = urllib.parse.urlparse(url)
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ev-journal")
    if u.username:
        c.username_pw_set(u.username, u.password)
    c.connect(u.hostname, u.port or 1883)
    c.loop_start()
    try:
        c.publish(topic, json.dumps(payload), qos=1, retain=True).wait_for_publish(timeout=30)
    finally:
        c.disconnect()
        c.loop_stop()
