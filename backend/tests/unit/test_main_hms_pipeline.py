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

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app import main as main_module
from backend.app.core.tasks import spawn_background_task
from backend.app.services import hms_edges, notify_dedup

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


# Strictly-increasing wire stamp, one tick per constructed push. ``hms_edges.note_push``
# consumes a frame only when ``hms_wire_at`` ADVANCES — the monotonic stamp bambu_mqtt
# writes whenever a push carries an ``hms`` list, an empty one included (a wire
# all-clear is evidence), and never on a local clear.
_WIRE_AT = [1000.0]


def _state(hms: list, layer_num: int = 0, tray_now: int = 255) -> SimpleNamespace:
    """Minimal PrinterState stub carrying HMS. ``layer_num`` varies the status key
    so consecutive pushes are not swallowed by the broadcast dedup."""
    _WIRE_AT[0] += 1.0
    return SimpleNamespace(
        connected=True,
        connection_epoch=1,
        disconnected_at=None,
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
        tray_now=tray_now,
        door_open=False,
        subtask_name="",
        subtask_id="",
        ams_filament_backup=None,
        hms_errors=list(hms),
        hms_wire_at=_WIRE_AT[0],
        sdcard=True,
        remaining_time=0,
        gcode_file="",
    )


async def _warm_up(printer_id: int, hms: list | None = None) -> None:
    """Spend the edge tracker's SEEDING frame for ``printer_id``.

    The first frame a process consumes per printer seeds without edging — that IS
    restart-replay suppression — so every case that wants an APPEARANCE must burn one
    frame first. Must be called INSIDE a ``_Harness`` (it drives the real callback), and
    BEFORE the frame under test is constructed: ``_state`` stamps ``hms_wire_at`` at
    construction time and the tracker only consumes a stamp that ADVANCED.
    """
    await main_module.on_printer_status_change(printer_id, _state(hms or [], layer_num=-1))


async def _drain_tasks() -> None:
    """Run the fire-and-forget tasks main spawned to completion.

    ``asyncio.sleep(0)`` yields exactly once — enough for a task that awaits nothing,
    but not for one that opens a session and queries (``apply_runout_edges``). Bounded
    so a hung task fails the case instead of the suite.
    """
    for _ in range(20):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
        if not pending:
            return
        await asyncio.wait(pending, timeout=1.0)


@pytest.fixture(autouse=True)
def _reset_state():
    notify_dedup._reset_state()
    hms_edges._reset_state()  # the edge-triggered state consumers ride this ledger
    main_module._printer_offline_edge_at.clear()
    main_module._printer_reconciled_epoch.clear()
    main_module._last_status_broadcast.clear()
    yield
    notify_dedup._reset_state()
    hms_edges._reset_state()
    main_module._printer_offline_edge_at.clear()
    main_module._printer_reconciled_epoch.clear()
    main_module._last_status_broadcast.clear()


class _Harness:
    """Patches every side effect around the HMS pipeline and records its calls.

    ``session_factory`` swaps the mocked ``async_session`` for a real one (an
    ``async_sessionmaker`` bound to a test engine) so a case can assert on rows the
    pipeline actually wrote — used by ``test_hms_event.py`` for the durable HMS
    vocabulary. ``catalog`` maps ``full_code -> description``; the default (None) keeps
    the blanket "the vendored catalog knows every code" stub the older cases were
    written against, while a dict lets a case model an UNDESCRIBED code (the shape the
    vocabulary table exists for).
    """

    def __init__(self, *, session_factory=None, catalog: dict[str, str] | None = None):
        self.session_factory = session_factory
        self.catalog = catalog
        self.notify = MagicMock()
        self.notify.on_printer_error = AsyncMock()
        self.record_sent = AsyncMock()
        self.seed_standing = AsyncMock(return_value=set())
        self.suppress_read_failure = False
        # The runout guidance-refresh hook (006-H2S 2026-07-26) runs on every push
        # that carries new codes and opens its OWN session, so it is stubbed here
        # for the whole file; its behavior is pinned in test_spool_recovery.py.
        self.guidance_refresh = AsyncMock(return_value=False)
        # Names of the background tasks main spawned during the block, in order.
        self.spawned: list[str] = []

    def _spawn(self, coro, *, name=None):
        """Record and REALLY spawn — ``main``'s fire-and-forget hooks must still run.

        This used to be a blanket no-op stub, which was fine while the HMS hooks used a
        bare ``asyncio.create_task``: stubbing the helper suppressed only the unrelated
        reconnect lane. Since the runout / storage-low / backup-swap hooks moved onto
        ``core.tasks.spawn_background_task`` (strong refs — a weakly-held stamp task can
        be collected mid-await, one of the ways spent stamps died silently), a no-op stub
        here would swallow the very lanes this file pins, and every liveness case below
        would pass by proving nothing. The unrelated lanes are stood down individually.
        """
        self.spawned.append(name or "")
        return spawn_background_task(coro, name=name)

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

        if self.session_factory is not None:
            session_patch = patch("backend.app.main.async_session", self.session_factory)
        else:
            session_patch = patch("backend.app.main.async_session", return_value=session_cm)
        if self.catalog is not None:
            catalog = dict(self.catalog)
            catalog_patch = patch(
                "backend.app.services.hms_catalog.lookup_full_code",
                side_effect=lambda full_code: catalog.get(full_code),
            )
        else:
            catalog_patch = patch(
                "backend.app.services.hms_catalog.lookup_full_code", return_value="Failed to read the filament"
            )

        self._patches = [
            patch("backend.app.main.ws_manager", ws),
            patch("backend.app.main.mqtt_relay", relay),
            patch("backend.app.main.printer_manager", pm),
            patch("backend.app.main.spawn_background_task", new=self._spawn),
            # The one unrelated lane the old blanket stub was suppressing: the
            # once-per-reconnect archive reconcile. It has nothing to do with HMS and
            # would query through main's mocked session, so it is stood down by name.
            patch("backend.app.main.reconcile_stale_active_prints", new=AsyncMock(return_value=0)),
            patch("backend.app.main.printer_state_to_dict", return_value={}),
            session_patch,
            patch("backend.app.main.notification_service", self.notify),
            patch("backend.app.main._capture_snapshot_for_notification", new=AsyncMock(return_value=None)),
            catalog_patch,
            patch(
                "backend.app.services.ams_presence.is_expected_read_failure",
                side_effect=self._is_expected_read_failure,
            ),
            patch.object(notify_dedup, "record_sent", self.record_sent),
            patch.object(notify_dedup, "seed_standing", self.seed_standing),
            patch(
                "backend.app.services.spool_recovery.maybe_refresh_runout_guidance",
                new=self.guidance_refresh,
            ),
            # The fire-and-forget services main spawns have no session to borrow and
            # open their own from ``core.database`` (read at call time). Left alone that
            # name is the APPLICATION's engine, so a spawned task would reach a real
            # database from a unit test; stub it with the same mock main gets. A case
            # that wants one of those lanes on a real engine re-patches it on top.
            patch("backend.app.core.database.async_session", return_value=session_cm),
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
            patch("backend.app.services.spool_recovery.on_ams_fault", new=AsyncMock()),
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
            patch("backend.app.services.spool_recovery.on_ams_fault", new=AsyncMock()),
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
            patch("backend.app.services.spool_recovery.on_ams_fault", new=AsyncMock()),
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


@pytest.mark.asyncio
class TestRunoutGuidanceRefreshHook:
    """006-H2S 2026-07-26 wiring pin: the HMS pipeline hands the guidance-refresh
    lane the codes that APPEARED on this frame's wire-HMS edge plus the live state, and
    a hook failure can never break the status flow (invariant 10). The lane's own
    decisions (including its `_guidance_sent` dedup) are pinned in
    test_spool_recovery.py."""

    async def test_hook_receives_the_appeared_full_codes_and_state(self):
        with _Harness() as h:
            await _warm_up(5)
            state = _state([_RUNOUT_COMPANION])
            await main_module.on_printer_status_change(5, state)

        h.guidance_refresh.assert_awaited_once()
        args = h.guidance_refresh.await_args.args
        assert args[0] == 5
        assert args[1] == {_RUNOUT_COMPANION.full_code}
        assert args[2] is state

    async def test_the_seeding_frame_never_invokes_the_hook(self):
        """A code live on the first frame the process consumes is a restart replay —
        it seeds instead of edging, so nothing downstream fires."""
        with _Harness() as h:
            await main_module.on_printer_status_change(5, _state([_RUNOUT_COMPANION]))

        h.guidance_refresh.assert_not_awaited()

    async def test_a_standing_code_does_not_re_invoke_the_hook(self):
        """The hook rides the APPEARANCE edge, so a code standing across later frames
        is one continuing observation and does not re-trigger it."""
        with _Harness() as h:
            await _warm_up(5)
            await main_module.on_printer_status_change(5, _state([_RUNOUT_COMPANION], layer_num=1))
            await main_module.on_printer_status_change(5, _state([_RUNOUT_COMPANION], layer_num=2))

        assert h.guidance_refresh.await_count == 1

    async def test_hook_failure_does_not_break_the_status_flow(self):
        with _Harness() as h:
            h.guidance_refresh.side_effect = RuntimeError("boom")
            await _warm_up(5)
            await main_module.on_printer_status_change(5, _state([_RUNOUT_COMPANION, _UNRELATED]))

        # The push completed: the ordinary code still notified and still stamped.
        assert h.notify.on_printer_error.await_count == 1
        assert "0700_4025" in h.sent_bodies[0]


# The firmware's "ran out and automatically switched" statement: code word 0x00030002
# on attr 0x07002200 (AMS0 slot 3 → tray 2). Its short code is "0700_0002", which the
# pipeline must NEVER match as a bare string — the slot byte and the code-word high
# bits that separate it from the demand both live outside the short code.
_AUTO_SWITCH = _err(code="0x30002", attr=0x07002200, module=0x07, full_code="0700220000030002")


@pytest.mark.asyncio
class TestSlotRunoutSpentHook:
    """Wiring pin for Lane B: an APPEARANCE edge spawns ``apply_runout_edges``, which
    builds the auto-switch spent events (full_code + attr + code word per entry, so
    chained multi-slot runouts survive the hand-off) and hands them to the trigger in
    its own session. The trigger's own decisions are pinned in test_spool_respool.py."""

    @pytest.fixture(autouse=True)
    def _reset_respool_state(self):
        from backend.app.services import spool_respool

        spool_respool._reset_state()
        yield
        spool_respool._reset_state()

    async def test_an_appearing_auto_switch_code_invokes_the_hook_with_the_event_tuple(self):
        with (
            _Harness(),
            patch(
                "backend.app.services.spool_respool.mark_spent_on_slot_runout",
                new=AsyncMock(return_value=[]),
            ) as hook,
        ):
            await _warm_up(5)
            state = _state([_AUTO_SWITCH])
            await main_module.on_printer_status_change(5, state)
            await _drain_tasks()

        hook.assert_awaited_once()
        args = hook.await_args.args
        assert args[1] == 5
        assert args[2] == [(_AUTO_SWITCH.full_code, 0x07002200, 0x00030002)]
        assert args[3] is state

    async def test_a_code_live_on_the_seeding_frame_never_reaches_the_hook(self):
        """Restart replay: the first frame the process consumes seeds, so a code already
        standing there can never stamp the roll swapped in during the downtime."""
        with (
            _Harness(),
            patch(
                "backend.app.services.spool_respool.mark_spent_on_slot_runout",
                new=AsyncMock(return_value=[]),
            ) as hook,
        ):
            await main_module.on_printer_status_change(5, _state([_AUTO_SWITCH]))
            await _drain_tasks()

        hook.assert_not_awaited()

    async def test_demand_family_codes_never_reach_the_hook(self):
        """Only the auto-switch code word is spent evidence. The bare demand decodes a
        slot just as well, so the lane filter is the only thing keeping a
        firmware-latched bogus demand (006-H2S) from archiving a healthy roll."""
        with (
            _Harness(),
            patch(
                "backend.app.services.spool_respool.mark_spent_on_slot_runout",
                new=AsyncMock(return_value=[]),
            ) as hook,
        ):
            await _warm_up(5)
            await main_module.on_printer_status_change(5, _state([_RUNOUT_COMPANION]))
            await _drain_tasks()

        hook.assert_not_awaited()

    async def test_a_standing_code_does_not_re_invoke_the_hook(self):
        """The hook rides the wire-HMS APPEARANCE edge, so the auto-switch code standing
        across later frames is one observation — it must not re-stamp whatever is bound
        now."""
        with (
            _Harness(),
            patch(
                "backend.app.services.spool_respool.mark_spent_on_slot_runout",
                new=AsyncMock(return_value=[]),
            ) as hook,
        ):
            await _warm_up(5)
            await main_module.on_printer_status_change(5, _state([_AUTO_SWITCH], layer_num=1))
            await main_module.on_printer_status_change(5, _state([_AUTO_SWITCH], layer_num=2))
            await _drain_tasks()

        assert hook.await_count == 1

    async def test_hook_failure_does_not_break_the_status_flow(self):
        """The spawn site's guard (invariant 10). ``apply_runout_edges`` carries its own
        whole-body guard as a fire-and-forget task; this pins the call site for the case
        it cannot cover — a failure raised before the task is ever scheduled."""
        with (
            _Harness() as h,
            patch(
                "backend.app.services.spool_respool.apply_runout_edges",
                new=MagicMock(side_effect=RuntimeError("boom")),
            ),
        ):
            await _warm_up(5)
            await main_module.on_printer_status_change(5, _state([_AUTO_SWITCH, _UNRELATED]))

        # The push completed: the ordinary code still notified and still stamped.
        assert h.notify.on_printer_error.await_count == 1
        assert "0700_4025" in h.sent_bodies[0]


@pytest.mark.asyncio
class TestStandingCodesStillReachTheIncidentMachine:
    """THE silent-class reproduction (WS2b finding (b)).

    The recovery spawn used to be nested inside ``if new_error_codes:`` — the
    NOTIFICATION dedup's edge. Two everyday shapes therefore never reached it, and
    produced no incident, no alert and not even a log line (9 runout episodes):

      * a code STANDING at restart — ``notify_dedup.seed_standing`` pre-marks every
        live code as already-seen so a deploy does not re-blast, which also emptied
        ``new_error_codes`` on the first push;
      * a code FLAPPING inside the 600 s re-notify window — one continuing incident
        for notification purposes, but a fresh physical fault for the machine.

    The spawn is now derived from the LIVE hms list and fires per push. Running these
    cases against the pre-WS2b pipeline produced zero calls (reference only — the old
    code is deleted, so the contrast is documented rather than executed).
    """

    async def test_a_code_standing_since_restart_still_spawns(self):
        import asyncio

        runout = SimpleNamespace(code="0x8011", attr=0x07000000, module=0x07, severity=2, full_code="0700000000008011")
        with (
            _Harness() as h,
            patch("backend.app.services.spool_recovery.will_own", new=AsyncMock(return_value=True)),
            patch("backend.app.services.spool_recovery.on_ams_fault", new=AsyncMock()) as spawn,
        ):
            # Exactly what startup does: the live code is pre-marked as already
            # alerted, so it is NOT new on this push.
            h.seed_standing.return_value = {runout.full_code}
            notify_dedup.new_codes(5, {runout.full_code}, 1000.0)

            await main_module.on_printer_status_change(5, _state([_hms(runout)], layer_num=3))
            await asyncio.sleep(0)  # let the fire-and-forget task run

        assert spawn.await_count == 1  # the incident machine was reached
        assert h.notify.on_printer_error.await_count == 0  # ...and nothing re-blasted

    async def test_a_code_flapping_inside_the_renotify_window_still_spawns(self):
        import asyncio

        runout = SimpleNamespace(code="0x8011", attr=0x07000000, module=0x07, severity=2, full_code="0700000000008011")
        with (
            _Harness(),
            patch("backend.app.services.spool_recovery.will_own", new=AsyncMock(return_value=True)),
            patch("backend.app.services.spool_recovery.on_ams_fault", new=AsyncMock()) as spawn,
        ):
            await main_module.on_printer_status_change(5, _state([_hms(runout)], layer_num=1))
            await asyncio.sleep(0)
            # It clears and comes back well inside the 600 s window: one incident for
            # notification, a live fault for the machine on EVERY push.
            await main_module.on_printer_status_change(5, _state([], layer_num=2))
            await main_module.on_printer_status_change(5, _state([_hms(runout)], layer_num=3))
            await asyncio.sleep(0)

        assert spawn.await_count == 2

    async def test_no_actionable_code_never_spawns(self):
        """The gate is the TAXONOMY, not "any HMS": an RFID-read failure is not an
        incident and must not open a hold nobody can clear."""
        import asyncio

        with (
            _Harness(),
            patch("backend.app.services.spool_recovery.on_ams_fault", new=AsyncMock()) as spawn,
        ):
            await main_module.on_printer_status_change(5, _state([_hms(_READ_FAIL), _hms(_MAIN_BOARD)]))
            await asyncio.sleep(0)

        spawn.assert_not_awaited()

    async def test_the_wire_sampler_runs_on_every_push_even_with_no_hms(self):
        """The sampler owns the "printer is running again" close and the demand-clear
        resume — both are edges where hms is EMPTY, so it must not sit inside the
        HMS branch."""
        with (
            _Harness(),
            patch("backend.app.services.spool_recovery.note_demand_watch") as sampler,
        ):
            await main_module.on_printer_status_change(5, _state([], layer_num=7))

        sampler.assert_called_once()


@pytest.mark.asyncio
class TestAutoSwitchNotificationSuppression:
    """C5: the firmware's runout auto-switch statement stops paging the operator.

    ``0x00030002`` is the AMS backup REPORTING a rescue it already completed — the
    print never stopped and nothing is asked for. The firmware sends it at severity 3
    (common), which sits inside the notify band, so it produced 87 alerts in 14 days.
    Everything else it drives is untouched: it is still THE spent evidence, and it is
    still recorded in the durable HMS vocabulary.
    """

    async def test_the_auto_switch_never_notifies(self):
        with _Harness() as h:
            await main_module.on_printer_status_change(5, _state([_hms(_AUTO_SWITCH)]))

        h.notify.on_printer_error.assert_not_awaited()

    async def test_a_suppressed_alert_is_not_ledger_stamped(self):
        """An alert nobody sent is not an operator who was told — the same rule the
        discovery-read suppression follows."""
        with _Harness() as h:
            await main_module.on_printer_status_change(5, _state([_hms(_AUTO_SWITCH)]))

        h.record_sent.assert_not_awaited()

    async def test_the_spent_hook_still_receives_it(self):
        """Suppressing a notification is not un-consuming the evidence."""
        with (
            _Harness(),
            patch(
                "backend.app.services.spool_respool.mark_spent_on_slot_runout",
                new=AsyncMock(return_value=[]),
            ) as hook,
        ):
            await _warm_up(5)
            await main_module.on_printer_status_change(5, _state([_hms(_AUTO_SWITCH)]))
            await _drain_tasks()

        hook.assert_awaited_once()
        assert hook.await_args.args[2] == [(_AUTO_SWITCH.full_code, 0x07002200, 0x00030002)]

    async def test_the_durable_vocabulary_still_records_it(self):
        """The hms_event write is upstream of the notify filter, so the code stays
        queryable however loudly (or quietly) it is reported."""
        import backend.app.main as m

        m._hms_event_written_at.clear()
        with _Harness(), patch("backend.app.models.hms_event.HMSEvent"):
            await main_module.on_printer_status_change(5, _state([_hms(_AUTO_SWITCH)]))

        assert (5, _AUTO_SWITCH.full_code) in m._hms_event_written_at

    async def test_the_assist_motor_overload_sharing_the_short_form_still_notifies(self):
        """THE reason suppression is keyed by CODE WORD: 0x00030002 masks to the short
        form ``0700_0002``, and so does the assist-motor overload 0x00020002 — the
        swap trigger's own 16-hex twin, which must keep alerting."""
        # Attr submodule 0x10 = an assist/feeder motor, which is what scopes this code
        # word to "the AMS A assist motor is overloaded" — a live swap trigger.
        overload = _err(code="0x20002", attr=0x07001000, module=0x07, full_code="0700100000020002")
        with (
            _Harness() as h,
            patch("backend.app.services.spool_recovery.will_own", new=AsyncMock(return_value=False)),
            patch("backend.app.services.spool_recovery.on_ams_fault", new=AsyncMock()),
        ):
            await main_module.on_printer_status_change(5, _state([_hms(overload)]))

        assert h.notify.on_printer_error.await_count == 1


# --- the edge-triggered STATE consumers (W1c) --------------------------------
# Five consumers used to ride ``new_error_codes`` — notify_dedup's 600 s re-NOTIFY
# window. That window is a level-triggered ALERT policy: it deliberately calls a code
# flapping out-and-back inside it ONE continuing incident, and pre-marks every code
# standing at restart as already-seen. Right for paging a human, wrong for a state
# decision. They now ride the wire-HMS APPEARANCE edge instead; these pin that the
# switch actually happens — an edge fires them even when notify_dedup calls the code
# stale, which is exactly the shape that was silently dropping them.

# A native plate-occupancy code (H2-series pre-print vision check), and the USB
# storage-low code — the two membership-tested consumers.
_PLATE_OCCUPANCY = _err(code="0x808C", attr=0x05000000, module=0x05, full_code="050000000000808C")
_STORAGE_LOW = _err(code="0x30004", attr=0x05000100, module=0x05, full_code="0500010000030004")


async def _stale_then_reappear(printer_id: int, err: SimpleNamespace) -> None:
    """Drive the shape the old ``new_error_codes`` gate could not see.

    The code is pre-marked in notify_dedup (so it is NOT "new" for the alert lane on
    either push), then a wire ALL-CLEAR frame seeds the edge tracker and the code
    REAPPEARS. Only the appearance edge can fire on that.
    """
    import time as _time

    notify_dedup.new_codes(printer_id, {err.full_code}, _time.time())
    await main_module.on_printer_status_change(printer_id, _state([], layer_num=-1))
    await main_module.on_printer_status_change(printer_id, _state([err], layer_num=1))


@pytest.mark.asyncio
class TestMovedConsumersRideTheAppearanceEdge:
    async def test_plate_occupancy_fires_on_an_edge_notify_dedup_calls_stale(self):
        # The vision edge now drives ``pause_recovery.on_plate_vision_trip`` (the lane
        # that records the trip and STOPS the print); ``on_native_plate_detection`` is
        # deleted. It is SPAWNED rather than awaited — the lane sends a stop and can
        # sleep for its retry, and the ~1 Hz status flow must not wait on either.
        with (
            _Harness() as h,
            patch("backend.app.services.pause_recovery.on_plate_vision_trip", new=AsyncMock()) as plate_hook,
        ):
            await _stale_then_reappear(5, _PLATE_OCCUPANCY)
            await asyncio.sleep(0)  # let the fire-and-forget task run

        plate_hook.assert_awaited_once()
        assert plate_hook.await_args.args == (5, {"0500_808C"})
        # ...and the alert lane genuinely considered the code stale on that push.
        h.notify.on_printer_error.assert_not_awaited()

    async def test_storage_low_fires_on_an_edge_notify_dedup_calls_stale(self):
        with (
            _Harness() as h,
            patch("backend.app.main.on_storage_low", new=AsyncMock()) as storage_hook,
        ):
            await _stale_then_reappear(5, _STORAGE_LOW)
            await asyncio.sleep(0)  # let the fire-and-forget task run

        storage_hook.assert_awaited_once()
        assert storage_hook.await_args.args == (5, {_STORAGE_LOW.full_code})
        h.notify.on_printer_error.assert_not_awaited()

    async def test_guidance_refresh_fires_on_an_edge_notify_dedup_calls_stale(self):
        with _Harness() as h:
            await _stale_then_reappear(5, _RUNOUT_COMPANION)

        h.guidance_refresh.assert_awaited_once()
        assert h.guidance_refresh.await_args.args[1] == {_RUNOUT_COMPANION.full_code}
        h.notify.on_printer_error.assert_not_awaited()


@pytest.mark.asyncio
class TestStatusHooksHoldAStrongTaskReference:
    """The status callback's fire-and-forget hooks go through ``core.tasks``, never a
    bare ``asyncio.create_task``.

    asyncio holds only a WEAK reference to a task whose handle the caller discards, so a
    spent-stamp task can be collected mid-await or dropped at loop shutdown and leave no
    trace at all — indistinguishable from a lane that never fired, which is precisely the
    failure mode the 2026-08-13 investigation spent three days on. The task NAME is the
    other half: an uncaught exception surfaces under it, so it is part of the contract,
    not decoration.
    """

    async def test_the_runout_edge_hook_is_spawned_by_name(self):
        with (
            _Harness() as h,
            patch("backend.app.services.spool_respool.apply_runout_edges", new=AsyncMock()) as hook,
        ):
            await _warm_up(5)
            await main_module.on_printer_status_change(5, _state([_AUTO_SWITCH]))
            await _drain_tasks()

        hook.assert_awaited_once()
        assert "runout-edges-p5" in h.spawned

    async def test_the_storage_low_hook_is_spawned_by_name(self):
        with (
            _Harness() as h,
            patch("backend.app.main.on_storage_low", new=AsyncMock()) as storage_hook,
        ):
            await _warm_up(5)
            await main_module.on_printer_status_change(5, _state([_hms(_STORAGE_LOW)]))
            await _drain_tasks()

        storage_hook.assert_awaited_once()
        assert "storage-low-p5" in h.spawned


# --- liveness: the stampers still WRITE ---------------------------------------
# A cured storm and a starved lane are indistinguishable on absence metrics, so every
# gating change here is paired with a probe that the thing still HAPPENS. These drive
# the real callback against a real engine and assert the DB row.


@pytest.fixture
def sessions(test_engine):
    """A real session factory for the pipeline, bound to the test engine."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def _seat_spool(db, printer_id: int, ams_id: int, tray_id: int):
    """A full spool assigned to one AMS slot of ``printer_id``."""
    from backend.app.models.spool import Spool
    from backend.app.models.spool_assignment import SpoolAssignment

    spool = Spool(material="PETG", label_weight=1000, core_weight=250, weight_used=950)
    spool.k_profiles = []
    spool.assignments = []
    db.add(spool)
    await db.flush()
    db.add(SpoolAssignment(spool_id=spool.id, printer_id=printer_id, ams_id=ams_id, tray_id=tray_id))
    await db.commit()
    return spool


# The unrescued runout ("insert filament", print held): names its AMS unit in the attr
# but no slot, so Lane A resolves the exhausted tray from the live feeder.
_UNRESCUED_RUNOUT = _err(code="0x8011", attr=0x07000000, module=0x07, full_code="0700000000008011")


def _runout_lane_on(session_factory):
    """Give ONLY the runout lane a real database, and stand the rest of the block down.

    ``apply_runout_edges`` opens its own session (``core.database.async_session``, read
    at call time) because ``main`` fires it with none to borrow, so that is the name to
    point at the test engine. ``main``'s own sessions stay mocked by ``_Harness`` and the
    incident machine is stubbed out — deliberately, and not only for isolation: the test
    engine is one in-memory SQLite database behind a StaticPool, i.e. a SINGLE shared
    connection, so a second live session interleaving with the stamp's transaction
    silently swallows the write. Production gives every session its own connection.
    """
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("backend.app.core.database.async_session", session_factory))
    stack.enter_context(patch("backend.app.services.spool_recovery.will_own", new=AsyncMock(return_value=False)))
    stack.enter_context(patch("backend.app.services.spool_recovery.on_ams_fault", new=AsyncMock()))
    return stack


@pytest.mark.asyncio
class TestSpentStampingStillHappensEndToEnd:
    @pytest.fixture(autouse=True)
    def _reset_respool_state(self):
        from backend.app.services import spool_respool

        spool_respool._reset_state()
        yield
        spool_respool._reset_state()

    async def test_lane_a_unrescued_runout_stamps_the_feeding_slot(self, db_session, printer_factory, sessions):
        """The unrescued vocabulary, plain case: the runout appears, the spool feeding
        the slot gets stamped, and the row is really written."""
        printer = await printer_factory()
        spool = await _seat_spool(db_session, printer.id, 0, 0)

        with _Harness(), _runout_lane_on(sessions):
            await _warm_up(printer.id)
            await main_module.on_printer_status_change(printer.id, _state([_UNRESCUED_RUNOUT], layer_num=1, tray_now=0))
            await _drain_tasks()

        await db_session.refresh(spool)
        assert spool.spent_at is not None

    async def test_lane_b_auto_switch_stamps_the_slot_the_firmware_named(self, db_session, printer_factory, sessions):
        """The firmware's own auto-switch statement, plain case: attr 0x07002200 names
        AMS0 slot 3 (tray 2), so that roll is stamped and the slot now FEEDING — which a
        tray_now inference would have picked — stays untouched."""
        printer = await printer_factory()
        exhausted = await _seat_spool(db_session, printer.id, 0, 2)
        feeding = await _seat_spool(db_session, printer.id, 0, 0)

        with _Harness(), _runout_lane_on(sessions):
            await _warm_up(printer.id)
            await main_module.on_printer_status_change(
                printer.id, _state([_hms(_AUTO_SWITCH)], layer_num=1, tray_now=0)
            )
            await _drain_tasks()

        await db_session.refresh(exhausted)
        await db_session.refresh(feeding)
        assert exhausted.spent_at is not None
        assert feeding.spent_at is None
