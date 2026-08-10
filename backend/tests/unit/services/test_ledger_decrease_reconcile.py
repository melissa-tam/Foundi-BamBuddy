"""Tagged-ledger DECREASE reconcile (W6) — ``usage_tracker``.

The push-driven weight sync is INCREASE-ONLY, so a ledger that over-counts can
never heal itself. Production spool 37 (003-H2S T3) sat at 899.28 g used against a
wire-FULL roll: the grams were charged on 07-17 while the row was stale-bound to
another printer's tray, and the only bidirectional path was a manual tool nobody
could safely run. Doctrine rule 8 — for a TAGGED row the wire remain IS truth,
because the firmware read that roll's own RFID chip — so the contradiction is
repaired automatically, loudly, and only when it is unmistakable.

Every test below defends one gate. The point of the gate set is that an automatic
write to the gram ledger must be impossible to trigger by accident.
"""

from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from backend.app.models.spool import Spool
from backend.app.services import spool_respool, usage_tracker
from backend.app.services.usage_tracker import (
    _LEDGER_DECREASE_STABLE_S,
    _reset_ledger_decrease_state,
    clear_ledger_decrease_window,
    maybe_reconcile_tagged_ledger_decrease,
)

TAG_UID = "AABBCCDD11223344"
TRAY_UUID = "AABBCCDD11223344AABBCCDD11223344"

# The prod acceptance case: spool 37, label 1000 g, ledger 899.28 g used (≈10 %
# remaining) while the tray's own chip reads 100 % → a ~90-point contradiction.
SPOOL_37_USED = 899.28


@pytest.fixture(autouse=True)
def _clean_state():
    _reset_ledger_decrease_state()
    spool_respool._reset_state()
    yield
    _reset_ledger_decrease_state()
    spool_respool._reset_state()


@pytest.fixture
def clock(monkeypatch):
    """Drives the corroboration window without wall-clock waits."""
    now = {"t": 1000.0}
    monkeypatch.setattr(usage_tracker, "monotonic", lambda: now["t"])
    return now


@pytest.fixture
def notify(monkeypatch):
    from backend.app.services.notification_service import notification_service

    spy = AsyncMock()
    monkeypatch.setattr(notification_service, "on_printer_error", spy)
    return spy


@pytest.fixture
def trusted(monkeypatch):
    """Default the wire-trust gate to "trustworthy" (no identify, no drying)."""
    monkeypatch.setattr(spool_respool, "remain_reading_untrustworthy", lambda *a: False)


def _tray(remain=100, tag_uid=TAG_UID, tray_uuid=TRAY_UUID):
    return {
        "id": 3,
        "state": 11,
        "tray_type": "PETG",
        "tray_color": "000000FF",
        "tray_info_idx": "GFG99",
        "tag_uid": tag_uid,
        "tray_uuid": tray_uuid,
        "remain": remain,
    }


async def _seed_spool(db, *, weight_used=SPOOL_37_USED, tagged=True, **kw):
    spool = Spool(
        material="PETG",
        rgba="000000FF",
        label_weight=1000,
        weight_used=weight_used,
        tag_uid=TAG_UID if tagged else None,
        tray_uuid=TRAY_UUID if tagged else None,
        **kw,
    )
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


async def _push(db, spool, *, tray=None, sync_allowed=True, slot=(1, 0, 3)):
    printer_id, ams_id, tray_id = slot
    return await maybe_reconcile_tagged_ledger_decrease(
        db,
        printer_id,
        ams_id,
        tray_id,
        tray if tray is not None else _tray(),
        spool,
        sync_allowed=sync_allowed,
    )


async def _two_stable_pushes(db, spool, clock, **kw):
    """The minimum corroboration: two pushes spanning the stability window."""
    first = await _push(db, spool, **kw)
    clock["t"] += _LEDGER_DECREASE_STABLE_S
    second = await _push(db, spool, **kw)
    return first, second


@pytest.mark.asyncio
class TestTheProdCase:
    async def test_spool_37_reconciles_after_two_stable_pushes(self, db_session, clock, notify, trusted, caplog):
        """Label 1000 g, ledger 899.28 g used, wire remain 100 % → weight_used 0.0.

        One push is deliberately not enough: the AMS re-reports a tray on every
        state change, so the contradiction must HOLD.
        """
        spool = await _seed_spool(db_session)

        with caplog.at_level("WARNING", logger="backend.app.services.usage_tracker"):
            first, second = await _two_stable_pushes(db_session, spool, clock)

        assert first is False  # corroborating, not yet acting
        assert second is True
        assert spool.weight_used == 0.0

        warning = "\n".join(r.getMessage() for r in caplog.records if r.levelname == "WARNING")
        assert "[ledger-reconcile]" in warning
        assert "weight_used 899.3 -> 0.0" in warning
        assert "wire remain 100% contradicts ledger by 90 pts" in warning
        assert "doctrine rule 8" in warning

    async def test_operator_is_notified(self, db_session, clock, notify, trusted):
        """A silent ledger rewrite is exactly what nobody should ship — it rides the
        existing AMS-issue notification channel."""
        spool = await _seed_spool(db_session)
        await _two_stable_pushes(db_session, spool, clock)

        notify.assert_awaited_once()
        args = notify.await_args.args
        assert args[2] == "Spool ledger corrected"
        detail = args[4]
        assert f"Spool #{spool.id}" in detail
        assert "100% full on the wire" in detail
        assert "899.3 g used" in detail

    async def test_partial_correction_for_a_half_used_roll(self, db_session, clock, notify, trusted):
        """The write targets the WIRE value, not zero: a 60 % roll against a
        950 g-used ledger reconciles to 400 g used."""
        spool = await _seed_spool(db_session, weight_used=950.0)
        await _two_stable_pushes(db_session, spool, clock, tray=_tray(remain=60))
        assert spool.weight_used == 400.0

    async def test_notification_failure_never_undoes_the_write(self, db_session, clock, trusted, monkeypatch):
        from backend.app.services.notification_service import notification_service

        monkeypatch.setattr(notification_service, "on_printer_error", AsyncMock(side_effect=RuntimeError("smtp")))
        spool = await _seed_spool(db_session)

        _, second = await _two_stable_pushes(db_session, spool, clock)

        assert second is True
        assert spool.weight_used == 0.0


@pytest.mark.asyncio
class TestCorroboration:
    async def test_one_push_never_writes(self, db_session, clock, notify, trusted):
        spool = await _seed_spool(db_session)
        assert await _push(db_session, spool) is False
        assert spool.weight_used == SPOOL_37_USED

    async def test_two_pushes_too_close_together_never_write(self, db_session, clock, notify, trusted):
        """Both the count AND the elapsed window must be satisfied — a burst of
        pushes inside one second is one observation, not two."""
        spool = await _seed_spool(db_session)
        await _push(db_session, spool)
        clock["t"] += _LEDGER_DECREASE_STABLE_S - 0.1
        assert await _push(db_session, spool) is False
        assert spool.weight_used == SPOOL_37_USED

    async def test_a_push_that_stops_contradicting_drops_the_window(self, db_session, clock, notify, trusted):
        """The condition must HOLD, not merely have happened once: one intervening
        agreeing push resets the corroboration to zero."""
        spool = await _seed_spool(db_session)
        await _push(db_session, spool)
        clock["t"] += _LEDGER_DECREASE_STABLE_S
        await _push(db_session, spool, tray=_tray(remain=5))  # ledger and wire agree
        assert await _push(db_session, spool) is False  # window restarted
        assert spool.weight_used == SPOOL_37_USED

    async def test_windows_are_per_slot(self, db_session, clock, notify, trusted):
        spool = await _seed_spool(db_session)
        await _push(db_session, spool, slot=(1, 0, 3))
        clock["t"] += _LEDGER_DECREASE_STABLE_S
        assert await _push(db_session, spool, slot=(1, 0, 2)) is False  # different slot, own window

    async def test_empty_edge_clears_the_window(self, db_session, clock, notify, trusted):
        """An emptied slot invalidates everything learned about the roll that was in
        it — otherwise a half-corroborated window carries across a roll swap and the
        NEXT roll's first qualifying push fires immediately."""
        spool = await _seed_spool(db_session)
        await _push(db_session, spool)
        clear_ledger_decrease_window(1, 0, 3)
        clock["t"] += _LEDGER_DECREASE_STABLE_S
        assert await _push(db_session, spool) is False
        assert spool.weight_used == SPOOL_37_USED


@pytest.mark.asyncio
class TestGates:
    async def test_tagless_row_is_never_reconciled(self, db_session, clock, notify, trusted):
        """A tagless row has no wire truth to defer to — the tray's remain% describes
        whatever roll the operator put there, not this ledger's history."""
        spool = await _seed_spool(db_session, tagged=False)
        await _two_stable_pushes(db_session, spool, clock)
        assert spool.weight_used == SPOOL_37_USED

    async def test_untagged_tray_is_never_reconciled(self, db_session, clock, notify, trusted):
        """Same rule from the other side: the LIVE tray must carry a valid tag."""
        spool = await _seed_spool(db_session)
        await _two_stable_pushes(db_session, spool, clock, tray=_tray(tag_uid="0" * 16, tray_uuid="0" * 32))
        assert spool.weight_used == SPOOL_37_USED

    async def test_weight_locked_row_is_never_reconciled(self, db_session, clock, notify, trusted):
        spool = await _seed_spool(db_session, weight_locked=True)
        await _two_stable_pushes(db_session, spool, clock)
        assert spool.weight_used == SPOOL_37_USED

    async def test_mid_print_is_never_reconciled(self, db_session, clock, notify, trusted):
        """``sync_allowed`` carries the caller's ams_weight_sync_allowed verdict: mid
        print the usage tracker owns the ledger and the wire lags a live extrusion."""
        spool = await _seed_spool(db_session)
        await _two_stable_pushes(db_session, spool, clock, sync_allowed=False)
        assert spool.weight_used == SPOOL_37_USED

    async def test_margin_below_threshold_is_never_reconciled(self, db_session, clock, notify, trusted):
        """49 points of disagreement is ordinary integer-remain noise plus ledger
        drift; only an unmistakable contradiction may rewrite grams."""
        spool = await _seed_spool(db_session, weight_used=490.0)  # ledger 51 %, wire 100 % → 49 pts
        await _two_stable_pushes(db_session, spool, clock)
        assert spool.weight_used == 490.0

    async def test_margin_exactly_at_threshold_reconciles(self, db_session, clock, notify, trusted):
        spool = await _seed_spool(db_session, weight_used=500.0)  # ledger 50 %, wire 100 % → 50 pts
        _, second = await _two_stable_pushes(db_session, spool, clock)
        assert second is True
        assert spool.weight_used == 0.0

    @pytest.mark.parametrize("remain", [0, -1, 101, None, "abc"])
    async def test_unusable_remain_is_never_reconciled(self, db_session, clock, notify, trusted, remain):
        """0 and -1 are firmware's "no reading" — the same rule the weight syncs use."""
        spool = await _seed_spool(db_session)
        await _two_stable_pushes(db_session, spool, clock, tray=_tray(remain=remain))
        assert spool.weight_used == SPOOL_37_USED

    async def test_untrustworthy_reading_neither_fires_nor_counts(self, db_session, clock, notify, monkeypatch):
        """A reading taken mid-identify or mid-drying is in flux: it must not write,
        and it must not advance the corroboration either."""
        blocked = {"v": True}
        monkeypatch.setattr(spool_respool, "remain_reading_untrustworthy", lambda *a: blocked["v"])
        spool = await _seed_spool(db_session)

        await _push(db_session, spool)
        clock["t"] += _LEDGER_DECREASE_STABLE_S
        assert await _push(db_session, spool) is False

        blocked["v"] = False  # trust restored — corroboration starts from scratch
        assert await _push(db_session, spool) is False
        clock["t"] += _LEDGER_DECREASE_STABLE_S
        assert await _push(db_session, spool) is True

    async def test_open_respool_prompt_defers(self, db_session, clock, notify, trusted):
        """The operator is already being asked whether this tag moved onto a fresh
        roll. Answering it automatically — in the wrong direction — is how a donor's
        history gets erased, so the lane stands aside."""
        spool = await _seed_spool(db_session)
        spool_respool._respool_prompt_dedup[1] = {(0, 3): (TAG_UID, TRAY_UUID)}

        await _two_stable_pushes(db_session, spool, clock)
        assert spool.weight_used == SPOOL_37_USED

        # Prompt resolved (the slot's empty edge clears the dedup) → lane resumes.
        spool_respool.clear_respool_prompt_dedup(1, 0, 3)
        _, second = await _two_stable_pushes(db_session, spool, clock)
        assert second is True
        assert spool.weight_used == 0.0

    async def test_wire_agreeing_or_higher_used_never_writes(self, db_session, clock, notify, trusted):
        """Increase direction only ever belongs to the increase-only sync; this lane
        writes DOWN or not at all."""
        spool = await _seed_spool(db_session, weight_used=0.0)
        await _two_stable_pushes(db_session, spool, clock, tray=_tray(remain=100))
        assert spool.weight_used == 0.0
        notify.assert_not_awaited()

    async def test_zero_label_weight_is_never_reconciled(self, db_session, clock, notify, trusted):
        spool = await _seed_spool(db_session, weight_used=500.0)
        spool.label_weight = 0
        await db_session.commit()
        await _two_stable_pushes(db_session, spool, clock)
        assert spool.weight_used == 500.0


class TestSharedArithmetic:
    """The margin is computed in ONE place (``spool_respool.remain_jump_margin``) so
    the re-spool trigger and this lane can never drift apart."""

    def test_margin_is_wire_minus_ledger_points(self):
        spool = Spool(material="PETG", label_weight=1000, weight_used=SPOOL_37_USED)
        assert spool_respool.remain_jump_margin(spool, _tray(remain=100)) == pytest.approx(89.928)

    def test_margin_is_none_when_inapplicable(self):
        spool = Spool(material="PETG", label_weight=1000, weight_used=0)
        assert spool_respool.remain_jump_margin(spool, _tray(remain=0)) is None
        assert spool_respool.remain_jump_margin(spool, _tray(tag_uid="0" * 16, tray_uuid="0" * 32)) is None

    def test_over_used_ledger_clamps_at_zero_remaining(self):
        """A negative ledger remaining is clamped, mirroring the re-spool reading —
        the margin can never exceed 100 points."""
        spool = Spool(material="PETG", label_weight=1000, weight_used=1850.0)
        assert spool_respool.remain_jump_margin(spool, _tray(remain=100)) == pytest.approx(100.0)

    def test_respool_trigger_still_reads_through_the_same_helper(self):
        """Behaviour pin for the extraction: the ≥30-point re-spool reading is
        exactly "margin computable and at or above the threshold"."""
        spool = Spool(material="PETG", label_weight=1000, weight_used=958.99)
        margin = spool_respool.remain_jump_margin(spool, _tray(remain=100))
        assert margin >= spool_respool._RESPOOL_REMAIN_JUMP_PCT
        assert spool_respool._remain_jump_reading(spool, _tray(remain=100)) is True


class TestPromptAccessor:
    def test_open_only_while_the_dedup_holds_the_slot(self):
        assert spool_respool.respool_prompt_open_for_slot(7, 0, 1) is False
        spool_respool._respool_prompt_dedup[7] = {(0, 1): (TAG_UID, TRAY_UUID)}
        assert spool_respool.respool_prompt_open_for_slot(7, 0, 1) is True
        assert spool_respool.respool_prompt_open_for_slot(7, 0, 2) is False  # per slot
        spool_respool.clear_respool_prompt_dedup(7, 0, 1)
        assert spool_respool.respool_prompt_open_for_slot(7, 0, 1) is False

    def test_a_tier3_observation_is_not_an_open_prompt(self):
        """The 2026-08-10 demotion turned tier 3 into a log line, and its dedup is a
        SEPARATE dict for two reasons that both land here. An entry in the prompt
        record would resurrect the retired modal through the reconnect replay — and
        it would make THIS reconcile defer forever, waiting on an answer to a
        question nobody is being asked. Which matters: a corroborated remain jump is
        exactly the shape this reconcile exists to correct (rule 10 — on this fleet a
        wire-vs-ledger contradiction is misattribution, never a refill story), so the
        deferral must not outlive the prompt that justified it."""
        spool_respool._respool_observation_logged[7] = {(0, 1): (TAG_UID, TRAY_UUID)}
        try:
            assert spool_respool.respool_prompt_open_for_slot(7, 0, 1) is False
            # ...and the shared empty-slot edge still re-arms it for the next roll.
            spool_respool.clear_respool_prompt_dedup(7, 0, 1)
            assert spool_respool._respool_observation_logged[7] == {}
        finally:
            spool_respool._respool_observation_logged.pop(7, None)


@pytest.mark.asyncio
class TestSpentRowsAreLeftToTheirOwnMachine:
    async def test_a_spent_row_reading_full_still_reconciles(self, db_session, clock, notify, trusted):
        """The spent LATCH is the runout state machine's business (W1); the gram
        ledger is this lane's. A spent row whose chip now reads full has had a fresh
        roll put on it — the re-spool lane owns the identity question, and this lane
        only corrects grams, so it does not special-case ``spent_at``.
        """
        spool = await _seed_spool(db_session, spent_at=datetime.utcnow())
        _, second = await _two_stable_pushes(db_session, spool, clock)
        assert second is True
        assert spool.weight_used == 0.0
        assert spool.spent_at is not None  # latch untouched


@pytest.mark.asyncio
class TestConsumerLoopWiring:
    """The lane is reached from ``main.on_ams_change``'s slim consumer loop.

    An outcome-level pin, not a call-count one: a real spool-37-shaped row bound to
    a real slot must actually end up corrected by two live AMS pushes.
    """

    @staticmethod
    def _wire(monkeypatch, db, state):
        from unittest.mock import AsyncMock, MagicMock

        session_cm = AsyncMock()
        session_cm.__aenter__ = AsyncMock(return_value=db)
        session_cm.__aexit__ = AsyncMock(return_value=None)
        monkeypatch.setattr("backend.app.main.async_session", lambda: session_cm)

        pm = MagicMock()
        pm.get_printer.return_value = None  # skips the relay leg
        pm.get_status.return_value = state
        pm.get_model.return_value = "H2S"
        pm.get_drying_targets.return_value = {}
        monkeypatch.setattr("backend.app.main.printer_manager", pm)
        monkeypatch.setattr("backend.app.main.ws_manager", AsyncMock())
        monkeypatch.setattr(
            "backend.app.services.spool_tag_matcher.reapply_k_profile_if_drifted", AsyncMock(return_value=False)
        )

    async def test_two_pushes_through_on_ams_change_correct_the_ledger(
        self, db_session, printer_factory, clock, notify, trusted, monkeypatch
    ):
        from types import SimpleNamespace

        from backend.app.main import on_ams_change
        from backend.app.models.spool_assignment import SpoolAssignment

        printer = await printer_factory()
        spool = await _seed_spool(db_session)
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=3))
        await db_session.commit()

        ams_data = [{"id": 0, "tray": [_tray()]}]
        state = SimpleNamespace(state="IDLE", raw_data={"ams": ams_data}, ams_extruder_map={}, tray_now=255)
        self._wire(monkeypatch, db_session, state)

        await on_ams_change(printer.id, ams_data)
        assert spool.weight_used == SPOOL_37_USED  # one push corroborates only

        clock["t"] += _LEDGER_DECREASE_STABLE_S
        await on_ams_change(printer.id, ams_data)

        db_session.expunge_all()
        refreshed = await db_session.get(Spool, spool.id)
        assert refreshed.weight_used == 0.0
        notify.assert_awaited_once()

    async def test_a_printing_printer_never_reaches_the_lane(
        self, db_session, printer_factory, clock, notify, trusted, monkeypatch
    ):
        """The loop's own ``ams_weight_sync_allowed`` gate short-circuits first."""
        from types import SimpleNamespace

        from backend.app.main import on_ams_change
        from backend.app.models.spool_assignment import SpoolAssignment

        printer = await printer_factory()
        spool = await _seed_spool(db_session)
        db_session.add(SpoolAssignment(spool_id=spool.id, printer_id=printer.id, ams_id=0, tray_id=3))
        await db_session.commit()

        ams_data = [{"id": 0, "tray": [_tray()]}]
        state = SimpleNamespace(state="RUNNING", raw_data={"ams": ams_data}, ams_extruder_map={}, tray_now=255)
        self._wire(monkeypatch, db_session, state)

        await on_ams_change(printer.id, ams_data)
        clock["t"] += _LEDGER_DECREASE_STABLE_S
        await on_ams_change(printer.id, ams_data)

        db_session.expunge_all()
        refreshed = await db_session.get(Spool, spool.id)
        assert refreshed.weight_used == SPOOL_37_USED
        notify.assert_not_awaited()


@pytest.mark.asyncio
class TestIdentityAgreement:
    """The wire roll must BE the row being rewritten. Justification for the write is
    "the firmware read THIS roll's chip", so identity agrees before grams do."""

    async def test_sibling_tag_read_still_reconciles(self, db_session, clock, notify, trusted):
        """UUID-PRIMARY (2026-08-01 correction): a Bambu roll carries TWO chips
        sharing one tray_uuid, so a differing tag_uid beside an agreeing uuid is a
        sibling read of the SAME roll — the 001-T3 lesson."""
        spool = await _seed_spool(db_session)
        sibling = _tray(tag_uid="1111222233334444")  # same uuid, other chip

        _, second = await _two_stable_pushes(db_session, spool, clock, tray=sibling)

        assert second is True
        assert spool.weight_used == 0.0

    async def test_different_uuid_is_refused(self, db_session, clock, notify, trusted):
        """A genuinely different roll in the slot: the pipeline owns the rebind, and
        this row's ledger must not be rewritten from a stranger's remain%."""
        spool = await _seed_spool(db_session)
        stranger = _tray(tray_uuid="99998888777766665555444433332222")

        await _two_stable_pushes(db_session, spool, clock, tray=stranger)
        assert spool.weight_used == SPOOL_37_USED

    async def test_tag_decides_when_neither_side_asserts_a_uuid(self, db_session, clock, notify, trusted):
        spool = await _seed_spool(db_session)
        spool.tray_uuid = None
        await db_session.commit()
        tray = _tray(tray_uuid="0" * 32)

        _, second = await _two_stable_pushes(db_session, spool, clock, tray=tray)
        assert second is True
        assert spool.weight_used == 0.0

    async def test_uuidless_row_with_a_different_tag_is_refused(self, db_session, clock, notify, trusted):
        spool = await _seed_spool(db_session)
        spool.tray_uuid = None
        await db_session.commit()
        tray = _tray(tag_uid="1111222233334444", tray_uuid="0" * 32)

        await _two_stable_pushes(db_session, spool, clock, tray=tray)
        assert spool.weight_used == SPOOL_37_USED

    async def test_nothing_comparable_is_refused(self, db_session, clock, notify, trusted):
        """Silence is never agreement: a row or a push asserting no identity member
        cannot prove the roll, so the lane declines."""
        spool = await _seed_spool(db_session, tagged=False)
        await _two_stable_pushes(db_session, spool, clock)
        assert spool.weight_used == SPOOL_37_USED
