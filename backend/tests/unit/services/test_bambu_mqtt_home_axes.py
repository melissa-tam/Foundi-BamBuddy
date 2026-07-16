"""Unit tests for BambuMQTTClient.home_axes model-aware homing (F1 / 007-H2C).

A bare ``G28`` stall-loops dual-nozzle H2-series firmware (the sensorless X-homing
stall threshold rams the dual carriage into the X wall — 007-H2C ram incident,
2026-07-12). ``home_axes`` must therefore emit the torque-parameterized stock forms
on dual-nozzle models and keep the bare ``G28`` for single-nozzle models.

The client is constructed directly — ``__init__`` does no network — and
``send_gcode`` is monkeypatched to capture the exact G-code payload.
"""

from __future__ import annotations

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient

_TRIPLET = "G28 X T300\nG28 Y T300\nG28 Z P0 T250"


def _client(model: str | None):
    return BambuMQTTClient(
        ip_address="127.0.0.1",
        serial_number="TESTSN",
        access_code="00000000",
        model=model,
    )


def _capture(client) -> list[str]:
    sent: list[str] = []

    def _send(gcode: str) -> bool:
        sent.append(gcode)
        return True

    client.send_gcode = _send
    return sent


def test_single_nozzle_h2s_sends_bare_g28():
    client = _client("H2S")
    sent = _capture(client)
    assert client.home_axes() is True
    assert sent == ["G28"]


@pytest.mark.parametrize("model", ["H2C", "O1C2"])
def test_dual_nozzle_model_sends_torque_triplet(model):
    # Both the display name (H2C) and the raw device code (O1C2) resolve dual.
    client = _client(model)
    sent = _capture(client)
    assert client.home_axes() is True
    assert sent == [_TRIPLET]


def test_runtime_dual_flag_overrides_missing_model():
    # No configured model, but device.extruder.info flagged dual at runtime — the
    # ``_is_dual_nozzle or is_dual_nozzle_model`` idiom must still pick the triplet
    # so a mis-modelled real dual is never bare-G28'd.
    client = _client(None)
    client._is_dual_nozzle = True
    sent = _capture(client)
    assert client.home_axes() is True
    assert sent == [_TRIPLET]


def test_axes_argument_is_ignored_single_nozzle():
    # The legacy axes argument never narrows the home — always a full home.
    client = _client("H2S")
    sent = _capture(client)
    assert client.home_axes("z") is True
    assert sent == ["G28"]
