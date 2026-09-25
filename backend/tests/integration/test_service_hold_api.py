"""``/api/v1/printers/{id}/service-hold`` — maintenance mode over HTTP.

The route pair is thin by design (404 → service → shape), so these pins are about the
CONTRACT the printer card is built against: the verdict's six bools, an idempotent
release, the permission, and the ``service_hold`` projection on all three read surfaces
(the printer list, a connected ``/status`` and a disconnected one).

One neighbour is pinned here too, because it is what an operator actually reaches for on
the 2026-09-12 shape: the deactivate PATCH runs the teardown quiesce BEFORE it tears the
MQTT session down
(the order is the whole fix). The clear-plate refusal's new copy ("Use Recover to
override.") stays with the route that raises it, in ``test_printers_api.py``.
"""

from types import SimpleNamespace

import pytest
from httpx import AsyncClient

from backend.app.models.printer_incident import KIND_SERVICE_HOLD, STATUS_ESCALATED, PrinterIncident
from backend.app.services import printer_incidents, service_hold
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _reset():
    printer_incidents._reset_state()
    yield
    printer_incidents._reset_state()


async def _enable_auth(db_session):
    from backend.app.models.settings import Settings

    db_session.add(Settings(key="auth_enabled", value="true"))
    await db_session.commit()


async def _api_key(db_session, **scopes) -> str:
    from backend.app.core.auth import generate_api_key
    from backend.app.models.api_key import APIKey

    full_key, key_hash, key_prefix = generate_api_key()
    db_session.add(APIKey(name="probe", key_hash=key_hash, key_prefix=key_prefix, enabled=True, **scopes))
    await db_session.commit()
    return full_key


class TestEnterAndExit:
    async def test_enter_returns_the_verdict_and_records_the_hold(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        printer = await printer_factory()

        response = await async_client.post(f"/api/v1/printers/{printer.id}/service-hold")

        assert response.status_code == 200
        assert response.json() == {
            "held": True,
            "already_held": False,
            # Nothing to quiesce on an idle printer with no session in a test process.
            # There is no ``job_stopped`` key: no mode verb ends a print (2026-09-19),
            # so a bool for it could only ever be False.
            "eject_stopped": False,
            "lease_revoked": False,
        }
        row = await printer_incidents.get_open(db_session, printer.id, kinds={KIND_SERVICE_HOLD})
        assert row is not None and row.status == STATUS_ESCALATED
        assert printer_incidents.automation_held(printer.id) is True

    async def test_a_second_enter_is_already_held(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory()
        await async_client.post(f"/api/v1/printers/{printer.id}/service-hold")

        response = await async_client.post(f"/api/v1/printers/{printer.id}/service-hold")

        assert response.status_code == 200
        assert response.json()["already_held"] is True
        assert response.json()["held"] is True

    async def test_enter_works_on_a_deactivated_printer(self, async_client: AsyncClient, printer_factory):
        """Nothing to quiesce with no session — and the hold is still what stops the
        kick-driven scheduler dispatching within ~1 s of re-activation."""
        printer = await printer_factory(is_active=False)

        response = await async_client.post(f"/api/v1/printers/{printer.id}/service-hold")

        assert response.status_code == 200
        assert response.json()["held"] is True
        assert printer_incidents.automation_held(printer.id) is True

    async def test_exit_releases_the_hold(self, async_client: AsyncClient, printer_factory):
        printer = await printer_factory()
        await async_client.post(f"/api/v1/printers/{printer.id}/service-hold")

        response = await async_client.delete(f"/api/v1/printers/{printer.id}/service-hold")

        assert response.status_code == 200
        assert response.json() == {"released": True}
        assert printer_incidents.automation_held(printer.id) is False

    async def test_exit_on_an_unheld_printer_is_a_200_with_released_false(
        self, async_client: AsyncClient, printer_factory
    ):
        """Idempotent on purpose: a 404 or 409 here would make the card's exit button
        fail on exactly the state it is trying to reach."""
        printer = await printer_factory()

        response = await async_client.delete(f"/api/v1/printers/{printer.id}/service-hold")

        assert response.status_code == 200
        assert response.json() == {"released": False}

    @pytest.mark.parametrize("method", ["post", "delete"])
    async def test_unknown_printer_is_404(self, async_client: AsyncClient, method):
        response = await getattr(async_client, method)("/api/v1/printers/424242/service-hold")

        assert response.status_code == 404

    @pytest.mark.parametrize("method", ["post", "delete"])
    async def test_forbidden_without_printers_control(
        self, async_client: AsyncClient, db_session, printer_factory, method
    ):
        """The verb STOPS a print and a sweep — that is the control class, not the
        printer-record edit permission."""
        printer = await printer_factory()
        await _enable_auth(db_session)
        key = await _api_key(db_session, can_read_status=True, can_control_printer=False)

        response = await getattr(async_client, method)(
            f"/api/v1/printers/{printer.id}/service-hold", headers={"X-API-Key": key}
        )

        assert response.status_code == 403
        assert printer_incidents.automation_held(printer.id) is False

    @pytest.mark.parametrize("method", ["post", "delete"])
    async def test_allowed_with_printers_control(self, async_client: AsyncClient, db_session, printer_factory, method):
        printer = await printer_factory()
        await _enable_auth(db_session)
        key = await _api_key(db_session, can_read_status=True, can_control_printer=True)

        response = await getattr(async_client, method)(
            f"/api/v1/printers/{printer.id}/service-hold", headers={"X-API-Key": key}
        )

        assert response.status_code == 200


class TestServiceHoldProjection:
    async def test_the_printer_list_carries_the_hold(self, async_client: AsyncClient, printer_factory, db_session):
        held = await printer_factory(name="009-H2S")
        free = await printer_factory(name="010-H2S")
        await service_hold.enter(db_session, held.id, actor="raymond")

        response = await async_client.get("/api/v1/printers/")

        assert response.status_code == 200
        by_id = {row["id"]: row for row in response.json()}
        assert by_id[held.id]["service_hold"]["since"]
        assert by_id[free.id]["service_hold"] is None

    async def test_the_disconnected_status_branch_carries_the_hold(
        self, async_client: AsyncClient, printer_factory, db_session
    ):
        """A held printer can also be deactivated, and that branch is the ONLY status
        the card gets then — a hold missing here is a hold the operator cannot exit."""
        printer = await printer_factory()
        await service_hold.enter(db_session, printer.id, actor="raymond")

        response = await async_client.get(f"/api/v1/printers/{printer.id}/status")

        assert response.status_code == 200
        body = response.json()
        assert body["connected"] is False  # no MQTT session in the test process
        assert body["service_hold"]["since"]

    async def test_the_connected_status_branch_carries_the_hold(
        self, async_client: AsyncClient, printer_factory, db_session, monkeypatch
    ):
        printer = await printer_factory()
        await service_hold.enter(db_session, printer.id, actor="raymond")
        monkeypatch.setattr(printer_manager, "get_status", lambda pid: _connected_state())

        response = await async_client.get(f"/api/v1/printers/{printer.id}/status")

        assert response.status_code == 200
        body = response.json()
        assert body["connected"] is True
        assert body["service_hold"]["since"]

    async def test_an_unheld_printer_reports_null_on_both_branches(
        self, async_client: AsyncClient, printer_factory, monkeypatch
    ):
        printer = await printer_factory()

        disconnected = await async_client.get(f"/api/v1/printers/{printer.id}/status")
        monkeypatch.setattr(printer_manager, "get_status", lambda pid: _connected_state())
        connected = await async_client.get(f"/api/v1/printers/{printer.id}/status")

        assert disconnected.json()["service_hold"] is None
        assert connected.json()["service_hold"] is None


def _connected_state():
    """The minimum live state the connected ``/status`` branch needs to build a frame."""
    return SimpleNamespace(
        connected=True,
        state="IDLE",
        current_print=None,
        subtask_name="",
        subtask_id="",
        gcode_file="",
        progress=0.0,
        remaining_time=0,
        layer_num=0,
        total_layers=0,
        temperatures={},
        hms_errors=[],
        raw_data={},
        kprofiles=[],
        ams_tray_exist_bits=None,
        ams_power_on_flag=None,
        ams_bits_trusted=False,
        sdcard=True,
        store_to_sdcard=False,
        timelapse=False,
        ipcam=False,
        wifi_signal=None,
        wired_network=False,
        door_open=False,
        nozzles=[],
        nozzle_rack=[],
        print_options=SimpleNamespace(
            spaghetti_detector=False,
            print_halt=False,
            halt_print_sensitivity="",
            first_layer_inspector=False,
            printing_monitor=False,
            buildplate_marker_detector=False,
            allow_skip_parts=False,
            nozzle_clumping_detector=False,
            nozzle_clumping_sensitivity="",
            pileup_detector=False,
            pileup_sensitivity="",
            airprint_detector=False,
            airprint_sensitivity="",
            auto_recovery_step_loss=False,
            filament_tangle_detect=False,
        ),
        stg_cur=-1,
        stg=[],
        airduct_mode=0,
        speed_level=2,
        chamber_light=False,
        active_extruder=0,
        ams_extruder_map={},
        tray_now=255,
        ams_status_main=0,
        ams_status_sub=0,
        mc_print_sub_stage=0,
        last_ams_update=0.0,
        printable_objects=[],
        cooling_fan_speed=None,
        big_fan1_speed=None,
        big_fan2_speed=None,
        heatbreak_fan_speed=None,
        firmware_version="01.01.02.00",
        developer_mode=True,
        ams_filament_backup=None,
        fila_switch=None,
    )


class TestDeactivateQuiescesFirst:
    async def test_the_patch_quiesces_on_the_deactivate_transition_before_disconnecting(
        self, async_client: AsyncClient, printer_factory, monkeypatch
    ):
        """The ROUTE's half: deactivation runs ONE verb, with the ``deactivate`` cause, first.

        ``quiesce_for_teardown`` is the verb, not the bare ``quiesce``: a deactivation is
        the one caller that must also RETIRE the plate watch, because it is about to drop
        the session the watch's actuators speak over. Entering maintenance mode explicitly
        does not.

        Call order alone is NOT the fix, and this pin deliberately does not claim to be
        one: the 2026-09-12 probe found the route already in this order while the fans
        stayed on, because ``stand_down``'s cancel is only scheduled. What the ordering
        has to ACHIEVE — both fans commanded off while the client still exists — is
        pinned as a consequence in
        ``test_service_hold.py::TestEnteringKeepsTheCooldown``, over the real monitor and
        the real cooldown prep. This one exists so that a route which stopped calling the
        teardown at all still fails something.
        """
        printer = await printer_factory(is_active=True)
        order: list[str] = []

        async def _fake_quiesce(printer_id, *, cause):
            order.append(f"quiesce:{cause}")
            return service_hold.QuiesceReport()

        monkeypatch.setattr(service_hold, "quiesce_for_teardown", _fake_quiesce)
        monkeypatch.setattr(printer_manager, "disconnect_printer", lambda pid: order.append("disconnect"))

        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"is_active": False})

        assert response.status_code == 200
        assert order == ["quiesce:deactivate", "disconnect"]

    async def test_an_unrelated_patch_quiesces_nothing(self, async_client: AsyncClient, printer_factory, monkeypatch):
        printer = await printer_factory(is_active=True)
        order: list[str] = []

        async def _fake_quiesce(printer_id, *, cause):
            order.append("quiesce")
            return service_hold.QuiesceReport()

        monkeypatch.setattr(service_hold, "quiesce_for_teardown", _fake_quiesce)

        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"location": "bench 2"})

        assert response.status_code == 200
        assert order == []

    async def test_deactivating_a_printer_mid_print_does_not_stop_the_print(
        self, async_client: AsyncClient, printer_factory, monkeypatch
    ):
        """The ROUTE-level half of the 2026-09-19 ruling, over the REAL quiesce.

        Deactivation runs the full teardown verb — the one that retires actuators while
        the wire is still there — on a printer reporting RUNNING, and no ``print.stop``
        goes out. The printer keeps printing from its own USB storage; the queue row is
        resolved by the reconcile that runs when it is re-activated.
        """
        from backend.app.services import print_control

        printer = await printer_factory(is_active=True)
        stopped: list[int] = []
        marked: list[int] = []

        async def _stamp(_db, item_id, *, requested_at):
            marked.append(item_id)
            return True

        monkeypatch.setattr(printer_manager, "get_status", lambda pid: SimpleNamespace(state="RUNNING"))
        monkeypatch.setattr(print_control.printer_manager, "stop_print", lambda pid: stopped.append(pid) or True)
        monkeypatch.setattr(print_control, "stamp_operator_stop", _stamp)
        monkeypatch.setattr(printer_manager, "disconnect_printer", lambda pid: None)

        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"is_active": False})

        assert response.status_code == 200
        assert stopped == []
        assert marked == []

    async def test_an_already_deactivated_printer_is_not_quiesced_again(
        self, async_client: AsyncClient, printer_factory, monkeypatch
    ):
        """The branch is about the TRANSITION: an idempotent write must not re-run a
        stand-down over a printer that has had no session for hours."""
        printer = await printer_factory(is_active=False)
        order: list[str] = []

        async def _fake_quiesce(printer_id, *, cause):
            order.append("quiesce")
            return service_hold.QuiesceReport()

        monkeypatch.setattr(service_hold, "quiesce_for_teardown", _fake_quiesce)

        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"is_active": False})

        assert response.status_code == 200
        assert order == []

    async def test_re_activation_reconsiders_the_plate(self, async_client: AsyncClient, printer_factory, monkeypatch):
        """A plate gated while the printer had no session gets its watch back without a
        restart. It is the ONE remaining ``reconsider`` cause: a deactivation retired the
        watch, and no occupancy transition happens to re-arm it (entering a hold no longer
        retires one, so releasing a hold no longer needs this)."""
        from backend.app.services.eject.monitor import eject_cooldown_monitor

        printer = await printer_factory(is_active=False)
        causes: list[str] = []

        async def _fake_connect(row):
            return None

        monkeypatch.setattr(printer_manager, "connect_printer", _fake_connect)
        monkeypatch.setattr(printer_manager, "disconnect_printer", lambda pid: None)
        monkeypatch.setattr(eject_cooldown_monitor, "reconsider", lambda pid, cause: causes.append((pid, cause)))

        response = await async_client.patch(f"/api/v1/printers/{printer.id}", json={"is_active": True})

        assert response.status_code == 200
        assert causes == [(printer.id, "printer activated")]
