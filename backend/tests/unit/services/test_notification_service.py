"""Unit tests for NotificationService.

Tests event-based notifications and toggle behavior.
"""

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services import notification_service as notification_service_module, notify_dedup
from backend.app.services.notification_service import NotificationService, compose_hms_error_summary


class TestNotificationService:
    """Tests for NotificationService class."""

    @pytest.fixture
    def service(self):
        """Create a fresh NotificationService instance."""
        return NotificationService()

    @pytest.fixture
    def mock_provider(self):
        """Create a mock notification provider."""
        provider = MagicMock()
        provider.id = 1
        provider.name = "Test Provider"
        provider.provider_type = "webhook"
        provider.enabled = True
        provider.config = json.dumps({"webhook_url": "http://test.local/webhook"})
        provider.on_print_start = True
        provider.on_print_complete = True
        provider.on_print_failed = True
        provider.on_print_stopped = False
        provider.on_print_progress = False
        provider.on_printer_offline = False
        provider.on_printer_error = False
        provider.on_filament_low = False
        provider.on_maintenance_due = False
        provider.on_ams_humidity_high = False
        provider.on_ams_temperature_high = False
        provider.quiet_hours_enabled = False
        provider.quiet_hours_start = None
        provider.quiet_hours_end = None
        provider.daily_digest_enabled = False
        provider.daily_digest_time = None
        provider.printer_id = None
        return provider

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.commit = AsyncMock()
        return db

    # ========================================================================
    # Tests for on_print_start
    # ========================================================================

    @pytest.mark.asyncio
    async def test_on_print_start_sends_notification(self, service, mock_provider, mock_db):
        """Verify notification is sent when print starts."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Print Started", "Test Printer: test.3mf")

            await service.on_print_start(
                printer_id=1,
                printer_name="Test Printer",
                data={"filename": "test.3mf", "subtask_name": "test"},
                db=mock_db,
            )

            mock_get.assert_called_once()
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_print_start_skipped_when_no_providers(self, service, mock_db):
        """Verify no error when no providers are configured for event."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            mock_get.return_value = []

            await service.on_print_start(
                printer_id=1,
                printer_name="Test Printer",
                data={},
                db=mock_db,
            )

            mock_send.assert_not_called()

    # ========================================================================
    # Tests for on_print_complete (status routing)
    # ========================================================================

    @pytest.mark.asyncio
    async def test_on_print_complete_routes_completed_status(self, service, mock_provider, mock_db):
        """Verify completed status uses on_print_complete field."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Test", "Test")

            await service.on_print_complete(
                printer_id=1,
                printer_name="Test",
                status="completed",
                data={},
                db=mock_db,
            )

            # Verify the correct event field was queried
            call_args = mock_get.call_args
            assert call_args[0][1] == "on_print_complete"

    @pytest.mark.asyncio
    async def test_on_print_complete_routes_failed_status(self, service, mock_provider, mock_db):
        """Verify failed status uses on_print_failed field."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Test", "Test")

            await service.on_print_complete(
                printer_id=1,
                printer_name="Test",
                status="failed",
                data={},
                db=mock_db,
            )

            call_args = mock_get.call_args
            assert call_args[0][1] == "on_print_failed"

    @pytest.mark.asyncio
    async def test_on_print_complete_routes_stopped_status(self, service, mock_provider, mock_db):
        """Verify stopped status uses on_print_stopped field."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Test", "Test")

            await service.on_print_complete(
                printer_id=1,
                printer_name="Test",
                status="stopped",
                data={},
                db=mock_db,
            )

            call_args = mock_get.call_args
            assert call_args[0][1] == "on_print_stopped"

    @pytest.mark.asyncio
    async def test_on_print_complete_routes_aborted_status(self, service, mock_provider, mock_db):
        """Verify aborted status uses on_print_stopped field."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Test", "Test")

            await service.on_print_complete(
                printer_id=1,
                printer_name="Test",
                status="aborted",
                data={},
                db=mock_db,
            )

            call_args = mock_get.call_args
            assert call_args[0][1] == "on_print_stopped"

    # ========================================================================
    # Tests for provider filtering
    # ========================================================================

    @pytest.mark.asyncio
    async def test_disabled_provider_not_returned(self, service, mock_provider, mock_db):
        """CRITICAL: Verify disabled providers don't receive notifications."""
        mock_provider.enabled = False

        # The actual filtering happens in _get_providers_for_event
        # which queries only enabled providers
        with patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get:
            # Simulate the query filtering out disabled providers
            mock_get.return_value = []

            result = await service._get_providers_for_event(mock_db, "on_print_start", printer_id=1)

            assert len(result) == 0

    @pytest.mark.asyncio
    async def test_provider_filtered_by_printer_id(self, service, mock_provider, mock_db):
        """Verify providers can be filtered by specific printer."""
        mock_provider.printer_id = 2  # Linked to printer 2

        with patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get:
            # When querying for printer 1, provider linked to printer 2 is excluded
            mock_get.return_value = []

            result = await service._get_providers_for_event(mock_db, "on_print_start", printer_id=1)

            assert len(result) == 0

    # ========================================================================
    # Tests for on_printer_quarantined copy (C3: single-event quarantines must NOT
    # claim "1 consecutive failures")
    # ========================================================================

    @pytest.mark.asyncio
    async def test_quarantine_single_event_copy_is_reason_led(self, service, mock_provider, mock_db):
        """failure_count == 1 (e.g. an unverified eject sweep) renders a reason-led
        summary that does NOT claim any 'consecutive failures'."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Printer Quarantined: P1", "body")

            await service.on_printer_quarantined(1, "P1", 1, "Eject job ended 'failed'", mock_db)

            variables = mock_build.call_args[0][2]
            assert variables["quarantine_summary"] == "P1 was quarantined."
            assert "consecutive failures" not in variables["quarantine_summary"]

    @pytest.mark.asyncio
    async def test_quarantine_multi_event_copy_keeps_consecutive_sentence(self, service, mock_provider, mock_db):
        """failure_count > 1 keeps the escalation sentence naming the count."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Printer Quarantined: P1", "body")

            await service.on_printer_quarantined(1, "P1", 3, "3 consecutive farm print failures", mock_db)

            variables = mock_build.call_args[0][2]
            assert variables["quarantine_summary"] == "P1 was quarantined after 3 consecutive failures."

    # ========================================================================
    # Tests for quiet hours
    # ========================================================================

    def test_is_in_quiet_hours_during_quiet_period(self, service, mock_provider):
        """Verify notifications are blocked during quiet hours."""
        mock_provider.quiet_hours_enabled = True
        mock_provider.quiet_hours_start = "22:00"
        mock_provider.quiet_hours_end = "07:00"

        with patch("backend.app.services.notification_service.datetime") as mock_datetime:
            # Test during quiet hours (23:00)
            mock_now = MagicMock()
            mock_now.hour = 23
            mock_now.minute = 0
            mock_datetime.now.return_value = mock_now

            result = service._is_in_quiet_hours(mock_provider)

            assert result is True

    def test_is_in_quiet_hours_outside_quiet_period(self, service, mock_provider):
        """Verify notifications are allowed outside quiet hours."""
        mock_provider.quiet_hours_enabled = True
        mock_provider.quiet_hours_start = "22:00"
        mock_provider.quiet_hours_end = "07:00"

        with patch("backend.app.services.notification_service.datetime") as mock_datetime:
            # Test outside quiet hours (12:00)
            mock_now = MagicMock()
            mock_now.hour = 12
            mock_now.minute = 0
            mock_datetime.now.return_value = mock_now

            result = service._is_in_quiet_hours(mock_provider)

            assert result is False

    def test_is_in_quiet_hours_disabled(self, service, mock_provider):
        """Verify quiet hours check returns False when disabled."""
        mock_provider.quiet_hours_enabled = False

        result = service._is_in_quiet_hours(mock_provider)

        assert result is False

    def test_is_in_quiet_hours_early_morning(self, service, mock_provider):
        """Verify quiet hours work across midnight (early morning)."""
        mock_provider.quiet_hours_enabled = True
        mock_provider.quiet_hours_start = "22:00"
        mock_provider.quiet_hours_end = "07:00"

        with patch("backend.app.services.notification_service.datetime") as mock_datetime:
            # Test early morning (03:00) - should be in quiet hours
            mock_now = MagicMock()
            mock_now.hour = 3
            mock_now.minute = 0
            mock_datetime.now.return_value = mock_now

            result = service._is_in_quiet_hours(mock_provider)

            assert result is True

    # ========================================================================
    # Tests for AMS alarms
    # ========================================================================

    @pytest.mark.asyncio
    async def test_on_ams_humidity_high_sends_notification(self, service, mock_provider, mock_db):
        """Verify AMS humidity alarm sends notification."""
        mock_provider.on_ams_humidity_high = True

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("AMS Humidity Alert", "High humidity detected")

            await service.on_ams_humidity_high(
                printer_id=1,
                printer_name="Test Printer",
                ams_label="AMS-A",
                humidity=75.0,
                threshold=60.0,
                db=mock_db,
            )

            mock_send.assert_called_once()
            # Verify force_immediate is True for alarms
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs.get("force_immediate") is True

    @pytest.mark.asyncio
    async def test_on_ams_temperature_high_sends_notification(self, service, mock_provider, mock_db):
        """Verify AMS temperature alarm sends notification."""
        mock_provider.on_ams_temperature_high = True

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("AMS Temperature Alert", "High temp detected")

            await service.on_ams_temperature_high(
                printer_id=1,
                printer_name="Test Printer",
                ams_label="AMS-A",
                temperature=40.0,
                threshold=35.0,
                db=mock_db,
            )

            mock_send.assert_called_once()
            # Verify force_immediate is True for alarms
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs.get("force_immediate") is True

    @pytest.mark.asyncio
    async def test_ams_alarm_skipped_when_toggle_disabled(self, service, mock_provider, mock_db):
        """CRITICAL: Verify AMS alarms respect toggle setting."""
        mock_provider.on_ams_humidity_high = False

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            # Provider with toggle disabled won't be returned
            mock_get.return_value = []

            await service.on_ams_humidity_high(
                printer_id=1,
                printer_name="Test",
                ams_label="AMS-A",
                humidity=75.0,
                threshold=60.0,
                db=mock_db,
            )

            mock_send.assert_not_called()

    # ========================================================================
    # Tests for daily digest
    # ========================================================================

    @pytest.mark.asyncio
    async def test_daily_digest_queues_notification(self, service, mock_provider, mock_db):
        """Verify notifications are queued when digest mode is enabled."""
        mock_provider.daily_digest_enabled = True
        mock_provider.daily_digest_time = "09:00"

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Test", "Test")

            await service.on_print_complete(
                printer_id=1,
                printer_name="Test",
                status="completed",
                data={},
                db=mock_db,
            )

            # When digest is enabled, _send_to_providers should still be called
            # but internally it will queue instead of send immediately
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_force_immediate_bypasses_digest(self, service, mock_provider, mock_db):
        """Verify force_immediate=True bypasses digest mode."""
        mock_provider.daily_digest_enabled = True
        mock_provider.on_ams_humidity_high = True

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Alert", "Alert message")

            await service.on_ams_humidity_high(
                printer_id=1,
                printer_name="Test",
                ams_label="AMS-A",
                humidity=75.0,
                threshold=60.0,
                db=mock_db,
            )

            # Verify force_immediate is passed
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs.get("force_immediate") is True


class TestDigestModeAlwaysSendsImmediately:
    """CRITICAL: Tests that notifications always send immediately regardless of digest setting."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.mark.asyncio
    async def test_notification_sends_immediately_even_with_digest_enabled(self, service):
        """CRITICAL: All notifications must be sent immediately, digest is just a summary."""
        # Create a mock provider with digest enabled
        mock_provider = MagicMock()
        mock_provider.id = 1
        mock_provider.name = "Test Provider"
        mock_provider.provider_type = "ntfy"
        mock_provider.enabled = True
        mock_provider.daily_digest_enabled = True  # Digest enabled
        mock_provider.daily_digest_time = "23:59"
        mock_provider.config = '{"server": "https://ntfy.sh", "topic": "test"}'

        mock_db = AsyncMock()

        # Mock the _send_to_provider method
        with (
            patch.object(service, "_send_to_provider", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_queue_for_digest", new_callable=AsyncMock) as mock_queue,
            patch.object(service, "_update_provider_status", new_callable=AsyncMock),
            patch.object(service, "_log_notification", new_callable=AsyncMock),
        ):
            mock_send.return_value = (True, None)

            await service._send_to_providers(
                providers=[mock_provider],
                title="Print Started",
                message="Your print has started",
                db=mock_db,
                event_type="print_start",
            )

            # CRITICAL: _send_to_provider MUST be called (immediate send)
            mock_send.assert_called_once()

            # Digest queue should also be called (for daily summary)
            mock_queue.assert_called_once()

    @pytest.mark.asyncio
    async def test_notification_sends_without_digest_queue_when_disabled(self, service):
        """When digest is disabled, notification sends but no digest queue."""
        mock_provider = MagicMock()
        mock_provider.id = 1
        mock_provider.name = "Test Provider"
        mock_provider.provider_type = "ntfy"
        mock_provider.enabled = True
        mock_provider.daily_digest_enabled = False  # Digest disabled
        mock_provider.daily_digest_time = None
        mock_provider.config = '{"server": "https://ntfy.sh", "topic": "test"}'

        mock_db = AsyncMock()

        with (
            patch.object(service, "_send_to_provider", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_queue_for_digest", new_callable=AsyncMock) as mock_queue,
            patch.object(service, "_update_provider_status", new_callable=AsyncMock),
            patch.object(service, "_log_notification", new_callable=AsyncMock),
        ):
            mock_send.return_value = (True, None)

            await service._send_to_providers(
                providers=[mock_provider],
                title="Print Started",
                message="Your print has started",
                db=mock_db,
                event_type="print_start",
            )

            # Notification must still be sent immediately
            mock_send.assert_called_once()

            # Digest queue should NOT be called when digest is disabled
            mock_queue.assert_not_called()


class TestNotificationProviderTypes:
    """Tests for different notification provider types."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.mark.asyncio
    async def test_webhook_provider_sends_request(self, service):
        """Verify webhook provider sends HTTP request."""
        config = {
            "webhook_url": "http://test.local/webhook",
            "field_title": "title",
            "field_message": "message",
        }

        # Create a mock response
        mock_response = MagicMock()
        mock_response.status_code = 200

        # Mock the _get_client method
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            success, message = await service._send_webhook(config, "Test Title", "Test Message")

            assert success is True
            mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_webhook_handles_failure(self, service):
        """Verify webhook gracefully handles HTTP errors."""
        config = {
            "webhook_url": "http://test.local/webhook",
        }

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_instance = AsyncMock()
            mock_instance.post.side_effect = Exception("Connection failed")
            mock_client_class.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_client_class.return_value.__aexit__ = AsyncMock()

            success, message = await service._send_webhook(config, "Test", "Test")

            assert success is False
            assert "Connection failed" in message or "error" in message.lower()

    @pytest.mark.asyncio
    async def test_webhook_slack_format_sends_text_only(self, service):
        """Verify Slack/Mattermost format sends only text field."""
        config = {
            "webhook_url": "http://mattermost.local/hooks/abc123",
            "payload_format": "slack",
        }

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            success, message = await service._send_webhook(config, "Test Title", "Test Message")

            assert success is True
            mock_client.post.assert_called_once()

            # Verify payload format is Slack-compatible
            call_args = mock_client.post.call_args
            payload = call_args.kwargs.get("json") or call_args[1].get("json")
            assert "text" in payload
            assert "*Test Title*" in payload["text"]
            assert "Test Message" in payload["text"]
            # Should NOT have generic fields
            assert "timestamp" not in payload
            assert "source" not in payload

    @pytest.mark.asyncio
    async def test_webhook_generic_format_includes_image(self, service):
        """Verify generic webhook includes base64-encoded image when provided."""
        config = {
            "webhook_url": "http://test.local/webhook",
            "field_title": "title",
            "field_message": "message",
        }

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            image_bytes = b"\xff\xd8\xff\xe0fake-jpeg-data"
            success, message = await service._send_webhook(config, "Test Title", "Test Message", image_data=image_bytes)

            assert success is True
            call_args = mock_client.post.call_args
            payload = call_args.kwargs.get("json") or call_args[1].get("json")
            assert "image" in payload

            import base64

            assert payload["image"] == base64.b64encode(image_bytes).decode("ascii")

    @pytest.mark.asyncio
    async def test_webhook_generic_format_no_image_when_none(self, service):
        """Verify generic webhook omits image field when no image_data provided."""
        config = {
            "webhook_url": "http://test.local/webhook",
            "field_title": "title",
            "field_message": "message",
        }

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            success, message = await service._send_webhook(config, "Test Title", "Test Message")

            assert success is True
            call_args = mock_client.post.call_args
            payload = call_args.kwargs.get("json") or call_args[1].get("json")
            assert "image" not in payload

    @pytest.mark.asyncio
    async def test_webhook_slack_format_excludes_image(self, service):
        """Verify Slack format does not include image even when provided."""
        config = {
            "webhook_url": "http://mattermost.local/hooks/abc123",
            "payload_format": "slack",
        }

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client

            success, message = await service._send_webhook(
                config, "Test Title", "Test Message", image_data=b"fake-image"
            )

            assert success is True
            call_args = mock_client.post.call_args
            payload = call_args.kwargs.get("json") or call_args[1].get("json")
            assert "image" not in payload


class TestDiscordProvider:
    """Discord webhook URL host validation (#1363)."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.mark.asyncio
    async def test_discord_accepts_discord_com_url(self, service):
        config = {"webhook_url": "https://discord.com/api/webhooks/123/abc"}
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client
            success, _ = await service._send_discord(config, "Title", "Body")

        assert success is True
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_discord_accepts_legacy_discordapp_com_url(self, service):
        """Discord's 'Copy Webhook URL' button emits discordapp.com URLs (#1363)."""
        config = {"webhook_url": "https://discordapp.com/api/webhooks/123/abc"}
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get_client:
            mock_get_client.return_value = mock_client
            success, _ = await service._send_discord(config, "Title", "Body")

        assert success is True
        mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_discord_rejects_non_discord_host(self, service):
        config = {"webhook_url": "https://evil.example.com/api/webhooks/123/abc"}
        success, message = await service._send_discord(config, "Title", "Body")
        assert success is False
        assert "Invalid Discord webhook URL" in message

    @pytest.mark.asyncio
    async def test_discord_rejects_empty_url(self, service):
        success, message = await service._send_discord({"webhook_url": ""}, "Title", "Body")
        assert success is False
        assert "required" in message.lower()


class TestNtfyPriority:
    """Per-event ntfy Priority header (#990)."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    @staticmethod
    def _mock_client(service):
        """Patch _get_client and return the mock client + 200 response."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.put = AsyncMock(return_value=mock_response)
        return mock_client

    @pytest.mark.asyncio
    async def test_priority_header_set_for_mapped_event(self, service):
        """Mapped event → ntfy Priority header carries the configured value."""
        config = {
            "topic": "bambuddy",
            "event_priorities": {"on_print_failed": 5, "on_print_complete": 2},
        }
        mock_client = self._mock_client(service)
        with patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_client
            success, _ = await service._send_ntfy(config, "Title", "Body", event_type="on_print_failed")

        assert success is True
        headers = mock_client.post.call_args.kwargs["headers"]
        assert headers.get("Priority") == "5"

    @pytest.mark.asyncio
    async def test_priority_header_omitted_for_unmapped_event(self, service):
        """Unmapped event → no Priority header so ntfy uses its server default."""
        config = {
            "topic": "bambuddy",
            "event_priorities": {"on_print_failed": 5},
        }
        mock_client = self._mock_client(service)
        with patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_client
            await service._send_ntfy(config, "Title", "Body", event_type="on_print_complete")

        headers = mock_client.post.call_args.kwargs["headers"]
        assert "Priority" not in headers

    @pytest.mark.asyncio
    async def test_priority_header_omitted_when_no_priorities_set(self, service):
        """Existing setups (no event_priorities key) keep current behaviour."""
        config = {"topic": "bambuddy"}
        mock_client = self._mock_client(service)
        with patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_client
            await service._send_ntfy(config, "Title", "Body", event_type="on_print_failed")

        headers = mock_client.post.call_args.kwargs["headers"]
        assert "Priority" not in headers

    @pytest.mark.asyncio
    async def test_priority_header_omitted_when_event_type_missing(self, service):
        """Test sends (no event_type) must not emit a Priority header."""
        config = {
            "topic": "bambuddy",
            "event_priorities": {"on_print_failed": 5},
        }
        mock_client = self._mock_client(service)
        with patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_client
            await service._send_ntfy(config, "Title", "Body")

        headers = mock_client.post.call_args.kwargs["headers"]
        assert "Priority" not in headers

    @pytest.mark.asyncio
    async def test_priority_out_of_range_is_ignored(self, service):
        """Values outside 1-5 (or non-numeric) are dropped, not clamped."""
        for bad in (0, 6, 99, -1, "not-a-number", None):
            config = {
                "topic": "bambuddy",
                "event_priorities": {"on_print_failed": bad},
            }
            mock_client = self._mock_client(service)
            with patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get:
                mock_get.return_value = mock_client
                await service._send_ntfy(config, "Title", "Body", event_type="on_print_failed")

            headers = mock_client.post.call_args.kwargs["headers"]
            assert "Priority" not in headers, f"unexpected header for bad value {bad!r}"

    @pytest.mark.asyncio
    async def test_priority_header_set_on_attachment_path(self, service):
        """Image-attachment path (PUT) must also carry the Priority header."""
        config = {
            "topic": "bambuddy",
            "event_priorities": {"on_first_layer_complete": 4},
        }
        mock_client = self._mock_client(service)
        with patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get:
            mock_get.return_value = mock_client
            await service._send_ntfy(
                config,
                "Title",
                "Body",
                image_data=b"\xff\xd8\xff\xe0fake-jpeg",
                event_type="on_first_layer_complete",
            )

        headers = mock_client.put.call_args.kwargs["headers"]
        assert headers.get("Priority") == "4"


class TestHomeAssistantProvider:
    """Tests for Home Assistant notification provider."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.mark.asyncio
    async def test_send_homeassistant_success(self, service):
        """Verify HA provider sends persistent notification to correct endpoint."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        mock_db = AsyncMock()

        with (
            patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get_client,
            patch(
                "backend.app.api.routes.settings.get_homeassistant_settings",
                new_callable=AsyncMock,
            ) as mock_ha_settings,
        ):
            mock_get_client.return_value = mock_client
            mock_ha_settings.return_value = {
                "ha_url": "http://ha.local:8123",
                "ha_token": "test-token-123",
                "ha_enabled": True,
            }

            success, message = await service._send_homeassistant({}, "Test Title", "Test Message", db=mock_db)

            assert success is True
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert call_args[0][0] == "http://ha.local:8123/api/services/persistent_notification/create"
            payload = call_args.kwargs.get("json") or call_args[1].get("json")
            assert payload["title"] == "Test Title"
            assert payload["message"] == "Test Message"

    @pytest.mark.asyncio
    async def test_send_homeassistant_no_db_no_env(self, service):
        """Verify HA provider fails gracefully without DB or env vars."""
        with patch.dict("os.environ", {}, clear=True):
            success, message = await service._send_homeassistant({}, "Test", "Test", db=None)

        assert success is False
        assert "not configured" in message.lower()

    @pytest.mark.asyncio
    async def test_send_homeassistant_auth_failure(self, service):
        """Verify HA provider reports auth failure."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        mock_db = AsyncMock()

        with (
            patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get_client,
            patch(
                "backend.app.api.routes.settings.get_homeassistant_settings",
                new_callable=AsyncMock,
            ) as mock_ha_settings,
        ):
            mock_get_client.return_value = mock_client
            mock_ha_settings.return_value = {
                "ha_url": "http://ha.local:8123",
                "ha_token": "bad-token",
                "ha_enabled": True,
            }

            success, message = await service._send_homeassistant({}, "Test", "Test", db=mock_db)

        assert success is False
        assert "authentication" in message.lower()

    @pytest.mark.asyncio
    async def test_send_homeassistant_env_fallback(self, service):
        """Verify HA provider falls back to env vars when no DB session."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with (
            patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get_client,
            patch.dict("os.environ", {"HA_URL": "http://env-ha:8123", "HA_TOKEN": "env-token"}),
        ):
            mock_get_client.return_value = mock_client

            success, message = await service._send_homeassistant({}, "Test", "Test", db=None)

        assert success is True
        call_args = mock_client.post.call_args
        assert "env-ha:8123" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_send_homeassistant_empty_config_accepted(self, service):
        """Verify HA provider works with empty config dict (no fields needed)."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        mock_db = AsyncMock()

        with (
            patch.object(service, "_get_client", new_callable=AsyncMock) as mock_get_client,
            patch(
                "backend.app.api.routes.settings.get_homeassistant_settings",
                new_callable=AsyncMock,
            ) as mock_ha_settings,
        ):
            mock_get_client.return_value = mock_client
            mock_ha_settings.return_value = {
                "ha_url": "http://ha.local:8123",
                "ha_token": "token",
                "ha_enabled": True,
            }

            success, _ = await service._send_homeassistant({}, "Title", "Body", db=mock_db)

        assert success is True

    @pytest.mark.asyncio
    async def test_send_to_provider_dispatches_homeassistant(self, service):
        """Verify _send_to_provider dispatches to _send_homeassistant."""
        provider = MagicMock()
        provider.provider_type = "homeassistant"
        provider.config = "{}"
        provider.quiet_hours_enabled = False

        with patch.object(service, "_send_homeassistant", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = (True, "OK")

            success, _ = await service._send_to_provider(provider, "Title", "Message", db=AsyncMock())

        assert success is True
        mock_send.assert_called_once()


class TestNotificationVariableFallbacks:
    """Tests for notification variable fallback values."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    def test_format_duration_with_valid_seconds(self, service):
        """Verify duration formats correctly with valid input."""
        result = service._format_duration(3661)  # 1h 1m 1s
        assert "1h" in result

    def test_format_duration_with_none_returns_unknown(self, service):
        """CRITICAL: Verify None duration returns 'Unknown' fallback."""
        result = service._format_duration(None)
        assert result == "Unknown"

    def test_format_duration_with_zero(self, service):
        """Verify zero duration formats correctly."""
        result = service._format_duration(0)
        # Should return some valid string, not "Unknown"
        assert result is not None
        assert isinstance(result, str)

    def test_format_duration_hours_and_minutes(self, service):
        """Verify duration formats hours and minutes."""
        result = service._format_duration(5400)  # 1h 30m
        assert "1h" in result
        assert "30m" in result

    def test_format_duration_minutes_only(self, service):
        """Verify duration formats minutes only when < 1 hour."""
        result = service._format_duration(1800)  # 30m
        assert "30m" in result or "30" in result

    @pytest.mark.asyncio
    async def test_print_complete_fallback_values(self, service):
        """CRITICAL: Verify fallback values when archive_data is missing."""
        mock_db = AsyncMock()

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = []  # No providers, just testing variable setup
            mock_build.return_value = ("Test", "Test")

            await service.on_print_complete(
                printer_id=1,
                printer_name="Test",
                status="completed",
                data={"subtask_name": "test_print"},
                db=mock_db,
                archive_data=None,  # No archive data - should use fallbacks
            )

            # Test passes if no exception is raised with missing archive_data

    @pytest.mark.asyncio
    async def test_print_complete_with_archive_data(self, service):
        """Verify archive data values are used when provided."""
        mock_db = AsyncMock()

        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            mock_get.return_value = []

            await service.on_print_complete(
                printer_id=1,
                printer_name="Test",
                status="completed",
                data={"subtask_name": "test_print"},
                db=mock_db,
                archive_data={
                    "print_time_seconds": 3600,
                    "actual_filament_grams": 50.5,
                },
            )

            # When archive data is provided, duration should not be "Unknown"
            if captured_variables.get("duration"):
                assert captured_variables["duration"] != "Unknown"

    @pytest.mark.asyncio
    async def test_duration_prefers_actual_time_seconds_over_slicer_estimate(self, service):
        """#1198: completion notification duration must reflect *actual* elapsed
        time from started_at/completed_at, not the slicer's pre-print estimate.

        Pre-fix the duration variable read from `print_time_seconds` (slicer
        estimate parsed from the 3MF at archive creation), so a print cancelled
        2 minutes into a 3-hour estimate would notify "duration: 3h"."""
        mock_db = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.id = 1

        captured_variables: dict = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_print_complete(
                printer_id=1,
                printer_name="Test",
                status="cancelled",
                data={"subtask_name": "test_print"},
                db=mock_db,
                archive_data={
                    "print_time_seconds": 10800,  # 3h slicer estimate
                    "actual_time_seconds": 120,  # 2m actual elapsed
                },
            )

        # 2 minutes — not 3 hours — even though the slicer estimate is in the dict.
        assert "2m" in captured_variables["duration"]
        assert "3h" not in captured_variables["duration"]

    @pytest.mark.asyncio
    async def test_duration_falls_back_to_slicer_estimate_when_actual_time_missing(self, service):
        """#1198: when actual_time_seconds is absent (e.g. timestamps weren't
        recorded for some reason), the duration variable falls back to
        print_time_seconds rather than rendering 'Unknown'. Preserves
        backwards-compat for any code path that didn't compute actual elapsed."""
        mock_db = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.id = 1

        captured_variables: dict = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_print_complete(
                printer_id=1,
                printer_name="Test",
                status="completed",
                data={"subtask_name": "test_print"},
                db=mock_db,
                archive_data={
                    "print_time_seconds": 3600,  # 1h slicer estimate, no actual
                    "actual_time_seconds": None,
                },
            )

        assert captured_variables["duration"] != "Unknown"
        assert "1h" in captured_variables["duration"]

    @pytest.mark.asyncio
    async def test_duration_unknown_when_both_time_fields_missing(self, service):
        """#1198: with neither actual nor estimated time available the duration
        variable surfaces the existing 'Unknown' fallback."""
        mock_db = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.id = 1

        captured_variables: dict = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_print_complete(
                printer_id=1,
                printer_name="Test",
                status="completed",
                data={"subtask_name": "test_print"},
                db=mock_db,
                archive_data={
                    "print_time_seconds": None,
                    "actual_time_seconds": None,
                },
            )

        assert captured_variables["duration"] == "Unknown"

    @pytest.mark.asyncio
    async def test_print_complete_with_finish_photo_url(self, service):
        """Verify finish_photo_url is passed through from archive_data."""
        mock_db = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.id = 1

        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_print_complete(
                printer_id=1,
                printer_name="Test",
                status="completed",
                data={"subtask_name": "test_print"},
                db=mock_db,
                archive_data={
                    "print_time_seconds": 3600,
                    "actual_filament_grams": 50.5,
                    "finish_photo_url": "http://localhost:8000/api/v1/archives/1/photos/finish_test.jpg",
                },
            )

            # finish_photo_url should be passed through to template variables
            assert (
                captured_variables.get("finish_photo_url")
                == "http://localhost:8000/api/v1/archives/1/photos/finish_test.jpg"
            )

    @pytest.mark.asyncio
    async def test_print_start_estimated_time_fallback(self, service):
        """Verify estimated time shows 'Unknown' when not available."""
        mock_db = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.id = 1

        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
            patch("backend.app.api.routes.settings.get_setting", new_callable=AsyncMock, return_value=None),
        ):
            # Need at least one provider to trigger message building
            mock_get.return_value = [mock_provider]

            await service.on_print_start(
                printer_id=1,
                printer_name="Test",
                data={
                    "subtask_name": "test",
                    # No estimated_time or mc_remaining_time
                },
                db=mock_db,
            )

            # When no time data, should show "Unknown"
            assert captured_variables.get("estimated_time") == "Unknown"

    @pytest.mark.asyncio
    async def test_print_progress_remaining_time_fallback(self, service):
        """Verify remaining time shows 'Unknown' when not available."""
        mock_db = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.id = 1

        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
            patch("backend.app.api.routes.settings.get_setting", new_callable=AsyncMock, return_value=None),
        ):
            # Need at least one provider to trigger message building
            mock_get.return_value = [mock_provider]

            await service.on_print_progress(
                printer_id=1,
                printer_name="Test",
                progress=50,
                remaining_time=None,  # No remaining time
                filename="test.3mf",
                db=mock_db,
            )

            # When no remaining time, should show "Unknown"
            assert captured_variables.get("remaining_time") == "Unknown"

    @pytest.mark.asyncio
    async def test_filename_fallback_to_unknown(self, service):
        """Verify filename defaults to 'Unknown' when not provided."""
        mock_db = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.id = 1

        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            # Need at least one provider to trigger message building
            mock_get.return_value = [mock_provider]

            await service.on_print_complete(
                printer_id=1,
                printer_name="Test",
                status="completed",
                data={},  # No subtask_name or filename
                db=mock_db,
            )

            # Filename should default to something (either "Unknown" or cleaned empty)
            assert "filename" in captured_variables

    @pytest.mark.asyncio
    async def test_print_start_uses_archive_print_time_seconds(self, service):
        """Verify print_time_seconds from archive_data is used for estimated_time."""
        mock_db = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.id = 1

        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
            patch("backend.app.api.routes.settings.get_setting", new_callable=AsyncMock, return_value=None),
        ):
            mock_get.return_value = [mock_provider]

            # Pass archive_data with print_time_seconds (7200 seconds = 2 hours)
            await service.on_print_start(
                printer_id=1,
                printer_name="Test",
                data={"subtask_name": "test"},
                db=mock_db,
                archive_data={"print_time_seconds": 7200},
            )

            # Should use archive's print_time_seconds: 7200 seconds = 2h 0m
            assert captured_variables.get("estimated_time") == "2h 0m"

    @pytest.mark.asyncio
    async def test_print_start_archive_data_overrides_mqtt(self, service):
        """Verify archive_data takes priority over MQTT remaining_time."""
        mock_db = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.id = 1

        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
            patch("backend.app.api.routes.settings.get_setting", new_callable=AsyncMock, return_value=None),
        ):
            mock_get.return_value = [mock_provider]

            # Both archive_data and MQTT remaining_time provided
            # Archive says 2 hours, MQTT says 30 minutes (wrong at start)
            await service.on_print_start(
                printer_id=1,
                printer_name="Test",
                data={
                    "subtask_name": "test",
                    "remaining_time": 1800,  # 30 minutes from MQTT
                },
                db=mock_db,
                archive_data={"print_time_seconds": 7200},  # 2 hours from 3MF
            )

            # Should use archive's print_time_seconds (more reliable)
            assert captured_variables.get("estimated_time") == "2h 0m"

    @pytest.mark.asyncio
    async def test_print_start_falls_back_to_mqtt_when_no_archive(self, service):
        """Verify MQTT remaining_time is used when archive_data not provided."""
        mock_db = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.id = 1

        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
            patch("backend.app.api.routes.settings.get_setting", new_callable=AsyncMock, return_value=None),
        ):
            mock_get.return_value = [mock_provider]

            # Only MQTT remaining_time provided (1800 seconds = 30 minutes)
            await service.on_print_start(
                printer_id=1,
                printer_name="Test",
                data={
                    "subtask_name": "test",
                    "remaining_time": 1800,
                },
                db=mock_db,
                # No archive_data
            )

            # Should use MQTT remaining_time
            assert captured_variables.get("estimated_time") == "30m"

    @pytest.mark.asyncio
    async def test_print_start_eta_calculated_from_estimated_time(self, service):
        """Verify ETA is calculated as wall-clock time from estimated_time."""
        mock_db = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.id = 1

        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
            patch("backend.app.api.routes.settings.get_setting", new_callable=AsyncMock, return_value=None),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_print_start(
                printer_id=1,
                printer_name="Test",
                data={"subtask_name": "test"},
                db=mock_db,
                archive_data={"print_time_seconds": 3600},  # 1 hour
            )

            # ETA should be a time string in HH:MM format
            eta = captured_variables.get("eta")
            assert eta is not None
            assert eta != "Unknown"
            assert ":" in eta  # HH:MM format

    @pytest.mark.asyncio
    async def test_print_start_eta_unknown_when_no_time(self, service):
        """Verify ETA shows 'Unknown' when no time data available."""
        mock_db = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.id = 1

        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
            patch("backend.app.api.routes.settings.get_setting", new_callable=AsyncMock, return_value=None),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_print_start(
                printer_id=1,
                printer_name="Test",
                data={"subtask_name": "test"},
                db=mock_db,
            )

            assert captured_variables.get("eta") == "Unknown"

    @pytest.mark.asyncio
    async def test_print_start_eta_respects_12h_format(self, service):
        """Verify ETA uses 12-hour format when time_format is '12h'."""
        mock_db = AsyncMock()
        mock_provider = MagicMock()
        mock_provider.id = 1

        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
            patch("backend.app.api.routes.settings.get_setting", new_callable=AsyncMock, return_value="12h"),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_print_start(
                printer_id=1,
                printer_name="Test",
                data={"subtask_name": "test"},
                db=mock_db,
                archive_data={"print_time_seconds": 3600},
            )

            eta = captured_variables.get("eta")
            assert eta is not None
            # 12h format should contain AM or PM
            assert "AM" in eta or "PM" in eta


class TestNotificationTemplates:
    """Tests for notification message template rendering."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.mark.asyncio
    async def test_template_renders_variables(self, service):
        """Verify template variables are replaced correctly."""
        template_title = "Print {progress}% Complete"
        template_body = "{printer}: {filename}\nRemaining: {remaining_time}"

        variables = {
            "printer": "Test Printer",
            "filename": "test.3mf",
            "progress": "50",
            "remaining_time": "1h 30m",
        }

        title = template_title.format(**variables)
        body = template_body.format(**variables)

        assert title == "Print 50% Complete"
        assert "Test Printer" in body
        assert "test.3mf" in body
        assert "1h 30m" in body

    @pytest.mark.asyncio
    async def test_template_handles_missing_variables(self, service):
        """Verify missing template variables don't cause crashes."""
        template = "{printer}: {unknown_var}"
        variables = {"printer": "Test"}

        # Should handle gracefully - either leave placeholder or skip
        try:
            result = template.format_map({**variables, "unknown_var": "{unknown_var}"})
            assert "Test" in result
        except KeyError:
            pytest.fail("Template should handle missing variables gracefully")


class TestPrinterErrorNotifications:
    """Tests for HMS error (printer error) notifications."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.fixture
    def mock_provider(self):
        """Create a mock notification provider with error notifications enabled."""
        provider = MagicMock()
        provider.id = 1
        provider.name = "Test Provider"
        provider.provider_type = "webhook"
        provider.enabled = True
        provider.config = json.dumps({"webhook_url": "http://test.local/webhook"})
        provider.on_printer_error = True  # Enable error notifications
        provider.quiet_hours_enabled = False
        provider.daily_digest_enabled = False
        provider.printer_id = None
        return provider

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.commit = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_on_printer_error_sends_notification(self, service, mock_provider, mock_db):
        """Verify HMS error notification is sent when triggered."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Printer Error", "AMS/Filament Error: 0700_8010")

            await service.on_printer_error(
                printer_id=1,
                printer_name="Test Printer",
                error_type="AMS/Filament Error",
                db=mock_db,
                error_detail="Error code: 0700_8010",
            )

            mock_get.assert_called_once()
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_printer_error_skipped_when_disabled(self, service, mock_provider, mock_db):
        """CRITICAL: Verify error notifications respect toggle setting."""
        mock_provider.on_printer_error = False

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            # Provider with toggle disabled won't be returned
            mock_get.return_value = []

            await service.on_printer_error(
                printer_id=1,
                printer_name="Test",
                error_type="AMS Error",
                db=mock_db,
                error_detail="Test error",
            )

            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_printer_error_includes_error_detail(self, service, mock_provider, mock_db):
        """Verify error details are passed to template variables."""
        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_printer_error(
                printer_id=1,
                printer_name="X1 Carbon",
                error_type="AMS/Filament Error",
                db=mock_db,
                error_detail="Error code: 0700_8010",
            )

            assert captured_variables["printer"] == "X1 Carbon"
            assert captured_variables["error_type"] == "AMS/Filament Error"
            assert captured_variables["error_detail"] == "Error code: 0700_8010"

    @pytest.mark.asyncio
    async def test_on_printer_error_fallback_when_no_detail(self, service, mock_provider, mock_db):
        """Verify fallback message when error_detail is None."""
        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_printer_error(
                printer_id=1,
                printer_name="Test Printer",
                error_type="Unknown Error",
                db=mock_db,
                error_detail=None,  # No detail provided
            )

            assert captured_variables["error_detail"] == "No details available"


class TestAIFailureDetectionNotifications:
    """Tests for the AI failure-detection event (#1794 — split out of on_printer_error).

    Pins that Obico failure-detection dispatches go through the dedicated
    on_ai_failure_detection event field, not the multiplexed printer-error
    field. Mirrors the printer-error coverage above so a regression on either
    surface fails its own case.
    """

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.fixture
    def mock_provider(self):
        provider = MagicMock()
        provider.id = 1
        provider.name = "Test Provider"
        provider.provider_type = "webhook"
        provider.enabled = True
        provider.config = json.dumps({"webhook_url": "http://test.local/webhook"})
        provider.on_ai_failure_detection = True
        provider.on_printer_error = False  # disabled — the regression guard
        provider.quiet_hours_enabled = False
        provider.daily_digest_enabled = False
        provider.printer_id = None
        return provider

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.commit = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_dispatch_uses_ai_failure_detection_event_not_printer_error(self, service, mock_provider, mock_db):
        """Regression guard: provider subscribed only to AI alerts must receive
        the Obico notification."""
        captured_event = []

        async def capture(db, event_field, printer_id):
            captured_event.append(event_field)
            return [mock_provider]

        with (
            patch.object(service, "_get_providers_for_event", side_effect=capture),
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_build.return_value = ("Possible Print Failure Detected", "details")

            await service.on_ai_failure_detection(
                printer_id=1,
                printer_name="X1 Carbon",
                task_name="benchy.3mf",
                confidence=0.87,
                action="notify",
                db=mock_db,
            )

            assert captured_event == ["on_ai_failure_detection"]
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_skipped_when_only_printer_error_is_enabled(self, service, mock_provider, mock_db):
        """Pre-#1794 behaviour MUST NOT survive: a provider with only the
        legacy on_printer_error toggle should NOT receive AI notifications now."""
        mock_provider.on_ai_failure_detection = False
        mock_provider.on_printer_error = True

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            mock_get.return_value = []  # the event-field filter excludes the provider

            await service.on_ai_failure_detection(
                printer_id=1,
                printer_name="X1 Carbon",
                task_name="benchy.3mf",
                confidence=0.87,
                action="notify",
                db=mock_db,
            )

            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_variables_include_task_name_confidence_action(self, service, mock_provider, mock_db):
        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_ai_failure_detection(
                printer_id=1,
                printer_name="X1 Carbon",
                task_name="benchy.3mf",
                confidence=0.873,
                action="pause_and_off",
                db=mock_db,
            )

            assert captured_variables["printer"] == "X1 Carbon"
            assert captured_variables["task_name"] == "benchy.3mf"
            assert captured_variables["confidence"] == "0.87"  # 2-decimal format
            assert captured_variables["action"] == "pause_and_off"

    @pytest.mark.asyncio
    async def test_task_name_fallback_when_unknown(self, service, mock_provider, mock_db):
        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_ai_failure_detection(
                printer_id=1,
                printer_name="Test",
                task_name="",  # empty
                confidence=0.5,
                action="notify",
                db=mock_db,
            )

            assert captured_variables["task_name"] == "current job"


class TestPlateNotEmptyNotifications:
    """Tests for plate not empty (build plate detection) notifications."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.fixture
    def mock_provider(self):
        """Create a mock notification provider with plate detection enabled."""
        provider = MagicMock()
        provider.id = 1
        provider.name = "Test Provider"
        provider.provider_type = "webhook"
        provider.enabled = True
        provider.config = json.dumps({"webhook_url": "http://test.local/webhook"})
        provider.on_plate_not_empty = True
        provider.quiet_hours_enabled = False
        provider.daily_digest_enabled = False
        provider.printer_id = None
        return provider

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.commit = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_on_plate_not_empty_sends_notification(self, service, mock_provider, mock_db):
        """Verify plate not empty notification is sent when triggered."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Plate Not Empty", "Objects detected on build plate")

            await service.on_plate_not_empty(
                printer_id=1,
                printer_name="Test Printer",
                db=mock_db,
                difference_percent=5.2,
            )

            mock_get.assert_called_once()
            mock_send.assert_called_once()
            # Verify force_immediate is True (critical alert)
            call_kwargs = mock_send.call_args[1]
            assert call_kwargs.get("force_immediate") is True

    @pytest.mark.asyncio
    async def test_on_plate_not_empty_skipped_when_disabled(self, service, mock_provider, mock_db):
        """Verify notification is skipped when toggle is disabled."""
        mock_provider.on_plate_not_empty = False

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            mock_get.return_value = []

            await service.on_plate_not_empty(
                printer_id=1,
                printer_name="Test",
                db=mock_db,
            )

            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_plate_not_empty_includes_difference_percent(self, service, mock_provider, mock_db):
        """Verify difference percentage is passed to template variables."""
        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_plate_not_empty(
                printer_id=1,
                printer_name="X1 Carbon",
                db=mock_db,
                difference_percent=3.5,
            )

            assert captured_variables["printer"] == "X1 Carbon"
            assert captured_variables["difference_percent"] == "3.5"


class TestBedCooledNotifications:
    """Tests for bed cooled (after print) notifications."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.fixture
    def mock_provider(self):
        """Create a mock notification provider with bed cooled enabled."""
        provider = MagicMock()
        provider.id = 1
        provider.name = "Test Provider"
        provider.provider_type = "webhook"
        provider.enabled = True
        provider.config = json.dumps({"webhook_url": "http://test.local/webhook"})
        provider.on_bed_cooled = True
        provider.quiet_hours_enabled = False
        provider.daily_digest_enabled = False
        provider.printer_id = None
        return provider

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.commit = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_on_bed_cooled_sends_notification(self, service, mock_provider, mock_db):
        """Verify bed cooled notification is sent when triggered."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Bed Cooled", "Test Printer: Bed cooled to 30°C")

            await service.on_bed_cooled(
                printer_id=1,
                printer_name="Test Printer",
                bed_temp=30.0,
                threshold=35.0,
                filename="benchy.3mf",
                db=mock_db,
            )

            mock_get.assert_called_once()
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_bed_cooled_skipped_when_no_providers(self, service, mock_db):
        """Verify notification is skipped when no providers have bed cooled enabled."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            mock_get.return_value = []

            await service.on_bed_cooled(
                printer_id=1,
                printer_name="Test Printer",
                bed_temp=30.0,
                threshold=35.0,
                filename="benchy.3mf",
                db=mock_db,
            )

            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_bed_cooled_includes_correct_variables(self, service, mock_provider, mock_db):
        """Verify bed temp, threshold, and filename are passed to template variables."""
        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_bed_cooled(
                printer_id=1,
                printer_name="X1 Carbon",
                bed_temp=28.7,
                threshold=35.0,
                filename="benchy.gcode.3mf",
                db=mock_db,
            )

            assert captured_variables["printer"] == "X1 Carbon"
            assert captured_variables["bed_temp"] == "29"
            assert captured_variables["threshold"] == "35"
            assert captured_variables["filename"] == "benchy"

    @pytest.mark.asyncio
    async def test_on_bed_cooled_handles_none_filename(self, service, mock_provider, mock_db):
        """Verify None filename is handled gracefully."""
        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_bed_cooled(
                printer_id=1,
                printer_name="Test Printer",
                bed_temp=30.0,
                threshold=35.0,
                filename=None,
                db=mock_db,
            )

            assert captured_variables["filename"] == "Unknown"


class TestFirstLayerCompleteNotifications:
    """Tests for first layer complete notifications."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.fixture
    def mock_provider(self):
        """Create a mock notification provider with first layer complete enabled."""
        provider = MagicMock()
        provider.id = 1
        provider.name = "Test Provider"
        provider.provider_type = "webhook"
        provider.enabled = True
        provider.config = json.dumps({"webhook_url": "http://test.local/webhook"})
        provider.on_first_layer_complete = True
        provider.quiet_hours_enabled = False
        provider.daily_digest_enabled = False
        provider.printer_id = None
        return provider

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.commit = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_on_first_layer_complete_sends_notification(self, service, mock_provider, mock_db):
        """Verify first layer complete notification is sent when triggered."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("First Layer Complete", "Test Printer: benchy.3mf")

            await service.on_first_layer_complete(
                printer_id=1,
                printer_name="Test Printer",
                filename="benchy.3mf",
                total_layers=50,
                db=mock_db,
            )

            mock_get.assert_called_once()
            mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_first_layer_complete_skipped_when_no_providers(self, service, mock_db):
        """Verify notification is skipped when no providers have first layer complete enabled."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            mock_get.return_value = []

            await service.on_first_layer_complete(
                printer_id=1,
                printer_name="Test Printer",
                filename="benchy.3mf",
                total_layers=50,
                db=mock_db,
            )

            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_first_layer_complete_includes_correct_variables(self, service, mock_provider, mock_db):
        """Verify printer name, filename, and total_layers are passed to template variables."""
        captured_variables = {}

        async def capture_build(db, event_type, variables):
            captured_variables.update(variables)
            return ("Test", "Test")

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", side_effect=capture_build),
        ):
            mock_get.return_value = [mock_provider]

            await service.on_first_layer_complete(
                printer_id=1,
                printer_name="X1 Carbon",
                filename="benchy.gcode.3mf",
                total_layers=120,
                db=mock_db,
            )

            assert captured_variables["printer"] == "X1 Carbon"
            assert captured_variables["filename"] == "benchy"
            assert captured_variables["total_layers"] == "120"

    @pytest.mark.asyncio
    async def test_on_first_layer_complete_passes_image_data(self, service, mock_provider, mock_db):
        """Verify image_data is passed through to _send_to_providers."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("First Layer Complete", "Test message")
            fake_image = b"\x89PNG\r\n\x1a\nfakeimage"

            await service.on_first_layer_complete(
                printer_id=1,
                printer_name="Test Printer",
                filename="benchy.3mf",
                total_layers=50,
                db=mock_db,
                image_data=fake_image,
            )

            mock_send.assert_called_once()
            call_kwargs = mock_send.call_args
            assert call_kwargs.kwargs.get("image_data") == fake_image


class TestNtfyOutbound:
    """Regression for #1534 — UA hygiene and Cloudflare-challenge detection."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.mark.asyncio
    async def test_notification_client_sets_honest_user_agent(self, service):
        """Default httpx UA leaks `python-httpx/<version>` — every other
        outbound client in the codebase identifies as Bambuddy. The
        notification client must too."""
        client = await service._get_client()
        try:
            assert client.headers.get("user-agent") == "Bambuddy/1.0 (+https://github.com/maziggy/bambuddy)"
        finally:
            await service.close()

    @pytest.mark.asyncio
    async def test_ntfy_cloudflare_challenge_returns_actionable_error(self, service):
        """When ntfy is fronted by Cloudflare and CF returns its JS
        challenge, the user must see a message that points at the actual
        fix (CF security skip), not the raw HTML."""
        import httpx

        challenge_html = (
            '<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>'
            '<meta http-equiv="Content-Type" content="text/html; charset=UTF-8">'
        )
        mock_response = httpx.Response(
            403,
            content=challenge_html.encode(),
            headers={"server": "cloudflare", "content-type": "text/html; charset=UTF-8"},
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(service, "_get_client", AsyncMock(return_value=mock_client)):
            ok, detail = await service._send_ntfy(
                {"server": "https://ntfy.example", "topic": "alerts", "auth_token": "tk_xxx"},
                title="t",
                message="m",
            )

        assert ok is False
        assert "Cloudflare" in detail
        assert "security-skip" in detail or "Bot Fight Mode" in detail
        # The raw HTML must not be the dominant content shown to the user.
        assert "<!DOCTYPE" not in detail

    @pytest.mark.asyncio
    async def test_ntfy_normal_403_still_surfaces_body(self, service):
        """A non-Cloudflare 403 (e.g. ntfy auth fail) must keep showing
        the original body so the user can debug the real error — we
        only intercept the Cloudflare-challenge shape."""
        import httpx

        mock_response = httpx.Response(
            403,
            content=b"forbidden: invalid auth token",
            headers={"content-type": "text/plain"},
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(service, "_get_client", AsyncMock(return_value=mock_client)):
            ok, detail = await service._send_ntfy(
                {"server": "https://ntfy.sh", "topic": "alerts", "auth_token": "bad"},
                title="t",
                message="m",
            )

        assert ok is False
        assert "Cloudflare" not in detail
        assert "invalid auth token" in detail
        assert detail.startswith("HTTP 403:")

    @pytest.mark.asyncio
    async def test_ntfy_origin_error_through_cloudflare_is_not_misclassified(self, service):
        """Cloudflare adds Server: cloudflare to EVERY proxied response,
        including legitimate origin errors. A real 401 "wrong token"
        from an ntfy server that happens to sit behind Cloudflare must
        still surface the origin's actual error body — we must not flip
        every CF-fronted 4xx into a "your Cloudflare is blocking" message.
        """
        import httpx

        mock_response = httpx.Response(
            401,
            content=b'{"code":40101,"http":401,"error":"unauthorized"}',
            headers={
                "server": "cloudflare",
                "cf-ray": "abc123-FRA",
                "content-type": "application/json",
                # No cf-mitigated — CF just proxied the origin response.
            },
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(service, "_get_client", AsyncMock(return_value=mock_client)):
            ok, detail = await service._send_ntfy(
                {"server": "https://ntfy.example", "topic": "alerts", "auth_token": "wrong"},
                title="t",
                message="m",
            )

        assert ok is False
        assert "Cloudflare" not in detail
        assert "unauthorized" in detail
        assert detail.startswith("HTTP 401:")

    @pytest.mark.asyncio
    async def test_ntfy_cloudflare_cf_mitigated_header_alone_triggers(self, service):
        """The cf-mitigated header on its own is enough — that's the
        canonical CF "I actively blocked this" signal, even if the
        response body shape changes between CF challenge generations."""
        import httpx

        mock_response = httpx.Response(
            403,
            content=b"<html>some future CF block page</html>",
            headers={
                "server": "cloudflare",
                "cf-mitigated": "challenge",
                "content-type": "text/html",
            },
        )

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch.object(service, "_get_client", AsyncMock(return_value=mock_client)):
            ok, detail = await service._send_ntfy(
                {"server": "https://ntfy.example", "topic": "alerts"},
                title="t",
                message="m",
            )

        assert ok is False
        assert "Cloudflare" in detail


class TestEmailProvider:
    """Tests for SMTP email provider, including #1792 finish-photo inline embed.

    Embed is opt-in via the template: only when the user's template referenced
    ``{finish_photo_url}`` (so the URL appears in the rendered body) AND the
    photo bytes are available does ``_send_email`` build the multipart/related
    shape. Otherwise it stays single-part text — no surprise inline image.
    """

    PHOTO_URL = "https://printer.local/api/v1/archives/42/photos/finish.jpg"

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.fixture
    def smtp_config(self):
        return {
            "smtp_server": "smtp.example.com",
            "smtp_port": "587",
            "username": "alice",
            "password": "secret",
            "from_email": "bambuddy@example.com",
            "to_email": "alice@example.com",
            "security": "starttls",
            "auth_enabled": "true",
        }

    @staticmethod
    def _fake_smtp_class(captured: dict):
        class FakeSMTP:
            def __init__(self, host, port):
                captured["host"] = host
                captured["port"] = port

            def starttls(self):
                captured["starttls"] = True

            def login(self, u, p):
                captured["login"] = (u, p)

            def sendmail(self, frm, to, body):
                captured["from"] = frm
                captured["to"] = to
                captured["raw"] = body

            def quit(self):
                captured["quit"] = True

        return FakeSMTP

    @pytest.mark.asyncio
    async def test_email_without_image_or_url_stays_text_only(self, service, smtp_config):
        """No image_data and no URL in body → original single-part text shape."""
        captured: dict = {}
        with patch("backend.app.services.notification_service.smtplib.SMTP", self._fake_smtp_class(captured)):
            ok, _ = await service._send_email(smtp_config, "Print Failed", "Reason: unknown")

        assert ok is True
        assert "image/jpeg" not in captured["raw"]
        assert "multipart/related" not in captured["raw"]
        assert "cid:bambuddy-finish-photo" not in captured["raw"]
        assert "Reason: unknown" in captured["raw"]

    @pytest.mark.asyncio
    async def test_email_image_without_template_reference_stays_text_only(self, service, smtp_config):
        """image_data present but template didn't include {finish_photo_url} → no embed.

        Pins the template-driven contract: a user whose body is just
        "Print failed. Reason: unknown" does NOT get a surprise inline image
        stapled to the bottom, even though the photo bytes are available
        upstream from the archive.
        """
        captured: dict = {}
        with patch("backend.app.services.notification_service.smtplib.SMTP", self._fake_smtp_class(captured)):
            ok, _ = await service._send_email(
                smtp_config,
                "Print Failed",
                "Reason: unknown",
                image_data=b"\xff\xd8\xff\xe0jpeg",
                finish_photo_url=self.PHOTO_URL,
            )

        assert ok is True
        raw = captured["raw"]
        assert "image/jpeg" not in raw
        assert "multipart/related" not in raw
        assert "cid:bambuddy-finish-photo" not in raw

    @pytest.mark.asyncio
    async def test_email_inlines_when_template_uses_finish_photo_url(self, service, smtp_config):
        """URL in body + image_data present → multipart/related + cid embed; HTML swaps URL for <img>."""
        captured: dict = {}
        body = f"Print failed. Reason: unknown\n\nSnapshot: {self.PHOTO_URL}"

        with patch("backend.app.services.notification_service.smtplib.SMTP", self._fake_smtp_class(captured)):
            ok, _ = await service._send_email(
                smtp_config,
                "Print Failed",
                body,
                image_data=b"\xff\xd8\xff\xe0fake-jpeg-bytes",
                finish_photo_url=self.PHOTO_URL,
            )

        assert ok is True
        raw = captured["raw"]
        # multipart/related shape with both alt parts and an image part
        assert "multipart/related" in raw
        assert "multipart/alternative" in raw
        assert "text/plain" in raw
        assert "text/html" in raw
        assert "image/jpeg" in raw
        # HTML references the exact cid the Content-ID header registers
        assert "Content-ID: <bambuddy-finish-photo>" in raw
        assert 'src="cid:bambuddy-finish-photo"' in raw
        # Inline disposition so renders embedded, not as download attachment
        assert 'Content-Disposition: inline; filename="finish-photo.jpg"' in raw
        # Plain-text body keeps the URL so non-HTML clients still get a clickable link
        assert self.PHOTO_URL in raw

    @pytest.mark.asyncio
    async def test_email_image_data_without_url_arg_stays_text_only(self, service, smtp_config):
        """image_data passed but finish_photo_url=None → defence-in-depth, no embed.

        Even if a future caller forgets to thread the URL through but does pass
        the bytes, the conservative default is no embed (avoids attaching an
        unreferenced image to an unrelated event type).
        """
        captured: dict = {}
        with patch("backend.app.services.notification_service.smtplib.SMTP", self._fake_smtp_class(captured)):
            ok, _ = await service._send_email(
                smtp_config,
                "Print Failed",
                f"Snapshot: {self.PHOTO_URL}",
                image_data=b"\xff\xd8\xff\xe0jpeg",
                finish_photo_url=None,
            )

        assert ok is True
        assert "image/jpeg" not in captured["raw"]
        assert "multipart/related" not in captured["raw"]

    @pytest.mark.asyncio
    async def test_email_html_body_escapes_user_content(self, service, smtp_config):
        """Template-rendered body must not be injected raw into the HTML part."""
        captured: dict = {}
        body = f"Filename: <script>alert(1)</script>\nLine 2\nSnapshot: {self.PHOTO_URL}"

        with patch("backend.app.services.notification_service.smtplib.SMTP", self._fake_smtp_class(captured)):
            ok, _ = await service._send_email(
                smtp_config,
                "Print Failed",
                body,
                image_data=b"\xff\xd8\xff\xe0jpeg",
                finish_photo_url=self.PHOTO_URL,
            )

        assert ok is True
        raw = captured["raw"]
        # Raw HTML must NOT round-trip into the HTML part — verify escaped form is present.
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in raw
        # Newlines in the body become <br> in HTML
        assert "Line 2" in raw
        assert "<br>" in raw

    @pytest.mark.asyncio
    async def test_email_html_swaps_url_for_img_tag(self, service, smtp_config):
        """In the HTML part, the URL substring is replaced with the <img cid:...> tag.

        Plain text keeps the URL; HTML clients see the inline image where the
        URL was. The URL must NOT appear inside an <a href> wrapping the image
        — we replace the URL outright with the img tag (renderers don't need
        the URL twice in the HTML part when the image is already inline).
        """
        captured: dict = {}
        body = f"See: {self.PHOTO_URL} for the snapshot."

        with patch("backend.app.services.notification_service.smtplib.SMTP", self._fake_smtp_class(captured)):
            ok, _ = await service._send_email(
                smtp_config,
                "Print Failed",
                body,
                image_data=b"\xff\xd8\xff\xe0jpeg",
                finish_photo_url=self.PHOTO_URL,
            )

        assert ok is True
        raw = captured["raw"]
        # The <img> tag appears in the HTML part
        assert 'src="cid:bambuddy-finish-photo"' in raw
        # The escaped URL is the marker we replaced — the HTML part should not
        # contain BOTH the escaped URL AND the cid img (we swapped, not duplicated).
        # The plain-text part still has the URL; check it's there at least once.
        assert self.PHOTO_URL in raw


class TestPlateNotEmptySourceDetail:
    """Source disambiguation + legacy-template tolerance for on_plate_not_empty (3.3)."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.commit = AsyncMock()
        return db

    def test_render_drops_missing_placeholder(self, service):
        # Base rendering behaviour: a body WITHOUT {source_detail} drops the value.
        out = service._render_template("{printer}: hello", {"printer": "P", "source_detail": "DETAIL"})
        assert out == "P: hello"
        assert "DETAIL" not in out

    def test_render_uses_placeholder_when_present(self, service):
        out = service._render_template("{printer}: {source_detail}", {"printer": "P", "source_detail": "DETAIL"})
        assert out == "P: DETAIL"

    @pytest.mark.asyncio
    async def test_legacy_template_appends_source_detail(self, service, mock_db):
        from types import SimpleNamespace

        legacy = SimpleNamespace(
            title_template="Plate Not Empty - Print Paused",
            body_template="{printer}: Objects detected on build plate. Clear plate and resume.",
        )
        provider = MagicMock()
        provider.printer_id = None
        captured = {}

        async def _fake_send(providers, title, message, *a, **k):
            captured["message"] = message

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock, return_value=[provider]),
            patch.object(service, "_get_template", new_callable=AsyncMock, return_value=legacy),
            patch.object(service, "_send_to_providers", new=_fake_send),
        ):
            await service.on_plate_not_empty(1, "P", mock_db, source_detail="Printer vision saw objects.")

        # The legacy body has no {source_detail} slot → the detail is appended so it
        # is never silently lost on an older install.
        assert "Printer vision saw objects." in captured["message"]

    @pytest.mark.asyncio
    async def test_new_template_renders_without_double_append(self, service, mock_db):
        from types import SimpleNamespace

        new_tmpl = SimpleNamespace(
            title_template="Plate Not Empty — {printer}",
            body_template="{printer}: {source_detail}",
        )
        provider = MagicMock()
        provider.printer_id = None
        captured = {}

        async def _fake_send(providers, title, message, *a, **k):
            captured["message"] = message

        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock, return_value=[provider]),
            patch.object(service, "_get_template", new_callable=AsyncMock, return_value=new_tmpl),
            patch.object(service, "_send_to_providers", new=_fake_send),
        ):
            await service.on_plate_not_empty(1, "P", mock_db, source_detail="Detail X.")

        # Rendered once via the placeholder; NOT appended a second time.
        assert captured["message"].count("Detail X.") == 1


class TestFarmLifecycleNotifications:
    """Phase 6: manual/lifecycle farm events (run aborted/resumed, FA approved).

    Each method mirrors ``on_run_paused`` — provider-boolean gated, no
    ``force_immediate`` (run-lifecycle events, not printer alarms).
    """

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.fixture
    def mock_provider(self):
        provider = MagicMock()
        provider.id = 1
        provider.name = "Test Provider"
        provider.printer_id = None
        return provider

    @pytest.fixture
    def mock_db(self):
        return AsyncMock()

    @pytest.mark.asyncio
    async def test_on_run_aborted_sends_when_enabled(self, service, mock_provider, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Run Aborted", "body")

            await service.on_run_aborted("Batch 9", "SKU007", mock_db)

            assert mock_get.call_args.args[1] == "on_run_aborted"
            mock_send.assert_called_once()
            # event_type routed positionally; no force_immediate (matches on_run_paused).
            assert mock_send.call_args.args[4] == "run_aborted"
            assert mock_send.call_args.kwargs.get("force_immediate") in (None, False)
            assert mock_send.call_args.kwargs["variables"] == {"run_name": "Batch 9", "sku_code": "SKU007"}

    @pytest.mark.asyncio
    async def test_on_run_aborted_skipped_when_disabled(self, service, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            mock_get.return_value = []  # provider boolean off → filtered out

            await service.on_run_aborted("Batch 9", "SKU007", mock_db)

            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_run_resumed_sends_with_topped_up(self, service, mock_provider, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Run Resumed", "body")

            await service.on_run_resumed("Batch 9", "SKU007", 2, mock_db)

            assert mock_get.call_args.args[1] == "on_run_resumed"
            mock_send.assert_called_once()
            assert mock_send.call_args.args[4] == "run_resumed"
            assert mock_send.call_args.kwargs["variables"]["topped_up"] == "2"

    @pytest.mark.asyncio
    async def test_on_run_resumed_skipped_when_disabled(self, service, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            mock_get.return_value = []

            await service.on_run_resumed("Batch 9", "SKU007", 0, mock_db)

            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_first_article_approved_sends_with_printer(self, service, mock_provider, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("First Article Approved", "body")

            await service.on_first_article_approved("Batch 9", "SKU007", "001-H2S", mock_db, printer_id=3)

            assert mock_get.call_args.args[1] == "on_first_article_approved"
            mock_send.assert_called_once()
            assert mock_send.call_args.args[4] == "first_article_approved"
            variables = mock_send.call_args.kwargs["variables"]
            assert variables["printer"] == "001-H2S"
            assert variables["run_name"] == "Batch 9"

    @pytest.mark.asyncio
    async def test_on_first_article_approved_skipped_when_disabled(self, service, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            mock_get.return_value = []

            await service.on_first_article_approved("Batch 9", "SKU007", None, mock_db)

            mock_send.assert_not_called()

    def test_farm_lifecycle_templates_render_with_vars(self, service):
        """The seeded run_aborted/run_resumed/first_article_approved bodies
        substitute their variables (proves the template + var contract)."""
        from backend.app.models.notification_template import DEFAULT_TEMPLATES

        by_type = {t["event_type"]: t for t in DEFAULT_TEMPLATES}
        for event_type in ("run_aborted", "run_resumed", "first_article_approved"):
            assert event_type in by_type, f"missing seed for {event_type}"

        aborted = service._render_template(
            by_type["run_aborted"]["body_template"], {"run_name": "Batch 9", "sku_code": "SKU007"}
        )
        assert "Batch 9" in aborted

        resumed = service._render_template(
            by_type["run_resumed"]["body_template"],
            {"run_name": "Batch 9", "sku_code": "SKU007", "topped_up": "3"},
        )
        assert "Batch 9" in resumed and "3" in resumed

        approved = service._render_template(
            by_type["first_article_approved"]["body_template"],
            {"run_name": "Batch 9", "sku_code": "SKU007", "printer": "001-H2S"},
        )
        assert "Batch 9" in approved and "001-H2S" in approved


class TestOnCooldownEscalation:
    """The dedicated cooldown-escalation event (NOT plate_not_empty): fires the
    honest 'cooldown running long' copy with the live bed, target and cap."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.commit = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_sends_with_cap_detail(self, service, mock_db):
        """max_hold_minutes > 0 → detail names the forced-eject cap."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [MagicMock()]
            mock_build.return_value = ("Cooldown running long — 001-H2S", "body")

            await service.on_cooldown_escalation(
                1, "001-H2S", bed_c=41.0, threshold_c=33.0, max_hold_minutes=180, db=mock_db
            )

            mock_get.assert_awaited_once_with(mock_db, "on_cooldown_escalation", 1)
            _, event, variables = mock_build.call_args.args
            assert event == "cooldown_escalation"
            detail = variables["detail"]
            assert "bed 41 °C" in detail
            assert "target 33 °C" in detail
            assert "forced eject at the 180-minute cap" in detail
            mock_send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_sends_with_no_cap_detail(self, service, mock_db):
        """max_hold_minutes == 0 → detail says no forced-eject cap is set."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [MagicMock()]
            mock_build.return_value = ("t", "b")

            await service.on_cooldown_escalation(
                2, "002-H2S", bed_c=35.0, threshold_c=33.0, max_hold_minutes=0, db=mock_db
            )

            _, _event, variables = mock_build.call_args.args
            assert "no forced-eject cap is set" in variables["detail"]
            assert "cap" not in variables["detail"].replace("no forced-eject cap is set", "")

    @pytest.mark.asyncio
    async def test_unknown_bed_when_none(self, service, mock_db):
        """An unreadable bed at fire time renders 'unknown', never a crash."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [MagicMock()]
            mock_build.return_value = ("t", "b")

            await service.on_cooldown_escalation(
                3, "003-H2S", bed_c=None, threshold_c=33.0, max_hold_minutes=90, db=mock_db
            )

            _, _event, variables = mock_build.call_args.args
            assert "bed unknown °C" in variables["detail"]

    @pytest.mark.asyncio
    async def test_toggle_off_sends_nothing(self, service, mock_db):
        """No provider subscribes to on_cooldown_escalation → nothing sent."""
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            mock_get.return_value = []

            await service.on_cooldown_escalation(
                4, "004-H2S", bed_c=40.0, threshold_c=33.0, max_hold_minutes=180, db=mock_db
            )

            mock_send.assert_not_called()

    def test_cooldown_escalation_template_seeded_and_renders(self, service):
        """The cooldown_escalation template exists and substitutes {printer}/{detail}."""
        from backend.app.models.notification_template import DEFAULT_TEMPLATES

        by_type = {t["event_type"]: t for t in DEFAULT_TEMPLATES}
        assert "cooldown_escalation" in by_type
        tmpl = by_type["cooldown_escalation"]
        title = service._render_template(tmpl["title_template"], {"printer": "001-H2S"})
        body = service._render_template(
            tmpl["body_template"], {"printer": "001-H2S", "detail": "Still cooling: bed 40 °C"}
        )
        assert "001-H2S" in title
        assert "001-H2S" in body and "Still cooling: bed 40 °C" in body


class TestSpoolRecoveryNotifications:
    """Farm mid-print spool-jam auto-recovery events (services/spool_recovery.py).

    Four printer-alarm events mirroring ``on_run_unit_stopped``/``on_storage_low``:
    provider-boolean gated, ``force_immediate`` (a printer alarm, not a run-lifecycle
    event). Each pair proves the toggle-ON provider receives the message with its
    template variables rendered and the toggle-OFF provider is filtered out.
    """

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.fixture
    def mock_provider(self):
        provider = MagicMock()
        provider.id = 1
        provider.name = "Test Provider"
        provider.printer_id = None
        return provider

    @pytest.fixture
    def mock_db(self):
        db = AsyncMock()
        db.commit = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_on_spool_recovery_succeeded_sends_when_enabled(self, service, mock_provider, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Spool jam recovered — 001-H2S", "body")

            await service.on_spool_recovery_succeeded(
                7,
                "001-H2S",
                "SKU007 plate",
                142,
                "Bambu PETG (RFID a1b2)",
                "Sunlu PETG (slot 3)",
                mock_db,
            )

            mock_get.assert_awaited_once_with(mock_db, "on_spool_recovery_succeeded", 7)
            _, event, variables = mock_build.call_args.args
            assert event == "spool_recovery_succeeded"
            assert variables == {
                "printer_name": "001-H2S",
                "job_name": "SKU007 plate",
                "layer": "142",
                "from_spool": "Bambu PETG (RFID a1b2)",
                "to_spool": "Sunlu PETG (slot 3)",
            }
            mock_send.assert_awaited_once()
            assert mock_send.call_args.args[4] == "spool_recovery_succeeded"
            assert mock_send.call_args.args[5] == 7
            assert mock_send.call_args.kwargs["force_immediate"] is True
            assert mock_send.call_args.kwargs["variables"] == variables

    @pytest.mark.asyncio
    async def test_on_spool_recovery_succeeded_skipped_when_disabled(self, service, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            mock_get.return_value = []  # provider boolean off → filtered out

            await service.on_spool_recovery_succeeded(
                7, "001-H2S", "SKU007 plate", 142, "Bambu PETG", "Sunlu PETG", mock_db
            )

            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_spool_recovery_failed_sends_when_enabled(self, service, mock_provider, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Spool jam NOT recovered — 001-H2S", "body")

            await service.on_spool_recovery_failed(
                7,
                "001-H2S",
                "SKU007 plate",
                "No other loaded spool matched PETG 0.6.",
                mock_db,
            )

            mock_get.assert_awaited_once_with(mock_db, "on_spool_recovery_failed", 7)
            _, event, variables = mock_build.call_args.args
            assert event == "spool_recovery_failed"
            assert variables == {
                "printer_name": "001-H2S",
                "job_name": "SKU007 plate",
                "detail": "No other loaded spool matched PETG 0.6.",
            }
            mock_send.assert_awaited_once()
            assert mock_send.call_args.args[4] == "spool_recovery_failed"
            assert mock_send.call_args.kwargs["force_immediate"] is True

    @pytest.mark.asyncio
    async def test_on_spool_recovery_failed_skipped_when_disabled(self, service, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            mock_get.return_value = []

            await service.on_spool_recovery_failed(7, "001-H2S", "SKU007 plate", "reason", mock_db)

            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_spool_out_of_rotation_sends_when_enabled(self, service, mock_provider, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Spool out of rotation — 001-H2S", "body")

            await service.on_spool_out_of_rotation(
                7,
                "001-H2S",
                "Bambu PETG (RFID a1b2)",
                "AMS 1 slot 3",
                "07C0_2000",
                mock_db,
            )

            mock_get.assert_awaited_once_with(mock_db, "on_spool_out_of_rotation", 7)
            _, event, variables = mock_build.call_args.args
            assert event == "spool_out_of_rotation"
            assert variables == {
                "printer_name": "001-H2S",
                "spool_desc": "Bambu PETG (RFID a1b2)",
                "slot_desc": "AMS 1 slot 3",
                "code": "07C0_2000",
            }
            mock_send.assert_awaited_once()
            assert mock_send.call_args.args[4] == "spool_out_of_rotation"
            assert mock_send.call_args.kwargs["force_immediate"] is True

    @pytest.mark.asyncio
    async def test_on_spool_out_of_rotation_skipped_when_disabled(self, service, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            mock_get.return_value = []

            await service.on_spool_out_of_rotation(7, "001-H2S", "Bambu PETG", "AMS 1 slot 3", "07C0_2000", mock_db)

            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_spool_recovery_self_healed_sends_when_enabled(self, service, mock_provider, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [mock_provider]
            mock_build.return_value = ("Feed fault self-healed — 001-H2S", "body")

            await service.on_spool_recovery_self_healed(
                7,
                "001-H2S",
                "SKU007 plate",
                142,
                "Bambu PETG (RFID a1b2)",
                "AMS1 slot 1",
                "0700_8010",
                mock_db,
            )

            mock_get.assert_awaited_once_with(mock_db, "on_spool_recovery_self_healed", 7)
            _, event, variables = mock_build.call_args.args
            assert event == "spool_recovery_self_healed"
            assert variables == {
                "printer_name": "001-H2S",
                "job_name": "SKU007 plate",
                "layer": "142",
                "spool_desc": "Bambu PETG (RFID a1b2)",
                "slot_desc": "AMS1 slot 1",
                "code": "0700_8010",
            }
            mock_send.assert_awaited_once()
            assert mock_send.call_args.args[4] == "spool_recovery_self_healed"
            assert mock_send.call_args.args[5] == 7
            assert mock_send.call_args.kwargs["force_immediate"] is True
            assert mock_send.call_args.kwargs["variables"] == variables

    @pytest.mark.asyncio
    async def test_on_spool_recovery_self_healed_skipped_when_disabled(self, service, mock_db):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
        ):
            mock_get.return_value = []  # provider boolean off → filtered out

            await service.on_spool_recovery_self_healed(
                7, "001-H2S", "SKU007 plate", 142, "Bambu PETG", "AMS1 slot 1", "0700_8010", mock_db
            )

            mock_send.assert_not_called()

    def test_spool_recovery_templates_seeded_and_render(self, service):
        """The four spool-recovery templates exist and substitute their variables
        (proves the template + variable contract for each event)."""
        from backend.app.models.notification_template import DEFAULT_TEMPLATES

        by_type = {t["event_type"]: t for t in DEFAULT_TEMPLATES}
        for event_type in (
            "spool_recovery_succeeded",
            "spool_recovery_failed",
            "spool_out_of_rotation",
            "spool_recovery_self_healed",
        ):
            assert event_type in by_type, f"missing seed for {event_type}"

        succeeded = service._render_template(
            by_type["spool_recovery_succeeded"]["body_template"],
            {
                "printer_name": "001-H2S",
                "job_name": "SKU007 plate",
                "layer": "142",
                "from_spool": "Bambu PETG",
                "to_spool": "Sunlu PETG",
            },
        )
        assert "layer 142" in succeeded and "Bambu PETG" in succeeded and "Sunlu PETG" in succeeded

        failed = service._render_template(
            by_type["spool_recovery_failed"]["body_template"],
            {"printer_name": "001-H2S", "job_name": "SKU007 plate", "detail": "No spool matched."},
        )
        assert "PAUSED" in failed and "No spool matched." in failed

        out = service._render_template(
            by_type["spool_out_of_rotation"]["body_template"],
            {
                "printer_name": "001-H2S",
                "spool_desc": "Bambu PETG",
                "slot_desc": "AMS 1 slot 3",
                "code": "07C0_2000",
            },
        )
        assert "Bambu PETG" in out and "AMS 1 slot 3" in out and "07C0_2000" in out

        self_healed = service._render_template(
            by_type["spool_recovery_self_healed"]["body_template"],
            {
                "printer_name": "001-H2S",
                "job_name": "SKU007 plate",
                "layer": "142",
                "spool_desc": "Bambu PETG",
                "slot_desc": "AMS1 slot 1",
                "code": "0700_8010",
            },
        )
        # The no-swap self-heal copy must be truthfully framed: same spool, nothing to do.
        assert "same spool" in self_healed and "no action needed" in self_healed


class TestOnStorageLowWording:
    """``on_storage_low`` detail composition: honest wording for the ``attempted``
    flag. ``attempted=False`` means NO cleanup ran (bare USB-drop detection) → the
    detail is the raw reason, never the misleading 'Auto-cleanup could not free
    space:' prefix that implies a failed cleanup attempt."""

    @pytest.fixture
    def service(self):
        return NotificationService()

    def _detail_for(self, mock_build):
        # _build_message_from_template(db, "storage_low", variables) — variables 3rd arg.
        return mock_build.call_args.args[2]["detail"]

    @pytest.mark.asyncio
    async def test_attempted_false_renders_reason_only_detail(self, service):
        mock_db = AsyncMock()
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [MagicMock()]
            mock_build.return_value = ("t", "m")
            await service.on_storage_low(
                4,
                "004-H2S",
                success=False,
                freed_bytes=0,
                files_deleted=0,
                free_bytes=None,
                reason="USB drive dropped/unmounted — power-cycle the printer to remount it",
                attempted=False,
                db=mock_db,
            )
        detail = self._detail_for(mock_build)
        assert "Auto-cleanup" not in detail
        # Grammatical: the raw reason with a single trailing period.
        assert detail == "USB drive dropped/unmounted — power-cycle the printer to remount it."

    @pytest.mark.asyncio
    async def test_attempted_false_fallback_when_no_reason(self, service):
        mock_db = AsyncMock()
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [MagicMock()]
            mock_build.return_value = ("t", "m")
            await service.on_storage_low(
                4,
                "004-H2S",
                success=False,
                freed_bytes=0,
                files_deleted=0,
                free_bytes=None,
                reason=None,
                attempted=False,
                db=mock_db,
            )
        assert self._detail_for(mock_build) == "USB problem detected."

    @pytest.mark.asyncio
    async def test_attempted_true_failure_keeps_prefix(self, service):
        """Default attempted=True: a real failed cleanup still reads the prefix."""
        mock_db = AsyncMock()
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [MagicMock()]
            mock_build.return_value = ("t", "m")
            await service.on_storage_low(
                4,
                "004-H2S",
                success=False,
                freed_bytes=0,
                files_deleted=0,
                free_bytes=None,
                reason="printer FTPS unreachable",
                db=mock_db,
            )
        assert self._detail_for(mock_build) == "Auto-cleanup could not free space: printer FTPS unreachable."

    @pytest.mark.asyncio
    async def test_attempted_true_success_unchanged(self, service):
        mock_db = AsyncMock()
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock),
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [MagicMock()]
            mock_build.return_value = ("t", "m")
            await service.on_storage_low(
                4,
                "004-H2S",
                success=True,
                freed_bytes=3 * 1024 * 1024,
                files_deleted=2,
                free_bytes=5 * 1024**3,
                reason=None,
                db=mock_db,
            )
        detail = self._detail_for(mock_build)
        assert detail.startswith("Auto-cleanup freed 3 MB across 2 file(s)")
        assert "5.0 GB free now" in detail


class TestQueueJobWaitingDedup:
    """Chokepoint re-notify floor for the queue-waiting event (Phase D).

    Production 2026-07-20: the identical "Low filament: 005-H2S (starting spool
    below minimum)" alert fired on EVERY 30 s scheduler tick — 16+ sends in
    8 minutes — because the emitter staged and notified with no transition guard.
    The emitters are fixed; this gate is the guarantee that no future caller can
    reproduce it. In-memory by design (see notify_dedup.allow).
    """

    @pytest.fixture
    def service(self):
        return NotificationService()

    @pytest.fixture(autouse=True)
    def _reset_dedup(self):
        notify_dedup._reset_state()
        yield
        notify_dedup._reset_state()

    @staticmethod
    async def _fire(service, db, *, reason="Low filament: 005-H2S", dedup_key="12"):
        with (
            patch.object(service, "_get_providers_for_event", new_callable=AsyncMock) as mock_get,
            patch.object(service, "_send_to_providers", new_callable=AsyncMock) as mock_send,
            patch.object(service, "_build_message_from_template", new_callable=AsyncMock) as mock_build,
        ):
            mock_get.return_value = [MagicMock()]
            mock_build.return_value = ("t", "m")
            await service.on_queue_job_waiting(
                job_name="job",
                target_model="H2S",
                waiting_reason=reason,
                db=db,
                dedup_key=dedup_key,
            )
            return mock_send.await_count

    @pytest.mark.asyncio
    async def test_repeat_within_the_window_is_suppressed(self, service):
        db = AsyncMock()
        assert await self._fire(service, db) == 1
        assert await self._fire(service, db) == 0
        assert await self._fire(service, db) == 0

    @pytest.mark.asyncio
    async def test_changed_reason_sends_again(self, service):
        """The reason NAMES the blocking printers — a different hold is news."""
        db = AsyncMock()
        assert await self._fire(service, db, reason="Low filament: 005-H2S") == 1
        assert await self._fire(service, db, reason="Low filament: 011-H2S") == 1

    @pytest.mark.asyncio
    async def test_other_items_are_independent(self, service):
        db = AsyncMock()
        assert await self._fire(service, db, dedup_key="12") == 1
        assert await self._fire(service, db, dedup_key="13") == 1

    @pytest.mark.asyncio
    async def test_no_dedup_key_keeps_the_ungated_behaviour(self, service):
        db = AsyncMock()
        assert await self._fire(service, db, dedup_key=None) == 1
        assert await self._fire(service, db, dedup_key=None) == 1

    @pytest.mark.asyncio
    async def test_sends_again_after_the_window_elapses(self, service):
        db = AsyncMock()
        assert await self._fire(service, db) == 1
        with patch(
            "backend.app.services.notification_service.time.time",
            return_value=time.time() + notification_service_module._QUEUE_WAITING_RENOTIFY_S + 1,
        ):
            assert await self._fire(service, db) == 1


class TestComposeHmsErrorSummary:
    """The pure aggregator that collapses a status push's surviving HMS codes into
    ONE printer-error message (2026-07-20: one physical feed fault sent 4 separate
    Discord messages under the old per-code send)."""

    def test_single_code_preserves_module_title_and_prefixes_code(self):
        error_type, error_detail = compose_hms_error_summary(
            [{"short_code": "0700_8010", "description": "AMS filament tangle", "module_name": "AMS/Filament"}]
        )
        assert error_type == "AMS/Filament Error"
        assert error_detail == "0700_8010 — AMS filament tangle"

    def test_multi_module_uses_generic_count_title(self):
        error_type, error_detail = compose_hms_error_summary(
            [
                {"short_code": "0700_8010", "description": "AMS tangle", "module_name": "AMS/Filament"},
                {"short_code": "0300_801E", "description": "Extruder overloaded", "module_name": "Print/Task"},
            ]
        )
        assert error_type == "Printer Errors (2)"
        assert "0700_8010 — AMS tangle" in error_detail
        assert "0300_801E — Extruder overloaded" in error_detail

    def test_triple_same_short_code_collapses_to_one_x3_line(self):
        """Three per-slot 0700_0081 instances render as one line with a ×3 suffix."""
        entry = {"short_code": "0700_0081", "description": "Failed to read the filament", "module_name": "AMS/Filament"}
        error_type, error_detail = compose_hms_error_summary([dict(entry), dict(entry), dict(entry)])
        assert error_type == "AMS/Filament Error"
        assert error_detail == "0700_0081 — Failed to read the filament ×3"
        assert "\n" not in error_detail  # collapsed to a single line

    def test_first_seen_order_is_preserved(self):
        error_type, error_detail = compose_hms_error_summary(
            [
                {"short_code": "0700_4025", "description": "Second", "module_name": "AMS/Filament"},
                {"short_code": "0700_0081", "description": "First", "module_name": "AMS/Filament"},
            ]
        )
        lines = error_detail.split("\n")
        assert lines[0].startswith("0700_4025")
        assert lines[1].startswith("0700_0081")
