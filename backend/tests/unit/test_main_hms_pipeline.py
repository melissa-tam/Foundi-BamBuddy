"""Regression tests for main's HMS notification pipeline (Phase D key switch).

The pipeline used to key its dedup off ``f"{e.attr:08x}"``. That key COLLIDES for
distinct codes sharing one attr — the AMS "failed to read" (``0700_0081``) and
"AMS main board" (``0700_4025``) faults arrive on the SAME attr, so only one of
them could ever notify — and it cannot address a durable ledger row without
ambiguity. The pipeline now keys off the lossless ``HMSError.full_code``,
records every ACTUAL send in the durable ledger, and seeds standing pre-restart
codes from it on the first push per printer.

These drive the real ``on_printer_status_change`` with the heavy side effects
(WebSocket, MQTT relay, DB, snapshot capture) patched out, so the assertions are
about the pipeline's decisions only. Phase A's discovery-read suppression branch
lives in the same loop and is pinned here too — the key switch must not change it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app import main as main_module
from backend.app.services import notify_dedup

# Two REAL production faults that share one attr — the collision the old key had.
_ATTR = 0x07002000
_READ_FAIL = SimpleNamespace(
    code="0x10081", attr=_ATTR, module=0x07, severity=2, full_code="0700200000010081", short="0700_0081"
)
_MAIN_BOARD = SimpleNamespace(
    code="0x24025", attr=_ATTR, module=0x07, severity=2, full_code="0700200000024025", short="0700_4025"
)


def _hms(spec: SimpleNamespace) -> SimpleNamespace:
    """An HMSError-shaped stub (the pipeline only reads these attributes)."""
    return SimpleNamespace(
        code=spec.code,
        attr=spec.attr,
        module=spec.module,
        severity=spec.severity,
        full_code=spec.full_code,
    )


def _state(hms: list, layer_num: int = 0) -> SimpleNamespace:
    """Minimal PrinterState stub carrying HMS. ``layer_num`` varies the status key
    so consecutive pushes are not swallowed by the broadcast dedup."""
    return SimpleNamespace(
        connected=True,
        state="IDLE",
        progress=0,
        layer_num=layer_num,
        temperatures={},
        raw_data={},
        stg_cur=0,
        cooling_fan_speed=0,
        big_fan1_speed=0,
        big_fan2_speed=0,
        chamber_light="",
        active_extruder=0,
        tray_now=255,
        door_open=False,
        subtask_name="",
        subtask_id="",
        ams_filament_backup=None,
        hms_errors=list(hms),
        sdcard=True,
        remaining_time=0,
        gcode_file="",
    )


@pytest.fixture(autouse=True)
def _reset_state():
    notify_dedup._reset_state()
    main_module._printer_last_connected.clear()
    main_module._printer_reconciled_since_connect.clear()
    main_module._last_status_broadcast.clear()
    yield
    notify_dedup._reset_state()
    main_module._printer_last_connected.clear()
    main_module._printer_reconciled_since_connect.clear()
    main_module._last_status_broadcast.clear()


class _Harness:
    """Patches every side effect around the HMS pipeline and records its calls."""

    def __init__(self):
        self.notify = MagicMock()
        self.notify.on_printer_error = AsyncMock()
        self.record_sent = AsyncMock()
        self.seed_standing = AsyncMock(return_value=set())
        self.suppress_read_failure = False

    def __enter__(self):
        db = AsyncMock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=SimpleNamespace(name="005-H2S")))
        )
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=db)
        session_cm.__aexit__ = AsyncMock(return_value=False)

        ws = MagicMock()
        ws.send_printer_status = AsyncMock()
        relay = MagicMock()
        relay.on_printer_status = AsyncMock()
        relay.on_printer_error = AsyncMock()
        pm = MagicMock()
        pm.get_printer.return_value = None
        pm.get_model.return_value = ""

        self._patches = [
            patch("backend.app.main.ws_manager", ws),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main.spawn_background_task"),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
            patch("backend.app.main.async_session", return_value=session_cm),
            patch("backend.app.main.notification_service", self.notify),
            patch("backend.app.main._capture_snapshot_for_notification", new=AsyncMock(return_value=None)),
            patch("backend.app.services.hms_catalog.lookup_full_code", return_value="Failed to read the filament"),
            patch(
                "backend.app.services.ams_presence.is_expected_read_failure",
                side_effect=lambda *_a, **_k: self.suppress_read_failure,
            ),
            patch.object(notify_dedup, "record_sent", self.record_sent),
            patch.object(notify_dedup, "seed_standing", self.seed_standing),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False

    @property
    def sent_error_types(self) -> list:
        return [c.args[2] for c in self.notify.on_printer_error.await_args_list]

    @property
    def ledger_keys(self) -> list[str]:
        return [c.args[2] for c in self.record_sent.await_args_list]


@pytest.mark.asyncio
class TestFullCodeKeySwitch:
    async def test_two_codes_sharing_one_attr_both_notify(self):
        """The collision fix: attr-only keying deduped these two DIFFERENT faults
        into one incident, so the second never reached the operator."""
        with _Harness() as h:
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL), _hms(_MAIN_BOARD)]))

        assert h.notify.on_printer_error.await_count == 2

    async def test_each_send_records_its_own_lossless_ledger_key(self):
        with _Harness() as h:
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL), _hms(_MAIN_BOARD)]))

        assert sorted(h.ledger_keys) == sorted(
            [
                notify_dedup.hms_ledger_key(5, _READ_FAIL.full_code),
                notify_dedup.hms_ledger_key(5, _MAIN_BOARD.full_code),
            ]
        )

    async def test_standing_code_does_not_renotify_on_later_pushes(self):
        """Unchanged dedup semantics after the key switch: the same live code on
        the next push is one continuing incident."""
        with _Harness() as h:
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL)], layer_num=1))
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL)], layer_num=2))

        assert h.notify.on_printer_error.await_count == 1

    async def test_second_fault_on_a_standing_attr_still_notifies(self):
        """Push 1 raises the read failure; push 2 adds the sibling code on the SAME
        attr. Under the old key push 2 was invisible."""
        with _Harness() as h:
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL)], layer_num=1))
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL), _hms(_MAIN_BOARD)], layer_num=2))

        assert h.notify.on_printer_error.await_count == 2
        assert h.ledger_keys[-1] == notify_dedup.hms_ledger_key(5, _MAIN_BOARD.full_code)


@pytest.mark.asyncio
class TestDiscoverySuppressionSurvivesTheKeySwitch:
    """Phase A: a "failed to read" answering a discovery read WE commanded on a
    possibly-tagless slot is the expected answer "no tag", not a fault report."""

    async def test_expected_read_failure_is_not_notified(self):
        with _Harness() as h:
            h.suppress_read_failure = True
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL)]))

        h.notify.on_printer_error.assert_not_awaited()

    async def test_suppressed_code_is_not_recorded_in_the_durable_ledger(self):
        """Only ACTUAL sends stamp the ledger — otherwise a suppressed discovery
        failure would masquerade as "the operator was told" after a restart."""
        with _Harness() as h:
            h.suppress_read_failure = True
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL)]))

        h.record_sent.assert_not_awaited()

    async def test_unexpected_read_failure_still_notifies(self):
        """No commanded read ⇒ a genuinely failing reader ⇒ the alert must land."""
        with _Harness() as h:
            h.suppress_read_failure = False
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL)]))

        assert h.notify.on_printer_error.await_count == 1


@pytest.mark.asyncio
class TestStandingSeedHook:
    async def test_first_push_seeds_with_the_live_full_codes(self):
        with _Harness() as h:
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL)]))

        h.seed_standing.assert_awaited_once()
        args = h.seed_standing.await_args.args
        assert args[1] == 5
        assert args[2] == {_READ_FAIL.full_code}

    async def test_seed_runs_once_per_printer(self):
        """needs_standing_seed() gates the DB session, so later pushes don't read."""
        with _Harness() as h:
            h.seed_standing.side_effect = lambda db, pid, keys, now: notify_dedup._standing_seeded.add(pid) or set()
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL)], layer_num=1))
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL)], layer_num=2))

        assert h.seed_standing.await_count == 1
