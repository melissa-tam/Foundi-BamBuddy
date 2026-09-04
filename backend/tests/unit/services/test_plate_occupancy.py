"""Tests for ``plate_occupancy`` — the plate + printer-ownership authority (WS1).

Named after the incidents they pin, so a future triage that reproduces one of
these shapes finds the test by grepping the date or the symptom:

* the 2026-08-29 restart-recovery FINISH that reported zero peaks and was read as
  "deposited nothing" (``TestDepositEvidence``);
* the 2026-08-30 01:06:57 declare-under-a-lease race (``TestDeclareOccupied``);
* the 2026-08-30 01:46-01:49 eight consecutive ``eject_in_flight`` 409s after a
  restart (``TestClaimForEject``);
* the ≥12-minute dead-claim lockout a ``db_claim``-derived eject refusal would
  create (``TestClaimForEject`` / ``TestGatePriorities``);
* the 2026-07-31 gouged-plate stamp-before-stop ordering
  (``TestRuntimeExceeded``);
* the never-armless policy floor from 2026-07-18/07-21 (``TestNotify``).

The module is I/O-free, so everything here is a plain synchronous call: no
session, no loop, no mocks of app services — only the five injected callables.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.services import plate_occupancy as po

RUNNING = "RUNNING"
IDLE = "IDLE"


# ---------------------------------------------------------------------------
# Fixtures + builders
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_authority():
    """Every test starts with an empty fleet and un-wired callables."""
    po.plate_occupancy.reset_for_tests()
    yield
    po.plate_occupancy.reset_for_tests()


class _Clock:
    """A steerable stand-in for ``time.monotonic``."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(po, "_now_mono", c)
    return c


def _deposit_evidence(*, deposited: bool) -> po.DepositEvidence:
    """The two unambiguous ends of the deposit table, for tests about something else."""
    if deposited:
        return po.DepositEvidence(
            final_status="completed",
            is_dry_run=False,
            peaks_reliable=True,
            last_layer_num=42,
            last_progress=100.0,
        )
    return po.DepositEvidence(
        final_status="cancelled",
        is_dry_run=False,
        peaks_reliable=True,
        last_layer_num=0,
        last_progress=0.0,
    )


def _disposition(
    *,
    queue_item_id: int | None = None,
    source: str | None = "subtask-A",
    deposited: bool = True,
    policy: po.OccupancyPolicy | None = None,
    raise_gate: bool = True,
) -> po.TerminalDisposition:
    return po.TerminalDisposition(
        queue_item_id=queue_item_id,
        source_subtask_id=source,
        evidence=_deposit_evidence(deposited=deposited),
        policy=policy if policy is not None else po.CooldownEject(unit_id=1, run_id=1),
        raise_gate=raise_gate,
    )


def _pending(
    *,
    purpose: po.EjectPurpose = "production",
    queue_item_id: int | None = 77,
    hydrated: bool = False,
    started_at: datetime | None = None,
) -> po.PendingEject:
    return po.PendingEject(
        purpose=purpose,
        run_id=5,
        queue_item_id=queue_item_id,
        expected_runtime_s=83.0,
        started_at=started_at,
        hydrated=hydrated,
    )


def _occupy(printer_id: int) -> None:
    """Put a deposit on the plate the short way (the operator's own statement)."""
    assert po.plate_occupancy.declare_occupied(printer_id, po.Evidence()) is None


def _hold_live_eject(printer_id: int) -> None:
    """Occupied plate + a LIVE (non-hydrated) eject claimed on it."""
    _occupy(printer_id)
    assert po.plate_occupancy.claim_for_eject(printer_id, _pending(), po.Evidence()) is None


# ---------------------------------------------------------------------------
# 1. DepositEvidence
# ---------------------------------------------------------------------------


class TestDepositEvidence:
    """Unknown peaks fail CLOSED — absence of measurement is not measurement of absence."""

    @pytest.mark.parametrize(
        ("label", "kwargs", "expected"),
        [
            (
                # 2026-08-29: six prints survived a redeploy, lost their in-memory
                # peaks, and finished `completed` reporting layer 0 / progress 0.
                # Read as "no deposit" they left no gate, no eject, and the next
                # unit dispatched onto the finished part 1-5 s later.
                "restart-recovery FINISH: completed, peaks unknown, zero peaks",
                {"final_status": "completed", "peaks_reliable": False, "last_layer_num": 0, "last_progress": 0.0},
                True,
            ),
            (
                "cancelled with unknown peaks — cannot prove the plate is empty",
                {"final_status": "cancelled", "peaks_reliable": False, "last_layer_num": None, "last_progress": None},
                True,
            ),
            (
                "cancelled with RELIABLE zero peaks — a measured non-deposit",
                {"final_status": "cancelled", "peaks_reliable": True, "last_layer_num": 0, "last_progress": 0.0},
                False,
            ),
            (
                "cancelled after real progress — a partial is still a part",
                {"final_status": "cancelled", "peaks_reliable": True, "last_layer_num": 12, "last_progress": 30.0},
                True,
            ),
            (
                # The measured non-deposit needs BOTH peaks at zero. A layer counter
                # still reading 0 while progress has moved is the firmware's own
                # lag-by-one on the first layer — material is already going down, so
                # the disjunction (not the conjunction) is what decides.
                "cancelled at layer 0 but with progress — the first layer is down",
                {"final_status": "cancelled", "peaks_reliable": True, "last_layer_num": 0, "last_progress": 3.2},
                True,
            ),
            (
                "cancelled with layers but no progress reading — the same rule mirrored",
                {"final_status": "cancelled", "peaks_reliable": True, "last_layer_num": 4, "last_progress": 0.0},
                True,
            ),
            (
                "failed with reliable zero peaks — nothing was ever laid down",
                {"final_status": "failed", "peaks_reliable": True, "last_layer_num": 0, "last_progress": 0.0},
                False,
            ),
        ],
    )
    def test_deposit_table(self, label, kwargs, expected):
        evidence = po.DepositEvidence(is_dry_run=False, **kwargs)
        assert evidence.deposited is expected, label

    @pytest.mark.parametrize("final_status", ["completed", "cancelled", "failed"])
    @pytest.mark.parametrize("peaks_reliable", [True, False])
    def test_dry_run_beats_every_other_signal(self, final_status, peaks_reliable):
        """The eject dry-run file is motion-only: it cannot deposit, whatever it echoes."""
        evidence = po.DepositEvidence(
            final_status=final_status,
            is_dry_run=True,
            peaks_reliable=peaks_reliable,
            last_layer_num=99,
            last_progress=100.0,
        )
        assert evidence.deposited is False

    def test_from_terminal_payload_without_peaks_reliable_key_fails_closed(self):
        """An absent key is an old client / a virtual printer, never a promise of zeros."""
        payload = {"status": "cancelled", "last_layer_num": 0, "last_progress": 0}
        evidence = po.DepositEvidence.from_terminal_payload(payload, is_dry_run=False)

        assert evidence.peaks_reliable is False
        assert evidence.deposited is True

    def test_from_terminal_payload_reads_the_real_completion_keys(self):
        payload = {
            "status": "cancelled",
            "peaks_reliable": True,
            "last_layer_num": 0,
            "last_progress": 0.0,
        }
        evidence = po.DepositEvidence.from_terminal_payload(payload, is_dry_run=False)

        assert (evidence.final_status, evidence.last_layer_num, evidence.last_progress) == ("cancelled", 0, 0.0)
        assert evidence.deposited is False

    def test_from_terminal_payload_defaults_status_to_completed(self):
        evidence = po.DepositEvidence.from_terminal_payload({}, is_dry_run=False)
        assert evidence.final_status == "completed"
        assert evidence.deposited is True

    def test_unknown_is_the_fail_closed_form(self):
        """A downtime reconcile synthesises a terminal nobody observed."""
        evidence = po.DepositEvidence.unknown("failed")
        assert (evidence.peaks_reliable, evidence.is_dry_run) == (False, False)
        assert evidence.deposited is True


# ---------------------------------------------------------------------------
# 2. note_terminal
# ---------------------------------------------------------------------------


class TestNoteTerminal:
    """Legal from every owner; raises but NEVER clears; consumes only its own lease."""

    def test_raises_the_gate_from_no_owner(self):
        po.plate_occupancy.note_terminal(1, _disposition())

        view = po.plate_occupancy.snapshot(1)
        assert view.plate_occupied is True
        assert view.plate_source_subtask_id == "subtask-A"
        assert view.plate_policy == po.CooldownEject(unit_id=1, run_id=1)

    def test_raises_the_gate_while_a_lease_is_held(self, clock):
        lease = po.plate_occupancy.claim_for_dispatch(
            1, 9, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
        )
        assert isinstance(lease, po.DispatchLease)

        po.plate_occupancy.note_terminal(1, _disposition(queue_item_id=9))

        view = po.plate_occupancy.snapshot(1, po.Evidence())
        assert view.plate_occupied is True
        # The terminal named the unit holding the lease, so the claim is consumed.
        assert view.lease_unit_id is None

    def test_a_foreign_terminal_leaves_the_lease_to_its_own_timers(self, clock):
        lease = po.plate_occupancy.claim_for_dispatch(
            1, 9, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
        )
        assert isinstance(lease, po.DispatchLease)

        po.plate_occupancy.note_terminal(1, _disposition(queue_item_id=404))

        view = po.plate_occupancy.snapshot(1, po.Evidence())
        assert view.lease_unit_id == 9
        assert view.lease_active is True

    def test_an_eject_owner_is_untouched(self):
        """A mismatched / foreign terminal keeps the pending eject, as today."""
        _hold_live_eject(1)

        po.plate_occupancy.note_terminal(1, _disposition(queue_item_id=404, source="subtask-B"))

        identity = po.plate_occupancy.eject_identity(1)
        assert identity is not None
        assert identity.queue_item_id == 77

    def test_a_second_deposit_replaces_source_and_policy(self):
        """The newest deposit is the one an eject would sweep, so it owns the gate."""
        po.plate_occupancy.note_terminal(1, _disposition(source="subtask-A", policy=po.EscalationOnly()))
        po.plate_occupancy.note_terminal(
            1, _disposition(source="subtask-B", policy=po.CooldownEject(unit_id=4, run_id=2))
        )

        view = po.plate_occupancy.snapshot(1)
        assert view.plate_source_subtask_id == "subtask-B"
        assert view.plate_policy == po.CooldownEject(unit_id=4, run_id=2)

    def test_a_no_deposit_terminal_never_clears_a_standing_gate(self):
        """The gate drops only for a human or a matched eject — never for a terminal."""
        po.plate_occupancy.note_terminal(1, _disposition(source="subtask-A"))
        po.plate_occupancy.note_terminal(1, _disposition(source="subtask-B", deposited=False))

        view = po.plate_occupancy.snapshot(1)
        assert view.plate_occupied is True
        assert view.plate_source_subtask_id == "subtask-A"

    def test_a_no_deposit_terminal_on_a_clear_plate_raises_nothing(self):
        po.plate_occupancy.note_terminal(1, _disposition(deposited=False))
        assert po.plate_occupancy.is_plate_occupied(1) is False

    def test_raise_gate_false_suppresses_the_raise(self):
        """The require_plate_clear / farm-involvement guard, carried on the disposition."""
        po.plate_occupancy.note_terminal(1, _disposition(raise_gate=False))
        assert po.plate_occupancy.is_plate_occupied(1) is False


# ---------------------------------------------------------------------------
# 3. declare_occupied
# ---------------------------------------------------------------------------


class TestDeclareOccupied:
    """The operator is the authority on occupancy (R1)."""

    def test_revokes_an_unrevoked_lease_and_commit_then_refuses(self, clock):
        """The 2026-08-30 01:06:57 shape: the declaration landed between the
        scheduler's claim (01:06:56.8) and its start_print (01:06:59.8) and used to
        be erased by an unconditional gate clear on the dispatch path."""
        lease = po.plate_occupancy.claim_for_dispatch(
            4, 1010, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
        )
        assert isinstance(lease, po.DispatchLease)

        assert po.plate_occupancy.declare_occupied(4, po.Evidence()) is None

        assert lease.revoked is True
        assert po.plate_occupancy.commit_dispatch(4, lease) == "lease_revoked"
        assert po.plate_occupancy.is_plate_occupied(4) is True

    def test_a_revoked_uncommitted_lease_is_not_pruned_on_read(self, clock):
        """It must survive to be FOUND by commit_dispatch — that is the refusal."""
        lease = po.plate_occupancy.claim_for_dispatch(
            4, 1010, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
        )
        assert isinstance(lease, po.DispatchLease)
        po.plate_occupancy.declare_occupied(4, po.Evidence())
        clock.advance(10_000)

        view = po.plate_occupancy.snapshot(4, po.Evidence())
        assert view.lease_unit_id == 1010
        assert view.lease_active is False

    def test_declaring_an_already_occupied_plate_is_an_idempotent_success(self):
        po.plate_occupancy.note_terminal(1, _disposition(source="subtask-A", policy=po.CooldownEject(2, 2)))
        before = po.plate_occupancy.snapshot(1)

        assert po.plate_occupancy.declare_occupied(1, po.Evidence()) is None

        after = po.plate_occupancy.snapshot(1)
        assert after.plate_policy == po.CooldownEject(2, 2)
        assert after.plate_source_subtask_id == "subtask-A"
        assert after.plate_since == before.plate_since

    def test_refused_while_a_job_is_running(self):
        assert po.plate_occupancy.declare_occupied(1, po.Evidence(live_state=RUNNING)) == "job_active"
        assert po.plate_occupancy.is_plate_occupied(1) is False

    def test_refused_under_a_live_eject(self):
        """The refusals are evaluated BEFORE the already-occupied idempotency, so a
        live sweep answers with its own reason rather than a bare success. Both roads
        lead to the same place — the manual lane's next call is ``ejectable``, which
        refuses identically — but the operator is told which one it is."""
        _hold_live_eject(1)

        assert po.plate_occupancy.declare_occupied(1, po.Evidence()) == "eject_in_flight"
        assert po.plate_occupancy.clear_plate(1) == "eject_in_flight"

    def test_a_hydrated_eject_does_not_block_a_declaration(self):
        """The farm has admitted it cannot verify that record; it cannot outrank a human."""
        po.plate_occupancy.hydrate_eject(2, _pending(hydrated=True))

        assert po.plate_occupancy.declare_occupied(2, po.Evidence()) is None
        assert po.plate_occupancy.is_plate_occupied(2) is True


# ---------------------------------------------------------------------------
# 4. The two claims
# ---------------------------------------------------------------------------


class TestClaimForDispatch:
    @pytest.mark.parametrize(
        ("label", "setup", "ev", "expected"),
        [
            ("plate carries a deposit", "plate", po.Evidence(), "plate_occupied"),
            ("an eject owns the printer", "live_eject", po.Evidence(), "plate_occupied"),
            ("a hydrated eject owns the printer", "hydrated_eject", po.Evidence(), "eject_in_flight"),
            ("a lease is already in flight", "lease", po.Evidence(), "dispatch_in_flight"),
            ("a printing row already claims it", None, po.Evidence(db_claim=True), "dispatch_in_flight"),
            ("the wire says a job is running", None, po.Evidence(live_state=RUNNING), "job_active"),
            ("nothing in the way", None, po.Evidence(live_state=IDLE), None),
        ],
    )
    def test_refusal_table(self, label, setup, ev, expected, clock):
        if setup == "plate":
            _occupy(1)
        elif setup == "live_eject":
            _hold_live_eject(1)
        elif setup == "hydrated_eject":
            po.plate_occupancy.hydrate_eject(1, _pending(hydrated=True))
        elif setup == "lease":
            po.plate_occupancy.claim_for_dispatch(
                1, 3, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
            )

        result = po.plate_occupancy.claim_for_dispatch(
            1, 9, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=ev
        )

        if expected is None:
            assert isinstance(result, po.DispatchLease), label
            assert result.unit_id == 9
        else:
            assert result == expected, label

    def test_commit_refuses_a_lease_the_printer_does_not_hold(self, clock):
        lease = po.plate_occupancy.claim_for_dispatch(
            1, 9, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
        )
        assert isinstance(lease, po.DispatchLease)
        po.plate_occupancy.release_dispatch(1, "test")

        assert po.plate_occupancy.commit_dispatch(1, lease) == "lease_unknown"

    def test_commit_refuses_onto_an_occupied_plate(self, clock):
        lease = po.plate_occupancy.claim_for_dispatch(
            1, 9, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
        )
        assert isinstance(lease, po.DispatchLease)
        po.plate_occupancy.note_terminal(1, _disposition(queue_item_id=404))

        assert po.plate_occupancy.commit_dispatch(1, lease) == "plate_occupied"

    def test_release_is_idempotent_and_never_refuses(self, clock):
        po.plate_occupancy.release_dispatch(1, "nothing held")
        lease = po.plate_occupancy.claim_for_dispatch(
            1, 9, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
        )
        assert isinstance(lease, po.DispatchLease)

        po.plate_occupancy.release_dispatch(1, "watchdog")
        po.plate_occupancy.release_dispatch(1, "again")

        assert po.plate_occupancy.snapshot(1, po.Evidence()).lease_unit_id is None


class TestClaimForEject:
    @pytest.mark.parametrize(
        ("label", "setup", "ev", "expected"),
        [
            ("the wire says a job is running", "plate", po.Evidence(live_state=RUNNING), "job_active"),
            ("a dispatch lease is in flight", "plate_and_lease", po.Evidence(), "dispatch_in_flight"),
            ("a LIVE eject already owns it", "live_eject", po.Evidence(), "eject_in_flight"),
            ("no deposit has been declared", None, po.Evidence(), "not_occupied"),
            ("plate declared, nothing in the way", "plate", po.Evidence(live_state=IDLE), None),
            # A `printing` row on an IDLE printer is the 2026-08-29 dead-claim class,
            # released only after 600 s + a 120 s dwell. Deriving an eject refusal
            # from it would lock the operator out of a provably idle plate for ≥12 min.
            ("a stale printing row must NOT lock the operator out", "plate", po.Evidence(db_claim=True), None),
        ],
    )
    def test_refusal_table(self, label, setup, ev, expected, clock):
        if setup == "plate":
            _occupy(1)
        elif setup == "plate_and_lease":
            po.plate_occupancy.claim_for_dispatch(
                1, 3, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
            )
            po.plate_occupancy.declare_occupied(1, po.Evidence())
            # The declaration REVOKES the lease, so force the in-flight shape back on
            # to test the refusal itself.
            po.plate_occupancy._records[1].lease.revoked = False
        elif setup == "live_eject":
            _hold_live_eject(1)

        assert po.plate_occupancy.claim_for_eject(1, _pending(queue_item_id=1), ev) == expected, label

    def test_supersedes_a_hydrated_eject(self, caplog):
        """The 2026-08-30 "8 consecutive 409s" class: printer 4, 01:46-01:49, after
        which the operator hand-jogged the toolhead ~70 times. A record the farm
        cannot verify must not refuse a human standing at the machine."""
        po.plate_occupancy.hydrate_plate(4, "subtask-A", po.EscalationOnly())
        po.plate_occupancy.hydrate_eject(4, _pending(queue_item_id=500, hydrated=True))

        with caplog.at_level("WARNING"):
            assert po.plate_occupancy.claim_for_eject(4, _pending(queue_item_id=900), po.Evidence()) is None

        identity = po.plate_occupancy.eject_identity(4)
        assert identity is not None
        assert identity.queue_item_id == 900
        assert identity.hydrated is False
        assert identity.dispatched_at is not None
        assert "superseded_by_operator" in caplog.text

    def test_the_claim_stamps_dispatched_at_and_forces_live(self):
        _occupy(1)
        assert po.plate_occupancy.claim_for_eject(1, _pending(hydrated=True), po.Evidence()) is None

        view = po.plate_occupancy.snapshot(1)
        assert view.eject_hydrated is False
        assert view.eject_age_s is not None and view.eject_age_s >= 0.0
        assert view.eject_started is False


# ---------------------------------------------------------------------------
# 5. clear_plate / operator_recover
# ---------------------------------------------------------------------------


class TestClearAndRecover:
    def test_clear_refused_during_a_live_eject(self):
        _hold_live_eject(1)
        assert po.plate_occupancy.clear_plate(1) == "eject_in_flight"
        assert po.plate_occupancy.is_plate_occupied(1) is True

    def test_clear_allowed_with_only_a_hydrated_eject(self):
        """The unverifiable record must not dead-end the operator's cure."""
        po.plate_occupancy.hydrate_plate(1, "subtask-A", po.EscalationOnly())
        po.plate_occupancy.hydrate_eject(1, _pending(hydrated=True))

        assert po.plate_occupancy.clear_plate(1) is None
        assert po.plate_occupancy.is_plate_occupied(1) is False
        # The eject record itself is the startup reconciler's business, not clear_plate's.
        assert po.plate_occupancy.eject_identity(1) is not None

    def test_clear_refuses_a_plate_that_is_not_occupied(self):
        assert po.plate_occupancy.clear_plate(1) == "not_occupied"

    def test_operator_recover_drops_everything(self, clock, caplog):
        po.plate_occupancy.claim_for_dispatch(
            1, 9, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
        )
        po.plate_occupancy.declare_occupied(1, po.Evidence())
        po.plate_occupancy._records[1].eject = _pending()

        with caplog.at_level("WARNING"):
            po.plate_occupancy.operator_recover(1)

        view = po.plate_occupancy.snapshot(1, po.Evidence())
        assert (view.plate_occupied, view.lease_unit_id, view.eject_purpose) == (False, None, None)
        assert "operator recover dropped" in caplog.text

    def test_operator_recover_on_a_clean_printer_is_a_no_op(self):
        kicks: list[int] = []
        po.plate_occupancy.configure(kick=lambda pid, cause: kicks.append(pid))

        po.plate_occupancy.operator_recover(1)

        assert kicks == []


# ---------------------------------------------------------------------------
# 6. Lease settlement (read-time, never a transition)
# ---------------------------------------------------------------------------


class TestLeaseSettlement:
    def _claim_and_commit(self, clock, *, pre_state: str | None = IDLE, min_hold_s=2.0, max_hold_s=60.0):
        lease = po.plate_occupancy.claim_for_dispatch(
            1,
            9,
            pre_state=pre_state,
            pre_subtask="sub-0",
            min_hold_s=min_hold_s,
            max_hold_s=max_hold_s,
            ev=po.Evidence(),
        )
        assert isinstance(lease, po.DispatchLease)
        assert po.plate_occupancy.commit_dispatch(1, lease) is None
        return lease

    def test_an_uncommitted_lease_is_active_forever(self, clock):
        """The upload is in flight; there is nothing on the wire to transition yet."""
        po.plate_occupancy.claim_for_dispatch(
            1, 9, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
        )
        clock.advance(10_000)

        view = po.plate_occupancy.snapshot(1, po.Evidence(live_state=RUNNING))
        assert view.lease_active is True
        assert view.lease_unit_id == 9

    def test_transition_plus_min_hold_settles_the_lease(self, clock):
        self._claim_and_commit(clock)

        # Transitioned but inside min_hold → still held.
        clock.advance(1.0)
        assert po.plate_occupancy.snapshot(1, po.Evidence(live_state=RUNNING)).lease_active is True

        clock.advance(2.0)
        view = po.plate_occupancy.snapshot(1, po.Evidence(live_state=RUNNING))
        assert view.lease_active is False
        # Settled AND pruned: a spent lease must not linger in the record.
        assert view.lease_unit_id is None

    def test_min_hold_alone_without_a_transition_keeps_the_lease(self, clock):
        self._claim_and_commit(clock)
        clock.advance(30.0)

        assert po.plate_occupancy.snapshot(1, po.Evidence(live_state=IDLE)).lease_active is True

    def test_max_hold_expires_the_lease_with_no_evidence_at_all(self, clock):
        """An idle queue must never strand a lease past its own ceiling."""
        self._claim_and_commit(clock)
        clock.advance(61.0)

        view = po.plate_occupancy.snapshot(1, po.Evidence())
        assert view.lease_active is False
        assert view.lease_unit_id is None

    def test_without_a_pre_state_min_hold_alone_decides(self, clock):
        """The scheduler's old empty-string sentinel: no snapshot, no transition test."""
        self._claim_and_commit(clock, pre_state=None)

        assert po.plate_occupancy.snapshot(1, po.Evidence()).lease_active is True
        clock.advance(2.5)
        assert po.plate_occupancy.snapshot(1, po.Evidence()).lease_active is False

    @pytest.mark.parametrize("live_state", [None, ""])
    def test_silence_is_not_a_transition(self, clock, live_state):
        self._claim_and_commit(clock)
        clock.advance(30.0)

        assert po.plate_occupancy.snapshot(1, po.Evidence(live_state=live_state)).lease_active is True

    def test_snapshot_without_evidence_reports_lease_active_none(self, clock):
        po.plate_occupancy.claim_for_dispatch(
            1, 9, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
        )
        assert po.plate_occupancy.snapshot(1).lease_active is None

    def test_settlement_never_fans_out(self, clock):
        """A settle is an expiry, not a state change: no persist, no broadcast, no kick."""
        self._claim_and_commit(clock)
        calls: list[str] = []
        po.plate_occupancy.configure(
            persist=lambda pid, view: calls.append("persist"),
            broadcast=lambda pid: calls.append("broadcast"),
            kick=lambda pid, cause: calls.append("kick"),
            policy_driver=lambda pid, view, cause: calls.append("policy"),
        )

        clock.advance(61.0)
        assert po.plate_occupancy.snapshot(1, po.Evidence()).lease_active is False

        assert calls == []

    def test_printers_with_lease_or_eject(self, clock):
        po.plate_occupancy.claim_for_dispatch(
            1, 9, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
        )
        _hold_live_eject(2)
        po.plate_occupancy.hydrate_eject(3, _pending(hydrated=True))
        _occupy(4)  # a plate alone is not a lease or an eject

        assert po.plate_occupancy.printers_with_lease_or_eject() == {1, 2, 3}


# ---------------------------------------------------------------------------
# 7. Eject lifecycle
# ---------------------------------------------------------------------------


class TestEjectLifecycle:
    def test_expire_start_fires_on_a_live_never_started_eject(self, caplog):
        """The firmware silently ignores a project_file sent while busy — the start
        deadline is the ONLY signal that shape produces (2026-08-30)."""
        _hold_live_eject(1)

        with caplog.at_level("WARNING"):
            assert po.plate_occupancy.expire_eject_start(1) is True

        view = po.plate_occupancy.snapshot(1)
        assert view.eject_purpose is None
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, po.EscalationOnly)
        assert "eject_never_started" in caplog.text

    def test_expire_start_never_touches_a_hydrated_eject(self):
        """Its started_at is None by construction; expiring on that fires every restart."""
        po.plate_occupancy.hydrate_plate(1, "subtask-A", po.EscalationOnly())
        po.plate_occupancy.hydrate_eject(1, _pending(hydrated=True))

        assert po.plate_occupancy.expire_eject_start(1) is False
        assert po.plate_occupancy.eject_identity(1) is not None

    def test_expire_start_never_touches_a_started_eject(self):
        _hold_live_eject(1)
        po.plate_occupancy.note_eject_started(1)

        assert po.plate_occupancy.expire_eject_start(1) is False
        assert po.plate_occupancy.eject_identity(1) is not None

    def test_expire_start_with_no_eject_is_false(self):
        assert po.plate_occupancy.expire_eject_start(1) is False

    def test_note_eject_started_is_first_write_wins(self):
        _hold_live_eject(1)
        po.plate_occupancy.note_eject_started(1)
        first = po.plate_occupancy.eject_identity(1)
        assert first is not None and first.started_at is not None

        po.plate_occupancy.note_eject_started(1)
        second = po.plate_occupancy.eject_identity(1)
        assert second is not None and second.started_at == first.started_at

    def test_note_eject_started_without_an_eject_is_a_no_op(self):
        po.plate_occupancy.note_eject_started(1)
        assert po.plate_occupancy.eject_identity(1) is None

    def test_resolve_completed_clears_the_plate(self):
        _hold_live_eject(1)

        assert po.plate_occupancy.resolve_eject(1, "completed") is None

        view = po.plate_occupancy.snapshot(1)
        assert (view.plate_occupied, view.eject_purpose) == (False, None)

    def test_resolve_unverified_keeps_the_plate_under_escalation_only(self):
        po.plate_occupancy.note_terminal(1, _disposition(source="subtask-A", policy=po.CooldownEject(3, 3)))
        before = po.plate_occupancy.snapshot(1)
        assert po.plate_occupancy.claim_for_eject(1, _pending(), po.Evidence()) is None

        assert po.plate_occupancy.resolve_eject(1, "unverified") is None

        view = po.plate_occupancy.snapshot(1)
        assert view.plate_occupied is True
        assert isinstance(view.plate_policy, po.EscalationOnly)
        # Source and `since` are preserved: the plate has carried this deposit since
        # then, and re-stamping would say the part appeared when the sweep failed.
        assert view.plate_source_subtask_id == "subtask-A"
        assert view.plate_since == before.plate_since
        assert view.eject_purpose is None

    def test_resolve_without_an_eject_refuses(self):
        assert po.plate_occupancy.resolve_eject(1, "completed") == "no_eject"

    def test_drop_hydrated_eject_keeps_the_plate(self):
        po.plate_occupancy.hydrate_plate(1, "subtask-A", po.EscalationOnly())
        po.plate_occupancy.hydrate_eject(1, _pending(hydrated=True))

        assert po.plate_occupancy.drop_hydrated_eject(1, "reconciled: printer idle") is True

        view = po.plate_occupancy.snapshot(1)
        assert view.eject_purpose is None
        assert view.plate_occupied is True

    def test_drop_hydrated_eject_refuses_to_touch_a_live_one(self):
        _hold_live_eject(1)
        assert po.plate_occupancy.drop_hydrated_eject(1, "reconciled") is False
        assert po.plate_occupancy.eject_identity(1) is not None

    def test_set_policy_needs_an_occupied_plate(self):
        assert po.plate_occupancy.set_policy(1, po.FirstArticleEject(unit_id=1, run_id=1)) == "not_occupied"

        _occupy(1)
        assert po.plate_occupancy.set_policy(1, po.FirstArticleEject(unit_id=1, run_id=1)) is None
        assert po.plate_occupancy.snapshot(1).plate_policy == po.FirstArticleEject(unit_id=1, run_id=1)


# ---------------------------------------------------------------------------
# 8. The watchdog verdict, stamped before the stop
# ---------------------------------------------------------------------------


class TestRuntimeExceeded:
    def test_the_mark_is_readable_before_the_terminal_resolves(self):
        """2026-07-31 gouged plate: the stall echoed `completed`, so the terminal
        handler must be able to see the verdict a racing watchdog already stamped."""
        _hold_live_eject(1)
        po.plate_occupancy.note_eject_started(1)
        fired_at = datetime.now(timezone.utc)

        po.plate_occupancy.note_eject_runtime_exceeded(1, fired_at, "drop")

        identity = po.plate_occupancy.eject_identity(1)
        assert identity is not None
        assert identity.runtime_exceeded_at == fired_at

        # The terminal arrives afterwards and is resolved on the watchdog's terms.
        assert po.plate_occupancy.resolve_eject(1, "unverified") is None
        assert po.plate_occupancy.is_plate_occupied(1) is True

    def test_the_mark_is_first_write_wins(self):
        _hold_live_eject(1)
        first = datetime.now(timezone.utc)
        later = first + timedelta(seconds=30)

        po.plate_occupancy.note_eject_runtime_exceeded(1, first, "drop")
        po.plate_occupancy.note_eject_runtime_exceeded(1, later, "total")

        identity = po.plate_occupancy.eject_identity(1)
        assert identity is not None and identity.runtime_exceeded_at == first

    def test_the_mark_without_an_eject_is_a_no_op(self):
        po.plate_occupancy.note_eject_runtime_exceeded(1, datetime.now(timezone.utc), "total")
        assert po.plate_occupancy.eject_identity(1) is None


# ---------------------------------------------------------------------------
# 9. The fan-out
# ---------------------------------------------------------------------------


class _Recorder:
    """Records the fan-out in call order, one tuple per callable."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.escalations: list[BaseException] = []

    def wire(self, **overrides) -> None:
        po.plate_occupancy.configure(
            persist=overrides.get("persist", self.persist),
            broadcast=overrides.get("broadcast", self.broadcast),
            kick=overrides.get("kick", self.kick),
            policy_driver=overrides.get("policy_driver", self.policy_driver),
            escalate=overrides.get("escalate", self.escalate),
        )

    def persist(self, printer_id: int, view: po.OccupancyView) -> None:
        self.calls.append(("persist", ""))

    def broadcast(self, printer_id: int) -> None:
        self.calls.append(("broadcast", ""))

    def kick(self, printer_id: int, cause: str) -> None:
        self.calls.append(("kick", cause))

    def policy_driver(self, printer_id: int, view: po.OccupancyView, cause: str) -> None:
        self.calls.append(("policy", cause))

    def escalate(self, printer_id: int, error: BaseException) -> None:
        self.escalations.append(error)

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


class TestNotify:
    def test_fixed_call_order(self):
        rec = _Recorder()
        rec.wire()

        po.plate_occupancy.note_terminal(1, _disposition())

        assert rec.calls == [("persist", ""), ("broadcast", ""), ("policy", "terminal")]

    def test_hydrate_skips_persist_broadcast_and_kick_but_arms_the_policy(self):
        """The state was just READ from the DB; echoing it back carries no information,
        and a kick would wake the scheduler before the reconcilers have decided."""
        rec = _Recorder()
        rec.wire()

        po.plate_occupancy.hydrate_plate(1, "subtask-A", po.CooldownEject(1, 1))
        po.plate_occupancy.hydrate_eject(1, _pending(hydrated=True))

        assert rec.calls == [("policy", "hydrate"), ("policy", "hydrate")]

    @pytest.mark.parametrize(
        ("label", "action", "kicks"),
        [
            ("plate raised — not a release", "raise_plate", False),
            ("eject claimed — not a release", "claim_eject", False),
            ("plate cleared — a release edge", "clear_plate", True),
            ("eject dropped — a release edge", "resolve_eject", True),
            ("hydrated eject dropped — a release edge", "drop_hydrated", True),
        ],
    )
    def test_kick_fires_only_on_the_two_release_edges(self, label, action, kicks):
        rec = _Recorder()

        if action == "raise_plate":
            rec.wire()
            po.plate_occupancy.note_terminal(1, _disposition())
        elif action == "claim_eject":
            _occupy(1)
            rec.wire()
            po.plate_occupancy.claim_for_eject(1, _pending(), po.Evidence())
        elif action == "clear_plate":
            _occupy(1)
            rec.wire()
            po.plate_occupancy.clear_plate(1)
        elif action == "resolve_eject":
            _hold_live_eject(1)
            rec.wire()
            po.plate_occupancy.resolve_eject(1, "unverified")
        elif action == "drop_hydrated":
            po.plate_occupancy.hydrate_plate(1, "subtask-A", po.EscalationOnly())
            po.plate_occupancy.hydrate_eject(1, _pending(hydrated=True))
            rec.wire()
            po.plate_occupancy.drop_hydrated_eject(1, "reconciled")

        assert ("kick" in rec.names) is kicks, label

    def test_persist_broadcast_and_kick_failures_are_swallowed(self):
        """A failed mirror must never unwind an occupancy fact that already happened."""

        def boom(*args, **kwargs):
            raise RuntimeError("sink down")

        _occupy(1)
        rec = _Recorder()
        rec.wire(persist=boom, broadcast=boom, kick=boom)

        po.plate_occupancy.clear_plate(1)

        assert rec.calls == [("policy", "clear_plate")]
        assert po.plate_occupancy.is_plate_occupied(1) is False

    def test_a_failing_policy_driver_repairs_to_escalation_only_and_retries(self):
        """The never-armless rule (2026-07-18 / 07-21) lives in exactly one place."""
        seen: list[po.OccupancyPolicy | None] = []

        def flaky(printer_id, view, cause):
            seen.append(view.plate_policy)
            if len(seen) == 1:
                raise RuntimeError("cooldown watch would not arm")

        rec = _Recorder()
        rec.wire(policy_driver=flaky)

        po.plate_occupancy.note_terminal(1, _disposition(policy=po.CooldownEject(unit_id=8, run_id=2)))

        assert len(seen) == 2
        assert seen[0] == po.CooldownEject(unit_id=8, run_id=2)
        assert isinstance(seen[1], po.EscalationOnly)
        assert isinstance(po.plate_occupancy.snapshot(1).plate_policy, po.EscalationOnly)
        assert rec.escalations == []

    def test_a_double_policy_failure_escalates(self):
        def always_boom(printer_id, view, cause):
            raise RuntimeError("nothing will arm")

        rec = _Recorder()
        rec.wire(policy_driver=always_boom)

        po.plate_occupancy.note_terminal(1, _disposition())

        assert len(rec.escalations) == 1
        assert isinstance(rec.escalations[0], RuntimeError)

    def test_an_escalation_that_itself_fails_is_swallowed(self):
        def always_boom(*args, **kwargs):
            raise RuntimeError("nothing works")

        rec = _Recorder()
        rec.wire(policy_driver=always_boom, escalate=always_boom)

        po.plate_occupancy.note_terminal(1, _disposition())

        assert po.plate_occupancy.is_plate_occupied(1) is True

    def test_a_nested_transition_defers_until_the_outer_fan_out_drains(self):
        """`kick` must never read a record that changed under it mid-notification."""
        rec = _Recorder()

        def driver(printer_id, view, cause):
            rec.calls.append(("policy", cause))
            if cause == "declare_occupied":
                # The monitor's release path, reaching back into the authority.
                po.plate_occupancy.clear_plate(printer_id)

        rec.wire(policy_driver=driver)

        po.plate_occupancy.declare_occupied(1, po.Evidence())

        assert rec.calls == [
            ("persist", ""),
            ("broadcast", ""),
            ("policy", "declare_occupied"),
            # Only now does the nested change fan out — and its release edge kicks.
            ("persist", ""),
            ("broadcast", ""),
            ("kick", "clear_plate"),
            ("policy", "clear_plate"),
        ]
        assert po.plate_occupancy.is_plate_occupied(1) is False

    def test_configure_leaves_unnamed_callables_alone(self):
        rec = _Recorder()
        rec.wire()
        po.plate_occupancy.configure(kick=lambda pid, cause: rec.calls.append(("kick2", cause)))
        _occupy(1)

        po.plate_occupancy.clear_plate(1)

        assert ("kick2", "clear_plate") in rec.calls
        assert ("persist", "") in rec.calls


# ---------------------------------------------------------------------------
# 10. The two gates, as tables
# ---------------------------------------------------------------------------


class TestGatePriorities:
    @pytest.mark.parametrize(
        ("label", "setup", "ev", "expected"),
        [
            ("plate first — it outranks everything", "plate", po.Evidence(live_state=RUNNING), "plate_occupied"),
            ("a LIVE eject owns the printer", "live_eject_no_plate", po.Evidence(), "eject_in_flight"),
            (
                # A hydrated eject still means "the startup reconciler owns this
                # printer": dispatching under it would race the verdict.
                "a HYDRATED eject still blocks dispatch",
                "hydrated_eject",
                po.Evidence(),
                "eject_in_flight",
            ),
            ("an in-flight lease", "lease", po.Evidence(), "dispatch_in_flight"),
            ("a printing row during the IDLE→RUNNING lag", None, po.Evidence(db_claim=True), "dispatch_in_flight"),
            ("the wire", None, po.Evidence(live_state="PAUSE"), "job_active"),
            ("a clean idle printer", None, po.Evidence(live_state=IDLE), None),
        ],
    )
    def test_dispatchable_table(self, label, setup, ev, expected, clock):
        self._setup(setup, clock)
        assert po.plate_occupancy.dispatchable(1, ev) == expected, label

    @pytest.mark.parametrize(
        ("label", "setup", "ev", "expected"),
        [
            ("the wire first — the one physical collision", "plate", po.Evidence(live_state="PREPARE"), "job_active"),
            ("an in-flight lease", "plate_and_lease", po.Evidence(), "dispatch_in_flight"),
            ("a LIVE eject", "live_eject", po.Evidence(), "eject_in_flight"),
            ("a HYDRATED eject does NOT refuse", "hydrated_eject_and_plate", po.Evidence(), None),
            ("nothing declared", None, po.Evidence(), "not_occupied"),
            ("a stale printing row is never an eject refusal", "plate", po.Evidence(db_claim=True), None),
            ("a declared plate on an idle printer", "plate", po.Evidence(live_state=IDLE), None),
            (
                # The 2026-09-04 002-H2S shape: the reboot destroyed the Z datum, so
                # every absolute Z move in the block would run against a fiction.
                "a lost Z reference",
                "plate",
                po.Evidence(live_state=IDLE, z_reference=False),
                "z_unreferenced",
            ),
            (
                # ...but the wire still comes first: telling an operator to remove a
                # part by hand from a plate that is mid-print is the wrong machine.
                "the wire outranks the Z frame",
                "plate",
                po.Evidence(live_state=RUNNING, z_reference=False),
                "job_active",
            ),
            ("an unanswered Z frame passes", "plate", po.Evidence(live_state=IDLE, z_reference=None), None),
            ("a re-established Z frame passes", "plate", po.Evidence(live_state=IDLE, z_reference=True), None),
        ],
    )
    def test_ejectable_table(self, label, setup, ev, expected, clock):
        self._setup(setup, clock)
        assert po.plate_occupancy.ejectable(1, ev) == expected, label

    def test_the_z_frame_never_refuses_a_unit_dispatch(self):
        """``dispatchable`` is untouched: a lost Z frame is re-established by the next
        print's OWN start-gcode, which homes every axis on an empty plate. Refusing the
        dispatch would hold the printer for a condition the dispatch itself cures."""
        assert po.plate_occupancy.dispatchable(1, po.Evidence(live_state=IDLE, z_reference=False)) is None

    def test_claim_for_eject_is_gated_by_the_same_rule(self):
        """The claim runs AFTER the upload, so a hold that appeared meanwhile must still
        refuse — the pre-flight and the claim are one gate, not two."""
        _occupy(1)
        refusal = po.plate_occupancy.claim_for_eject(1, _pending(), po.Evidence(live_state=IDLE, z_reference=False))
        assert refusal == "z_unreferenced"
        assert po.plate_occupancy.pending_eject_view(1) is None

    def test_the_owner_projection(self, clock):
        assert po.plate_occupancy.snapshot(1, po.Evidence(live_state=IDLE)).owner == "none"
        assert po.plate_occupancy.snapshot(1, po.Evidence(live_state=RUNNING)).owner == "job"

        po.plate_occupancy.claim_for_dispatch(
            1, 9, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
        )
        assert po.plate_occupancy.snapshot(1, po.Evidence(live_state=IDLE)).owner == "dispatch"

        po.plate_occupancy.release_dispatch(1, "test")
        _hold_live_eject(1)
        # The sweep IS a job on the wire; the eject lane is the finer truth.
        assert po.plate_occupancy.snapshot(1, po.Evidence(live_state=RUNNING)).owner == "eject"

    def test_plate_source_is_the_one_reader_of_the_gate_id(self):
        assert po.plate_occupancy.plate_source(1) is None
        po.plate_occupancy.note_terminal(1, _disposition(source="subtask-Z"))
        assert po.plate_occupancy.plate_source(1) == "subtask-Z"

    def test_snapshot_of_an_unknown_printer_is_empty(self):
        view = po.plate_occupancy.snapshot(99)
        assert view.plate_occupied is False
        assert view.owner == "none"
        assert view.eject_present is False

    @staticmethod
    def _setup(setup: str | None, clock: _Clock) -> None:
        if setup == "plate":
            _occupy(1)
        elif setup == "live_eject":
            _hold_live_eject(1)
        elif setup == "live_eject_no_plate":
            _hold_live_eject(1)
            po.plate_occupancy._records[1].plate = None
        elif setup == "hydrated_eject":
            po.plate_occupancy.hydrate_eject(1, _pending(hydrated=True))
        elif setup == "hydrated_eject_and_plate":
            po.plate_occupancy.hydrate_plate(1, "subtask-A", po.EscalationOnly())
            po.plate_occupancy.hydrate_eject(1, _pending(hydrated=True))
        elif setup == "lease":
            po.plate_occupancy.claim_for_dispatch(
                1, 3, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
            )
        elif setup == "plate_and_lease":
            po.plate_occupancy.claim_for_dispatch(
                1, 3, pre_state=IDLE, pre_subtask=None, min_hold_s=2.0, max_hold_s=60.0, ev=po.Evidence()
            )
            _occupy(1)
            po.plate_occupancy._records[1].lease.revoked = False
