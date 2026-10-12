"""ONE provider-event vocabulary (2026-10-11).

A notification provider's ``on_*`` toggles live in four places — the model's columns, the create and
update schemas, and the route's response dict and create kwargs. While the last three were hand-kept
lists, 15 farm event columns (the attention owner's ``on_printer_waiting`` among them) never reached the
API: a PATCH was dropped by ``model_dump(exclude_unset=True)``, the response never returned the field, and
the "Cooldown running long" toggle silently never persisted. The route now derives its dict and kwargs
from the model's columns (``PROVIDER_EVENT_FIELDS``); this pins the schemas to the same set and defaults,
and proves a toggle round-trips.
"""

import pytest
from httpx import AsyncClient

from backend.app.api.routes.notifications import PROVIDER_EVENT_FIELDS
from backend.app.models.notification import NotificationProvider
from backend.app.schemas.notification import (
    NotificationProviderBase,
    NotificationProviderResponse,
    NotificationProviderUpdate,
)


def _model_events() -> dict[str, bool]:
    return {
        column.name: column.default.arg
        for column in NotificationProvider.__table__.columns
        if column.name.startswith("on_")
    }


class TestOneVocabulary:
    def test_the_route_reads_every_model_column(self):
        assert set(PROVIDER_EVENT_FIELDS) == set(_model_events())

    def test_the_create_and_response_schemas_carry_every_column_with_its_default(self):
        fields = {
            name: field.default
            for name, field in NotificationProviderBase.model_fields.items()
            if name.startswith("on_")
        }
        assert fields == _model_events()
        assert {name for name in NotificationProviderResponse.model_fields if name.startswith("on_")} == set(
            _model_events()
        )

    def test_the_update_schema_carries_every_column(self):
        assert {name for name in NotificationProviderUpdate.model_fields if name.startswith("on_")} == set(
            _model_events()
        )


class TestAToggleRoundTrips:
    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.parametrize("field", ["on_printer_waiting", "on_cooldown_escalation", "on_stock_break_alert"])
    async def test_a_patch_persists_and_reads_back(
        self, async_client: AsyncClient, notification_provider_factory, field
    ):
        provider = await notification_provider_factory(name=f"Toggle {field}")
        before = (await async_client.get(f"/api/v1/notifications/{provider.id}")).json()[field]

        response = await async_client.patch(f"/api/v1/notifications/{provider.id}", json={field: not before})

        assert response.status_code == 200
        assert response.json()[field] is (not before)
        assert (await async_client.get(f"/api/v1/notifications/{provider.id}")).json()[field] is (not before)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_response_returns_every_event_field(
        self, async_client: AsyncClient, notification_provider_factory
    ):
        provider = await notification_provider_factory(name="All events")

        body = (await async_client.get(f"/api/v1/notifications/{provider.id}")).json()

        assert {key for key in body if key.startswith("on_")} == set(_model_events())

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_create_carries_the_farm_events(self, async_client: AsyncClient):
        data = {
            "name": "Farm pager",
            "provider_type": "ntfy",
            "enabled": True,
            "config": {"server": "https://ntfy.sh", "topic": "farm"},
            "on_printer_waiting": False,
            "on_spool_recovery_failed": False,
        }

        body = (await async_client.post("/api/v1/notifications/", json=data)).json()

        assert body["on_printer_waiting"] is False
        assert body["on_spool_recovery_failed"] is False
        assert body["on_cooldown_escalation"] is True  # the model's default
