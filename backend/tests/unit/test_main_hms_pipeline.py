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
                side_effect=self._is_expected_read_failure,
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

    def _is_expected_read_failure(self, printer_id, attr, code):
        """``suppress_read_failure`` may be a bool (all codes) or a predicate
        ``(printer_id, attr, code) -> bool`` for per-code control."""
        pred = self.suppress_read_failure
        if callable(pred):
            return pred(printer_id, attr, code)
        return bool(pred)

    @property
    def sent_error_types(self) -> list:
        return [c.args[2] for c in self.notify.on_printer_error.await_args_list]

    @property
    def sent_bodies(self) -> list[str]:
        # on_printer_error(printer_id, printer_name, error_type, db, error_detail, ...)
        return [c.args[4] for c in self.notify.on_printer_error.await_args_list]

    @property
    def ledger_keys(self) -> list[str]:
        return [c.args[2] for c in self.record_sent.await_args_list]


@pytest.mark.asyncio
class TestFullCodeKeySwitch:
    async def test_two_codes_sharing_one_attr_both_notify(self):
        """The collision fix: attr-only keying deduped these two DIFFERENT faults
        into one incident, so the second never reached the operator. Both now reach
        the operator — aggregated (2026-07-20) into ONE message with a line each."""
        with _Harness() as h:
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL), _hms(_MAIN_BOARD)]))

        assert h.notify.on_printer_error.await_count == 1
        body = h.sent_bodies[0]
        assert "0700_0081" in body
        assert "0700_4025" in body

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


# --- Aggregation + recovery-owned suppression (2026-07-20) -------------------
# One physical feed fault emits several HMS codes at once, and the old loop fired
# one Discord message PER code (4 for one tangle). The pipeline now (a) aggregates
# a status push's surviving codes into ONE message and (b) suppresses the raw
# per-code alerts entirely when spool_recovery will own the incident (its lifecycle
# notifications are the operator signal).


def _err(*, code: str, attr: int, module: int, full_code: str, severity: int = 2) -> SimpleNamespace:
    """An HMSError-shaped stub with arbitrary raw fields (the pipeline reads only
    code/attr/module/severity/full_code)."""
    return SimpleNamespace(code=code, attr=attr, module=module, severity=severity, full_code=full_code)


# A recoverable AMS feed fault (0700_8010 ∈ RECOVERABLE_HMS_CODES).
_FEED_FAULT = _err(code="0x8010", attr=0x07000000, module=0x07, full_code="0700000000008010")
# A slot-attributed runout companion: short code "0700_0001" (which must NEVER be
# matched as a bare string — it collides with runout routing), but attr 0x07002000 +
# code 0x00020001 decodes via runout_slot_from_hms → (0, 0).
_RUNOUT_COMPANION = _err(code="0x20001", attr=0x07002000, module=0x07, full_code="0700200000020001")
# An ordinary known code that is neither recoverable nor a slot-attributed runout.
_UNRELATED = _err(code="0x24025", attr=0x07002000, module=0x07, full_code="0700200000024025")


@pytest.mark.asyncio
class TestHmsAggregation:
    async def test_two_new_codes_one_message(self):
        """Two catalog-known new codes in one push collapse to a single message
        carrying both, and each full_code is stamped in the durable ledger."""
        with _Harness() as h:
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL), _hms(_MAIN_BOARD)]))

        assert h.notify.on_printer_error.await_count == 1
        body = h.sent_bodies[0]
        assert "0700_0081 — Failed to read the filament" in body
        assert "0700_4025 — Failed to read the filament" in body
        assert sorted(h.ledger_keys) == sorted(
            [
                notify_dedup.hms_ledger_key(5, _READ_FAIL.full_code),
                notify_dedup.hms_ledger_key(5, _MAIN_BOARD.full_code),
            ]
        )

    async def test_same_short_code_three_instances_one_line(self):
        """Three distinct full codes sharing one short code (three per-slot 0700_0081
        instances on different attrs) render as ONE ×3 line, with three ledger keys."""
        a = _err(code="0x10081", attr=0x07002000, module=0x07, full_code="0700200000010081")
        b = _err(code="0x10081", attr=0x07002100, module=0x07, full_code="0700210000010081")
        c = _err(code="0x10081", attr=0x07002200, module=0x07, full_code="0700220000010081")
        with _Harness() as h:
            await main_module.on_printer_status_change(5, _state([a, b, c]))

        assert h.notify.on_printer_error.await_count == 1
        body = h.sent_bodies[0]
        assert body == "0700_0081 — Failed to read the filament ×3"
        assert len(h.ledger_keys) == 3
        assert set(h.ledger_keys) == {
            notify_dedup.hms_ledger_key(5, code) for code in (a.full_code, b.full_code, c.full_code)
        }

    async def test_per_code_filters_apply_before_aggregation(self):
        """A push mixing a suppress-set code, an expected-read-failure code and a
        storage-low code with one ordinary code yields a single message containing
        ONLY the ordinary code — every per-code filter runs before aggregation."""
        suppress = _err(code="0x400E", attr=0x05000000, module=0x05, full_code="050000000000400E")
        read_fail = _err(code="0x10081", attr=0x07002000, module=0x07, full_code="0700200000010081")
        storage = _err(code="0x30004", attr=0x05000100, module=0x05, full_code="0500010000030004")
        ordinary = _hms(_MAIN_BOARD)  # 0700_4025 on attr 0x07002000

        with _Harness() as h:
            # Only the read_fail code answers a commanded discovery read.
            h.suppress_read_failure = lambda pid, attr, code: attr == 0x07002000 and code == 0x10081
            await main_module.on_printer_status_change(5, _state([suppress, read_fail, storage, ordinary]))

        assert h.notify.on_printer_error.await_count == 1
        body = h.sent_bodies[0]
        assert "0700_4025" in body
        assert "0500_400E" not in body
        assert "0700_0081" not in body
        assert "0500_0004" not in body
        # Only the surviving ordinary code is recorded; the three filtered ones are not.
        assert h.ledger_keys == [notify_dedup.hms_ledger_key(5, _MAIN_BOARD.full_code)]


@pytest.mark.asyncio
class TestRecoveryOwnedSuppression:
    async def test_recovery_owned_suppression(self):
        """When recovery will own the incident, the raw feed-fault + slot-attributed
        companion alerts are suppressed (only the unrelated code notifies), yet ALL
        three full codes are stamped so a standing owned code can't re-blast."""
        with (
            _Harness() as h,
            patch("backend.app.services.spool_recovery.will_own", new=AsyncMock(return_value=True)) as will_own,
            patch("backend.app.services.spool_recovery.on_feed_fault_hms", new=AsyncMock()),
        ):
            await main_module.on_printer_status_change(5, _state([_FEED_FAULT, _RUNOUT_COMPANION, _UNRELATED]))

        will_own.assert_awaited_once()
        assert h.notify.on_printer_error.await_count == 1
        body = h.sent_bodies[0]
        assert "0700_4025" in body
        assert "0700_8010" not in body
        assert "0700_0001" not in body
        # Every code — suppressed-as-owned AND notified — is durably recorded.
        assert set(h.ledger_keys) == {
            notify_dedup.hms_ledger_key(5, code)
            for code in (_FEED_FAULT.full_code, _RUNOUT_COMPANION.full_code, _UNRELATED.full_code)
        }

    async def test_suppression_fails_open_when_predicate_returns_false(self):
        with (
            _Harness() as h,
            patch("backend.app.services.spool_recovery.will_own", new=AsyncMock(return_value=False)),
            patch("backend.app.services.spool_recovery.on_feed_fault_hms", new=AsyncMock()),
        ):
            await main_module.on_printer_status_change(5, _state([_FEED_FAULT, _RUNOUT_COMPANION, _UNRELATED]))

        assert h.notify.on_printer_error.await_count == 1
        body = h.sent_bodies[0]
        assert "0700_8010" in body
        assert "0700_0001" in body
        assert "0700_4025" in body

    async def test_suppression_fails_open_when_predicate_raises(self):
        """will_own already fails closed; the call-site guard is belt-and-braces so a
        crashed predicate never silences the raw alerts."""
        with (
            _Harness() as h,
            patch("backend.app.services.spool_recovery.will_own", new=AsyncMock(side_effect=RuntimeError("boom"))),
            patch("backend.app.services.spool_recovery.on_feed_fault_hms", new=AsyncMock()),
        ):
            await main_module.on_printer_status_change(5, _state([_FEED_FAULT, _RUNOUT_COMPANION, _UNRELATED]))

        assert h.notify.on_printer_error.await_count == 1
        body = h.sent_bodies[0]
        assert "0700_8010" in body
        assert "0700_0001" in body
        assert "0700_4025" in body

    async def test_will_own_skipped_without_recoverable_codes(self):
        """No recoverable code in the push ⇒ the predicate is never awaited (no db
        work) and both ordinary codes still aggregate into one message."""
        with (
            _Harness() as h,
            patch("backend.app.services.spool_recovery.will_own", new=AsyncMock(return_value=True)) as will_own,
        ):
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL), _hms(_MAIN_BOARD)]))

        will_own.assert_not_awaited()
        assert h.notify.on_printer_error.await_count == 1
