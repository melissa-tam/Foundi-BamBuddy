"""send_command returns a bool and never silently drops a printer command (B2).

Previously ``send_command`` returned None and no-op'd when the MQTT client was
missing/disconnected — a lost command with no signal. It now returns True on a
successful publish and False (with a WARNING naming the command verb + serial)
when the client is gone/disconnected or the broker rejects the publish. The thin
control wrappers (stop_print/pause_print/...) already return False when
disconnected; those contracts are re-asserted here.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import paho.mqtt.client as mqtt
import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient


@pytest.fixture
def client():
    c = BambuMQTTClient(ip_address="192.168.1.100", serial_number="TEST123", access_code="12345678")
    return c


def _connected(c):
    c._client = MagicMock()
    c._client.publish.return_value = SimpleNamespace(rc=mqtt.MQTT_ERR_SUCCESS)
    c.state.connected = True


class TestSendCommandContract:
    def test_returns_true_on_publish(self, client):
        _connected(client)
        assert client.send_command({"print": {"command": "gcode_line"}}) is True
        client._client.publish.assert_called_once()

    def test_returns_false_and_warns_when_client_missing(self, client, caplog):
        client._client = None
        client.state.connected = True  # connected flag set but no client object
        with caplog.at_level(logging.WARNING):
            result = client.send_command({"print": {"command": "stop"}})
        assert result is False
        assert any("Dropped MQTT command" in r.message and "stop" in r.message for r in caplog.records)
        assert any("TEST123" in r.message for r in caplog.records)

    def test_returns_false_and_warns_when_disconnected(self, client, caplog):
        client._client = MagicMock()
        client.state.connected = False
        with caplog.at_level(logging.WARNING):
            result = client.send_command({"pushing": {"command": "pushall"}})
        assert result is False
        client._client.publish.assert_not_called()
        assert any("Dropped MQTT command" in r.message and "pushall" in r.message for r in caplog.records)

    def test_returns_false_and_warns_on_publish_rejected(self, client, caplog):
        _connected(client)
        client._client.publish.return_value = SimpleNamespace(rc=mqtt.MQTT_ERR_NO_CONN)
        with caplog.at_level(logging.WARNING):
            result = client.send_command({"print": {"command": "resume"}})
        assert result is False
        assert any("publish for command 'resume'" in r.message for r in caplog.records)

    def test_command_verb_extraction_never_raises(self):
        # Malformed payloads must not blow up the log path.
        assert BambuMQTTClient._command_verb({}) == "unknown"
        assert BambuMQTTClient._command_verb({"print": {"no_command": 1}}) == "unknown"
        assert BambuMQTTClient._command_verb({"print": {"command": "stop"}}) == "stop"


class TestWrapperPropagation:
    """The thin control wrappers publish directly and return False when the client
    is gone/disconnected — the bool a caller sees is real."""

    def test_stop_print_false_when_disconnected(self, client):
        client._client = None
        client.state.connected = False
        assert client.stop_print() is False

    def test_pause_print_false_when_disconnected(self, client):
        client._client = None
        client.state.connected = False
        assert client.pause_print() is False

    def test_stop_print_true_when_connected(self, client):
        _connected(client)
        assert client.stop_print() is True
        client._client.publish.assert_called_once()
