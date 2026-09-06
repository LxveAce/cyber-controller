"""Regression tests for lossless serialization of hardware identity and link truth."""

from __future__ import annotations

import json

from src.models.device import BoardType, Device, Protocol


def test_device_truth_survives_json_round_trip_without_aliasing() -> None:
    device = Device(
        port="COM23",
        name="Lab S3",
        firmware="lxveos",
        protocol=Protocol.GENERIC,
        board_type=BoardType.ESP32_S3,
        detected_chip="esp32s3",
        link={"tier": "lora", "rssi": -104, "up": True},
        link_ts=1234.5,
    )

    payload = device.to_dict()

    assert payload["detected_chip"] == "esp32s3"
    assert payload["link"] == {"tier": "lora", "rssi": -104, "up": True}
    assert payload["link_ts"] == 1234.5
    payload["link"]["tier"] = "mutated"
    assert device.link["tier"] == "lora"

    restored = Device.from_dict(json.loads(json.dumps(device.to_dict())))
    assert restored.detected_chip == "esp32s3"
    assert restored.link == {"tier": "lora", "rssi": -104, "up": True}
    assert restored.link_ts == 1234.5


def test_legacy_device_payload_keeps_safe_defaults_for_new_truth_fields() -> None:
    payload = Device(port="COM7").to_dict()
    del payload["detected_chip"]
    del payload["link"]
    del payload["link_ts"]

    restored = Device.from_dict(payload)

    assert restored.detected_chip == ""
    assert restored.link == {}
    assert restored.link_ts == 0.0
