"""Route-level contract for POST /printers/{id}/eject — the verdict → HTTP map.

The service decides and returns ONE ``EjectVerdict``; the route maps it and owns every
English sentence in the lane. These tests patch the service and assert the SERIALISED
body, because that body is the frontend's contract: ``useEjectPlate`` branches on
``detail.code`` and reads per-code extras out of the same object.

The pinned shapes:

* 200 ``{mode, queue_item_id}`` for the two success outcomes;
* 409 ``{"code": "foreign_plate", "origin", ...}`` for ``needs_input`` — the dialog-
  opening contract, kept under its original wire code while ``origin`` widened
  underneath it;
* 409 ``{"code": "bed_hot", "bed_c", "threshold_c", "message"}``;
* one ``{code, message}`` shape for every refusal, 404 for the two that are a missing
  resource, with ``eject_in_flight`` alone carrying ``started``/``age_s``.
"""

from unittest.mock import AsyncMock, patch

import pytest

from backend.app.services.eject.manual import EjectVerdict
from backend.app.services.eject.remote import EjectDispatchError

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

_MANUAL = "backend.app.api.routes.printer_eject.manual_eject"


async def _post(async_client, printer_id, verdict, body=None):
    with patch(_MANUAL, AsyncMock(return_value=verdict)) as mock:
        response = await async_client.post(f"/api/v1/printers/{printer_id}/eject", json=body or {})
    return response, mock


class TestSuccessShapes:
    async def test_dispatched_returns_mode_and_item(self, async_client, printer_factory):
        printer = await printer_factory(name="EJR1", model="H2S")
        r, _ = await _post(async_client, printer.id, EjectVerdict.dispatched(41))
        assert r.status_code == 200
        assert r.json() == {"mode": "dispatched", "queue_item_id": 41}

    async def test_released_watch_returns_its_own_mode(self, async_client, printer_factory):
        printer = await printer_factory(name="EJR2", model="H2S")
        r, _ = await _post(async_client, printer.id, EjectVerdict.released_watch(7))
        assert r.status_code == 200
        assert r.json() == {"mode": "released_watch", "queue_item_id": 7}

    async def test_foreign_confirm_returns_a_null_queue_item(self, async_client, printer_factory):
        printer = await printer_factory(name="EJR3", model="H2S")
        r, _ = await _post(async_client, printer.id, EjectVerdict.dispatched(None))
        assert r.status_code == 200
        assert r.json() == {"mode": "dispatched", "queue_item_id": None}


class TestBodyThreading:
    async def test_every_field_reaches_the_service(self, async_client, printer_factory):
        printer = await printer_factory(name="EJRB", model="H2S")
        _r, mock = await _post(
            async_client,
            printer.id,
            EjectVerdict.dispatched(None),
            body={
                "eject_profile_id": 42,
                "allow_hot": True,
                "declare_occupied": True,
                "max_z_height_mm": 21.5,
            },
        )
        kwargs = mock.await_args.kwargs
        assert kwargs["eject_profile_id"] == 42
        assert kwargs["allow_hot"] is True
        assert kwargs["declare_occupied"] is True
        assert kwargs["max_z_override"] == 21.5

    async def test_a_non_positive_height_is_rejected_at_the_boundary(self, async_client, printer_factory):
        # ``max_z`` sets the sweep's clearance and lift; zero or negative is not a height.
        printer = await printer_factory(name="EJRZ", model="H2S")
        r, _ = await _post(async_client, printer.id, EjectVerdict.dispatched(None), body={"max_z_height_mm": 0})
        assert r.status_code == 422


class TestNeedsInput:
    """``needs_input`` is the dialog, not a failure. The wire code stays ``foreign_plate``
    — the contract ``useEjectPlate`` has always branched on — and ``origin`` is what
    widened underneath it."""

    @pytest.mark.parametrize("origin", ["foreign", "farm_unit", "declared"])
    async def test_each_origin_carries_its_own_message(self, async_client, printer_factory, origin):
        printer = await printer_factory(name=f"EJRN{origin}", model="H2S")
        verdict = EjectVerdict.needs_input(
            origin=origin,
            print_name="Foreign Widget",
            max_z_height_mm=18.0,
            suggested_eject_profile_id=7,
        )
        r, _ = await _post(async_client, printer.id, verdict)
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "foreign_plate"
        assert detail["origin"] == origin
        assert detail["print_name"] == "Foreign Widget"
        assert detail["max_z_height_mm"] == 18.0
        assert detail["suggested_eject_profile_id"] == 7
        assert isinstance(detail["message"], str) and detail["message"]

    async def test_messages_differ_per_origin(self, async_client, printer_factory):
        printer = await printer_factory(name="EJRNM", model="H2S")
        messages = set()
        for origin in ("foreign", "farm_unit", "declared"):
            verdict = EjectVerdict.needs_input(
                origin=origin, print_name="Widget", max_z_height_mm=None, suggested_eject_profile_id=None
            )
            r, _ = await _post(async_client, printer.id, verdict)
            messages.add(r.json()["detail"]["message"])
        assert len(messages) == 3

    async def test_farm_unit_message_names_the_unit(self, async_client, printer_factory):
        printer = await printer_factory(name="EJRNF", model="H2S")
        verdict = EjectVerdict.needs_input(
            origin="farm_unit", print_name="SKU007.01 unit", max_z_height_mm=12.0, suggested_eject_profile_id=3
        )
        r, _ = await _post(async_client, printer.id, verdict)
        assert "SKU007.01 unit" in r.json()["detail"]["message"]

    async def test_container_prompt_carries_explicit_nulls(self, async_client, printer_factory):
        # The dialog must see "unknown", never a fabricated 0 it might confirm unread.
        printer = await printer_factory(name="EJRNC", model="H2S")
        verdict = EjectVerdict.needs_input(
            origin="declared", print_name=None, max_z_height_mm=None, suggested_eject_profile_id=None
        )
        r, _ = await _post(async_client, printer.id, verdict)
        detail = r.json()["detail"]
        assert detail["print_name"] is None
        assert detail["max_z_height_mm"] is None
        assert detail["suggested_eject_profile_id"] is None


class TestBedHot:
    async def test_bed_hot_carries_both_numbers_and_a_message(self, async_client, printer_factory):
        printer = await printer_factory(name="EJRH", model="H2S")
        r, _ = await _post(async_client, printer.id, EjectVerdict.bed_hot(50.0, 33.0))
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == "bed_hot"
        assert detail["bed_c"] == 50.0
        assert detail["threshold_c"] == 33.0
        # The message is new: the old shape shipped bare numbers, so an API client or a
        # log line had nothing to read.
        assert "50.0" in detail["message"] and "33.0" in detail["message"]


class TestRefusals:
    @pytest.mark.parametrize(
        "reason",
        [
            "job_active",
            "dispatch_in_flight",
            "not_connected",
            "no_plate_gate",
            "bed_unreadable",
            "first_article",
            "no_donor",
        ],
    )
    async def test_conflict_refusals_carry_code_and_message(self, async_client, printer_factory, reason):
        printer = await printer_factory(name=f"EJRR{reason}", model="H2S")
        r, _ = await _post(async_client, printer.id, EjectVerdict.refused(reason))
        assert r.status_code == 409
        detail = r.json()["detail"]
        assert detail["code"] == reason
        assert isinstance(detail["message"], str) and detail["message"]
        # Only eject_in_flight ships extras; every other refusal is exactly two fields.
        assert set(detail) == {"code", "message"}

    @pytest.mark.parametrize("reason", ["not_found", "profile_not_found"])
    async def test_missing_resource_refusals_are_404_and_still_structured(self, async_client, printer_factory, reason):
        printer = await printer_factory(name=f"EJR404{reason}", model="H2S")
        r, _ = await _post(async_client, printer.id, EjectVerdict.refused(reason))
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == reason

    async def test_eject_in_flight_ships_started_and_age(self, async_client, printer_factory):
        printer = await printer_factory(name="EJRIF", model="H2S")
        verdict = EjectVerdict.refused("eject_in_flight", started=True, age_s=42.5)
        r, _ = await _post(async_client, printer.id, verdict)
        assert r.status_code == 409
        assert r.json()["detail"] == {
            "code": "eject_in_flight",
            "message": "An eject is already in flight on this printer",
            "started": True,
            "age_s": 42.5,
        }

    async def test_eject_in_flight_age_may_be_null(self, async_client, printer_factory):
        # A pending with no dispatch stamp (a hydrated record's shape) has no age to
        # report; the frontend renders a no-age sentence rather than "0 s ago".
        printer = await printer_factory(name="EJRIFN", model="H2S")
        verdict = EjectVerdict.refused("eject_in_flight", started=False, age_s=None)
        r, _ = await _post(async_client, printer.id, verdict)
        detail = r.json()["detail"]
        assert detail["started"] is False
        assert detail["age_s"] is None


class TestDispatchFailure:
    """``EjectDispatchError`` still propagates — a build or transport failure is
    infrastructure, not a verdict, and it keeps its own status hint and code."""

    async def test_dispatch_error_keeps_its_status_and_code(self, async_client, printer_factory):
        printer = await printer_factory(name="EJRD", model="H2S")
        exc = EjectDispatchError("Failed to upload the eject file to the printer", status_code=502)
        with patch(_MANUAL, AsyncMock(side_effect=exc)):
            r = await async_client.post(f"/api/v1/printers/{printer.id}/eject", json={})
        assert r.status_code == 502
        assert r.json()["detail"] == {
            "code": "eject_dispatch_failed",
            "message": "Failed to upload the eject file to the printer",
        }

    async def test_a_claim_lost_during_upload_reports_the_authority_token(self, async_client, printer_factory):
        # One refusal vocabulary from the state machine to the dialog.
        printer = await printer_factory(name="EJRDC", model="H2S")
        exc = EjectDispatchError("Printer is running a job", status_code=409, code="job_active")
        with patch(_MANUAL, AsyncMock(side_effect=exc)):
            r = await async_client.post(f"/api/v1/printers/{printer.id}/eject", json={})
        assert r.status_code == 409
        assert r.json()["detail"]["code"] == "job_active"
