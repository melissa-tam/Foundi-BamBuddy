"""Tests for services.slot_pipeline — the W3a orchestrator.

The decision table (``slot_state``) is pure and pinned by its own suite; this one pins
what the orchestrator DOES with each answer: the candidate lookups it feeds the table,
the writes it performs through ``spool_binding`` (the real writer — never a mock, so a
binding invariant break fails here too), the two idempotency guards a pure table cannot
carry (the mint existence recheck + the per-pass seen-set), the per-printer
serialization, and the never-raise contract (cross-cutting invariant 10).

Replay pins ride the FULL pipeline against a real session:

* the four 2026-08-01 production sibling slots — stage 1 (tag-only push) must schedule
  an identify and touch NOTHING, stage 2 (full read) must KEEP with no new row. Minting
  on stage 1 is what would have duplicated all four rolls;
* 003-H2S T2 / spool 140 — a live tagless row bound to a physically EMPTY slot is
  RELEASED, with its last location stamped so the roll can reclaim its grams later.
"""

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import ams_presence, slot_pipeline, spool_binding, spool_tagless
from backend.app.services.slot_pipeline import PipelineDeps, run_slot_pipeline
from backend.app.services.slot_state import Decision, DecisionKind, SlotState
from backend.app.services.tray_observation import observation_tray_dict, observe_tray


def _seed_reseat(printer_id, ams_id, tray_id, *, absent_for, under_active_feed=False):
    """Stand in for the wire's loss→gain edge pair on one slot.

    The de-bounce lane reads two PEEK predicates that the presence lane writes at the gain
    (`ams_presence.reseat_within_window` / `reseat_under_active_feed`). Seeding the ledger
    directly is how the rest of this file already drives that module's read economy, and it
    keeps each case's variable explicit: ``absent_for=None`` is UNKNOWN (never "short").
    """
    ams_presence._reseat[(printer_id, ams_id, tray_id)] = ams_presence._Reseat(absent_for, under_active_feed)


_PIPELINE_LOGGER = "backend.app.services.slot_pipeline"

# A stamp no clock can produce during a test run, so "preserved" is decidable without
# sleeping on wall-clock resolution (same device as test_spool_binding.py).
_SENTINEL = datetime(2020, 1, 2, 3, 4, 5)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts with empty locks, dedup sets, cycles, move damper and read ledgers.

    ``ams_presence`` is in the list because the pipeline now WRITES to its read economy (the
    spent-occupied constellation opens its own read occasion), so a leaked epoch/occasion
    would let one test grant another's slot a read it never earned."""
    slot_pipeline._reset_state()
    spool_tagless._reset_state()
    ams_presence._reset_state()
    spool_binding._move_damper.reset()
    yield
    slot_pipeline._reset_state()
    spool_tagless._reset_state()
    ams_presence._reset_state()
    spool_binding._move_damper.reset()


class _Recorder:
    """Fake dependency hooks: records instead of touching the wire."""

    def __init__(self, settings: dict[str, str] | None = None):
        self.settings = settings if settings is not None else {}
        self.broadcasts: list[dict] = []
        self.identifies: list[tuple[int, int, int, str]] = []
        self.pushes: list[tuple[int, int, int, int]] = []

    async def get_setting(self, key: str) -> str | None:
        return self.settings.get(key)

    async def broadcast(self, payload: dict) -> None:
        self.broadcasts.append(payload)

    async def schedule_identify(self, printer_id: int, ams_id: int, tray_id: int, reason: str) -> None:
        self.identifies.append((printer_id, ams_id, tray_id, reason))

    async def push_config(self, spool, printer_id: int, ams_id: int, tray_id: int, tray: dict) -> bool:
        self.pushes.append((spool.id, printer_id, ams_id, tray_id))
        return True

    def types(self) -> list[str]:
        return [p.get("type") for p in self.broadcasts]


class _FakeClient:
    """Minimal stand-in for a BambuMQTTClient's read-only wire-safety surface.

    The three exist-bit fields are the triage surface wave 2 put on ``PrinterState``
    (``ams_tray_exist_bits`` / ``ams_bits_trusted`` / ``last_full_report_at``), read by
    WS7's release-evidence record. Their defaults are what a client that has never carried
    a mask reports, so every pre-existing test keeps the exact client it had.
    """

    def __init__(
        self,
        *,
        drying: bool = False,
        refusal: str | None = None,
        gcode_state: str | None = None,
        tray_exist_bits: str | None = None,
        bits_trusted: bool = False,
        last_full_report_at: float = 0.0,
    ):
        self._drying = drying
        self._refusal = refusal
        self.state = SimpleNamespace(
            state=gcode_state,
            ams_tray_exist_bits=tray_exist_bits,
            ams_bits_trusted=bits_trusted,
            last_full_report_at=last_full_report_at,
        )

    def ams_unit_drying(self, ams_id: int) -> bool:
        return self._drying

    def ams_write_refusal(self, ams_id: int) -> str | None:
        return self._refusal


@pytest.fixture
def env(monkeypatch):
    """Shared fake settings backing BOTH the pipeline's setting reader and the tagless
    default parser (``spool_tagless._tagless_default`` imports get_setting at call
    time), plus a printer_manager with no live printers."""
    recorder = _Recorder()

    async def fake_get_setting(db, key):
        return recorder.settings.get(key)

    monkeypatch.setattr("backend.app.api.routes.settings.get_setting", fake_get_setting)
    monkeypatch.setattr(spool_tagless.printer_manager, "get_status", lambda pid: None)
    monkeypatch.setattr(spool_tagless.printer_manager, "get_client", lambda pid: None)
    return recorder


@pytest.fixture
def sessions(test_engine, monkeypatch):
    """Point ``spool_tagless``'s own-session opener at the test engine.

    ``note_physical_cycle`` — the production entry that ARMS a qualified cycle — runs its
    prompt lane in a session of its own (it is called from the AMS callback, which has none
    to lend). Tests that must exercise the arming path for real, rather than poking
    ``_pending_physical_cycles``, need that opener to reach this engine. Mirrors the fixture
    of the same name in ``test_spool_tagless.py``.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    import backend.app.core.database as core_db

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(core_db, "async_session", maker)
    return maker


def _deps(db_session, recorder: _Recorder, client=None) -> PipelineDeps:
    return PipelineDeps(
        db=db_session,
        client=client,
        get_setting=recorder.get_setting,
        schedule_identify=recorder.schedule_identify,
        broadcast=recorder.broadcast,
        push_config=recorder.push_config,
    )


# --- helpers ----------------------------------------------------------------


async def _spool(db_session, **kwargs) -> Spool:
    defaults = {"material": "PETG", "rgba": "000000FF", "label_weight": 1000, "core_weight": 250}
    defaults.update(kwargs)
    spool = Spool(**defaults)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    return spool


async def _bind_row(db_session, spool, printer_id, ams_id, tray_id, **kwargs) -> SpoolAssignment:
    """Seed a binding directly (no writer) so a test can start from a bound slot."""
    row = SpoolAssignment(
        spool_id=spool.id,
        printer_id=printer_id,
        ams_id=ams_id,
        tray_id=tray_id,
        fingerprint_color=kwargs.pop("fingerprint_color", "000000FF"),
        fingerprint_type=kwargs.pop("fingerprint_type", "PETG"),
        **kwargs,
    )
    db_session.add(row)
    await db_session.commit()
    return row


async def _assignment(db_session, printer_id, ams_id, tray_id) -> SpoolAssignment | None:
    res = await db_session.execute(
        select(SpoolAssignment).where(
            SpoolAssignment.printer_id == printer_id,
            SpoolAssignment.ams_id == ams_id,
            SpoolAssignment.tray_id == tray_id,
        )
    )
    return res.scalar_one_or_none()


async def _spool_count(db_session) -> int:
    return await db_session.scalar(select(func.count(Spool.id)))


def _obs(printer_id, tray: dict, ams_id: int = 0):
    return observe_tray(printer_id, ams_id, tray)


def _records(caplog, level):
    return [r.getMessage() for r in caplog.records if r.name == _PIPELINE_LOGGER and r.levelno == level]


TAG_A = "AABBCCDD11223344"
TAG_B = "1122334455667788"
TAG_C = "99887766554433221"[:16]  # a THIRD chip — physically impossible on a genuine roll
UUID_1 = "8AC9EC0847FD41D0890870319F2E1975"
UUID_2 = "3C78FA47DFCC4F0C8C95566C77A73DCE"


# --- KEEP -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_keep_refreshes_a_drifted_fingerprint(db_session, printer_factory, env):
    """A same-roll KEEP is not a binding change, but the assignment's fingerprint
    snapshot must track what the slot currently reports (main.py:1938-1953)."""
    printer = await printer_factory()
    spool = await _spool(db_session, tag_uid=TAG_A, tray_uuid=UUID_1)
    await _bind_row(db_session, spool, printer.id, 0, 0, fingerprint_color="000000FF", fingerprint_type="PETG")

    obs = _obs(
        printer.id,
        {"id": 0, "state": 11, "tag_uid": TAG_A, "tray_uuid": UUID_1, "tray_type": "PETG", "tray_color": "00FF00FF"},
    )
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert [t.decision.kind for t in transitions] == [DecisionKind.KEEP]
    assert transitions[0].decision.reason == "identity_matches_bound"
    assert transitions[0].applied is False  # a fingerprint refresh is not a binding change
    row = await _assignment(db_session, printer.id, 0, 0)
    assert (row.fingerprint_color, row.fingerprint_type) == ("00FF00FF", "PETG")
    assert row.spool_id == spool.id
    assert env.broadcasts == []


@pytest.mark.asyncio
async def test_sibling_read_persists_the_pair_and_announces_once(db_session, printer_factory, env, caplog):
    """First sighting of the roll's far chip: KEEP, one INFO, and the tag lands on the row.

    The announcement is the moment we LEARN the second half of the roll's identity, so
    it is also the moment we record it — that is what turns a recurring surprise into a
    one-time event."""
    printer = await printer_factory()
    spool = await _spool(db_session, tag_uid=TAG_A, tray_uuid=UUID_1)
    await _bind_row(db_session, spool, printer.id, 0, 3)

    obs = _obs(
        printer.id,
        {"id": 3, "state": 11, "tag_uid": TAG_B, "tray_uuid": UUID_1, "tray_type": "PETG", "tray_color": "000000FF"},
    )
    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        first = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))
        second = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert first[0].decision.reason == "sibling_tag_read"
    # The pair is recorded, so the SAME push is now an ordinary exact match — silent.
    assert second[0].decision.reason == "identity_matches_bound"
    await db_session.refresh(spool)
    assert spool.sibling_tag_uid == TAG_B
    assert spool.tag_uid == TAG_A, "the near chip is never overwritten — the row gains a pair"

    sibling_lines = [m for m in _records(caplog, logging.INFO) if m.startswith("[sibling-tag]")]
    assert len(sibling_lines) == 1
    assert f"spool={spool.id}" in sibling_lines[0]


@pytest.mark.asyncio
async def test_sibling_reread_after_restart_is_silent(db_session, printer_factory, env, caplog):
    """PROD-SIGNATURE PIN (2026-08-09). Six spools replayed "read its second tag" on
    every push after every restart, forever, because the dedup was a process-lifetime
    set. ``_reset_state()`` is that restart: with the pair on the ROW, the re-read is a
    plain exact match and nothing is announced at all.

    Mutation-verified against the old shape: a process-memory dedup cannot survive this.
    """
    printer = await printer_factory()
    spool = await _spool(db_session, tag_uid=TAG_A, tray_uuid=UUID_1)
    await _bind_row(db_session, spool, printer.id, 0, 3)
    obs = _obs(
        printer.id,
        {"id": 3, "state": 11, "tag_uid": TAG_B, "tray_uuid": UUID_1, "tray_type": "PETG", "tray_color": "000000FF"},
    )
    await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))  # first sighting: persists

    slot_pipeline._reset_state()  # a restart: every in-process ledger is gone

    # Drop the first sighting's (expected) announcement: caplog accumulates across the
    # whole test, so without this the assertion below depends on whatever level another
    # test left the logger at.
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        after_restart = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert after_restart[0].decision.kind is DecisionKind.KEEP
    assert after_restart[0].decision.reason == "identity_matches_bound"
    assert [m for m in _records(caplog, logging.INFO) if m.startswith("[sibling-tag]")] == []


@pytest.mark.asyncio
async def test_sibling_pair_resolves_the_slot_when_only_the_far_chip_is_on_the_wire(
    db_session, printer_factory, env, caplog
):
    """The identity pair must also ANSWER, not merely stay quiet. A push carrying only
    the far chip and NO uuid (the incremental-push shape) previously found no owning row
    at all; with the pair recorded it resolves to this roll — no defer, no mint."""
    printer = await printer_factory()
    spool = await _spool(db_session, tag_uid=TAG_A, tray_uuid=UUID_1, sibling_tag_uid=TAG_B)
    await _bind_row(db_session, spool, printer.id, 0, 3)
    before = await _spool_count(db_session)

    obs = _obs(  # tag only — the atomic-pair rule means no uuid is asserted
        printer.id,
        {"id": 3, "state": 11, "tag_uid": TAG_B, "tray_type": "PETG", "tray_color": "000000FF"},
    )
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.kind is DecisionKind.KEEP
    assert transitions[0].decision.reason == "identity_matches_bound"
    assert await _spool_count(db_session) == before, "the roll's own row answered — nothing minted"


@pytest.mark.asyncio
async def test_a_third_tag_is_refused_and_warned_not_absorbed(db_session, printer_factory, env, caplog):
    """A roll carries exactly TWO chips, so a third read over a uuid-matching binding is
    a misread or a chimera row. Surface it; never absorb it into the pair (overwriting
    would launder the anomaly into a legitimate-looking identity). The WARN is deduped
    because the condition STANDS — it re-derives on every push until a human settles it."""
    printer = await printer_factory()
    spool = await _spool(db_session, tag_uid=TAG_A, tray_uuid=UUID_1, sibling_tag_uid=TAG_B)
    await _bind_row(db_session, spool, printer.id, 0, 3)

    obs = _obs(
        printer.id,
        {"id": 3, "state": 11, "tag_uid": TAG_C, "tray_uuid": UUID_1, "tray_type": "PETG", "tray_color": "000000FF"},
    )
    with caplog.at_level(logging.WARNING, logger=_PIPELINE_LOGGER):
        first = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))
        second = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert first[0].decision.kind is DecisionKind.KEEP, "the uuid matched — the binding is still right"
    assert first[0].decision.reason == "sibling_tag_read"
    assert second[0].decision.reason == "sibling_tag_read", "unrecorded, so it stays anomalous"
    await db_session.refresh(spool)
    assert (spool.tag_uid, spool.sibling_tag_uid) == (TAG_A, TAG_B), "pair untouched"

    third_lines = [m for m in _records(caplog, logging.WARNING) if "THIRD tag" in m]
    assert len(third_lines) == 1, "loud once, not at 1 Hz"
    assert TAG_C in third_lines[0]


# --- BIND -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_moves_the_slot_to_the_uuid_owner(db_session, printer_factory, env):
    """uuid disagreement (both asserted) is positive proof of a different roll: the row
    owning the wire uuid takes the slot and the incumbent is displaced."""
    printer = await printer_factory()
    incumbent = await _spool(db_session, tag_uid=TAG_A, tray_uuid=UUID_1)
    arriving = await _spool(db_session, tag_uid=TAG_B, tray_uuid=UUID_2)
    await _bind_row(db_session, incumbent, printer.id, 0, 1)

    obs = _obs(
        printer.id,
        {"id": 1, "state": 11, "tag_uid": TAG_B, "tray_uuid": UUID_2, "tray_type": "PETG", "tray_color": "000000FF"},
    )
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(
        DecisionKind.BIND, spool_id=arriving.id, reason="identity_resolved_candidate"
    )
    assert transitions[0].applied is True
    row = await _assignment(db_session, printer.id, 0, 1)
    assert row.spool_id == arriving.id
    assert env.types() == ["spool_auto_assigned"]
    assert env.broadcasts[0]["spool_id"] == arriving.id
    assert "origin" not in env.broadcasts[0]  # tagged payload stays byte-identical to the RFID lane


@pytest.mark.asyncio
async def test_damped_bind_writes_nothing_and_the_pass_continues(db_session, printer_factory, env, caplog):
    """The writer refuses a second MOVE of one roll inside its damper window (returns
    None, DB untouched). The pass must treat that as 'no binding change' — no
    broadcast, no applied transition — and keep processing the other slots."""
    printer = await printer_factory()
    roaming = await _spool(db_session, tag_uid=TAG_A, tray_uuid=UUID_1)
    await _bind_row(db_session, roaming, printer.id, 0, 0)
    # Arm the damper as an earlier move would have.
    assert spool_binding._move_damper.allow(roaming.id) is True

    moving = _obs(
        printer.id,
        {"id": 1, "state": 11, "tag_uid": TAG_A, "tray_uuid": UUID_1, "tray_type": "PETG", "tray_color": "000000FF"},
    )
    other = _obs(printer.id, {"id": 2, "state": 9, "tray_type": ""})
    with caplog.at_level(logging.WARNING, logger="backend.app.services.spool_binding"):
        transitions = await run_slot_pipeline(printer.id, [moving, other], _deps(db_session, env))

    assert [t.decision.kind for t in transitions] == [DecisionKind.BIND, DecisionKind.NONE]
    assert transitions[0].applied is False
    assert env.broadcasts == []
    assert await _assignment(db_session, printer.id, 0, 1) is None
    assert (await _assignment(db_session, printer.id, 0, 0)).spool_id == roaming.id


@pytest.mark.asyncio
async def test_seen_set_skips_a_second_application_of_one_spool(db_session, printer_factory, env, caplog):
    """007-H2C spool 194: one tag presenting on two trays in ONE push. Both slots
    legitimately answer BIND (the table is per-slot and pure); the orchestrator applies
    the first and refuses the second — a roll cannot be in two trays."""
    printer = await printer_factory()
    roll = await _spool(db_session, tag_uid=TAG_A, tray_uuid=UUID_1)
    await db_session.commit()

    tray = {"state": 11, "tag_uid": TAG_A, "tray_uuid": UUID_1, "tray_type": "PETG", "tray_color": "000000FF"}
    first = _obs(printer.id, {**tray, "id": 0}, ams_id=1)
    second = _obs(printer.id, {**tray, "id": 1}, ams_id=1)

    with caplog.at_level(logging.WARNING, logger=_PIPELINE_LOGGER):
        transitions = await run_slot_pipeline(printer.id, [first, second], _deps(db_session, env))

    assert [t.applied for t in transitions] == [True, False]
    warnings = _records(caplog, logging.WARNING)
    assert any("duplicate application skipped" in m for m in warnings)
    rows = (
        (await db_session.execute(select(SpoolAssignment).where(SpoolAssignment.spool_id == roll.id))).scalars().all()
    )
    assert len(rows) == 1 and (rows[0].ams_id, rows[0].tray_id) == (1, 0)


# --- pre-configured one-shot ------------------------------------------------


@pytest.mark.asyncio
async def test_pre_configured_apply_clears_the_marker_and_pushes_config(db_session, printer_factory, env):
    """SpoolBuddy weigh-then-assign: the operator bound a roll to an EMPTY slot. The
    moment something is inserted the intent becomes a real location claim (marker
    cleared) and the deferred configuration finally goes out (main.py:2021-2054)."""
    printer = await printer_factory()
    spool = await _spool(db_session, material="PETG", rgba="000000FF")
    await _bind_row(
        db_session,
        spool,
        printer.id,
        0,
        2,
        fingerprint_color="",
        fingerprint_type="",
        pre_configured_at=_SENTINEL,
    )

    obs = _obs(printer.id, {"id": 2, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.BIND, spool_id=spool.id, reason="pre_configured_apply")
    assert transitions[0].from_state is SlotState.PRE_CONFIGURED
    assert transitions[0].applied is True
    row = await _assignment(db_session, printer.id, 0, 2)
    assert row.pre_configured_at is None
    assert (row.fingerprint_color, row.fingerprint_type) == ("000000FF", "PETG")
    assert env.pushes == [(spool.id, printer.id, 0, 2)]


@pytest.mark.asyncio
async def test_pre_configured_apply_on_a_presence_less_dialect(db_session, printer_factory, env):
    """FINDING A, end to end. The A1-family and P1S firmwares report a CONSTANT
    ``state=3``, so presence can only derive to UNKNOWN on them and the old
    ``present is True`` gate left the operator's pre-configured row "awaiting insert"
    forever — roll seated, configured, never applied (upstream #1322)."""
    printer = await printer_factory()
    spool = await _spool(db_session, material="PETG", rgba="000000FF")
    await _bind_row(
        db_session, spool, printer.id, 0, 2, fingerprint_color="", fingerprint_type="", pre_configured_at=_SENTINEL
    )

    obs = _obs(printer.id, {"id": 2, "state": 3, "tray_type": "PETG", "tray_color": "000000FF"})
    assert obs.present is None, "this dialect never reports presence — that is the point"
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.BIND, spool_id=spool.id, reason="pre_configured_apply")
    assert transitions[0].applied is True
    row = await _assignment(db_session, printer.id, 0, 2)
    assert row.pre_configured_at is None  # the one-shot is spent, not stuck
    assert env.pushes == [(spool.id, printer.id, 0, 2)]


@pytest.mark.asyncio
async def test_pre_configured_awaiting_insert_is_left_alone(db_session, printer_factory, env):
    """A deliberate bind-to-empty is state, not a location claim: release-on-empty must
    never delete the operator's intent."""
    printer = await printer_factory()
    spool = await _spool(db_session)
    await _bind_row(
        db_session, spool, printer.id, 0, 2, fingerprint_color="", fingerprint_type="", pre_configured_at=_SENTINEL
    )

    obs = _obs(printer.id, {"id": 2, "state": 9, "tray_type": ""})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.reason == "pre_configured_awaiting_insert"
    assert transitions[0].applied is False
    assert (await _assignment(db_session, printer.id, 0, 2)).pre_configured_at == _SENTINEL


# --- MINT -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_creates_a_tagged_row_from_the_full_pair(db_session, printer_factory, env):
    """Full identity pair, unknown to inventory, auto-add on → a genuinely new roll."""
    printer = await printer_factory()
    obs = _obs(
        printer.id,
        {
            "id": 0,
            "state": 11,
            "tag_uid": TAG_A,
            "tray_uuid": UUID_1,
            "tray_type": "PETG",
            "tray_color": "112233FF",
            "tray_sub_brands": "PETG HF",
            "remain": 100,
        },
    )
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.kind is DecisionKind.MINT
    assert transitions[0].decision.reason == "unknown_identity_auto_add"
    assert transitions[0].applied is True
    row = await _assignment(db_session, printer.id, 0, 0)
    minted = await db_session.get(Spool, row.spool_id)
    assert (minted.tag_uid, minted.tray_uuid) == (TAG_A, UUID_1)
    assert minted.data_origin == "rfid_auto"
    assert env.types() == ["spool_auto_assigned"]
    assert env.pushes == []  # the firmware already holds a tagged roll's identity


@pytest.mark.asyncio
async def test_mint_is_refused_when_auto_add_is_off(db_session, printer_factory, env):
    printer = await printer_factory()
    env.settings["auto_add_unknown_rfid"] = "false"
    obs = _obs(
        printer.id,
        {"id": 0, "state": 11, "tag_uid": TAG_A, "tray_uuid": UUID_1, "tray_type": "PETG", "tray_color": "112233FF"},
    )
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.NONE, reason="unknown_tag_prompt_owed")
    assert await _spool_count(db_session) == 0


@pytest.mark.asyncio
async def test_tagless_mint_from_the_tray(db_session, printer_factory, env):
    """A configured tray with no tag and nothing to reclaim mints a silently-tracked
    row from the tray's own configuration."""
    printer = await printer_factory()
    obs = _obs(
        printer.id,
        {"id": 1, "state": 11, "tray_type": "PLA", "tray_color": "FF0000FF", "tray_info_idx": "GFL99"},
    )
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.reason == "tagless_mint"
    minted = await db_session.get(Spool, (await _assignment(db_session, printer.id, 0, 1)).spool_id)
    assert minted.data_origin == "ams_auto"
    assert (minted.material, minted.rgba) == ("PLA", "FF0000FF")
    assert minted.tag_uid is None and minted.tray_uuid is None
    assert env.broadcasts[0]["origin"] == "tagless"  # the frontend toasts on this


@pytest.mark.asyncio
async def test_mint_recheck_converts_to_bind_when_an_owner_appears(db_session, printer_factory, env, monkeypatch):
    """The recheck exists for the window between resolve and apply (a concurrent pass,
    or a caller that resolved candidates badly). Forcing a MINT verdict while a row
    already owns the identity must produce a BIND of that row — never a twin."""
    printer = await printer_factory()
    owner = await _spool(db_session, tag_uid=TAG_A, tray_uuid=UUID_1)
    await db_session.commit()

    obs = _obs(
        printer.id,
        {"id": 0, "state": 11, "tag_uid": TAG_A, "tray_uuid": UUID_1, "tray_type": "PETG", "tray_color": "000000FF"},
    )
    forced = Decision(
        DecisionKind.MINT,
        mint_spec={"source": "tray", "tag_uid": TAG_A, "tray_uuid": UUID_1, "tray_type": "PETG"},
        reason="unknown_identity_auto_add",
    )
    monkeypatch.setattr(slot_pipeline, "resolve", lambda obs, state, ctx: forced)

    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.kind is DecisionKind.BIND
    assert transitions[0].decision.spool_id == owner.id
    assert transitions[0].applied is True
    assert await _spool_count(db_session) == 1  # no twin row
    assert (await _assignment(db_session, printer.id, 0, 0)).spool_id == owner.id


class TestUntaggedClaim:
    """FINDING B — a newly-identified roll lands on the operator's row for it.

    Pre-cutover an unknown tag first tried ``find_matching_untagged_spool`` and, on a
    hit, ``link_tag_to_inventory_spool`` moved the identity ONTO that row; a bound slot
    never reached the mint lane at all, so a weigh-then-assign pre-config row simply
    received its roll's tag at insert. After the cutover the table minted a fresh
    ``rfid_auto`` row (default 1000 g label) beside it and orphaned the weighed one.

    Both lanes are pinned here through the REAL writer and the REAL linker, because what
    makes them correct is what they do NOT do: no second row, no displacement, no config
    push over an RFID-owned slot.
    """

    TAG = "AABBCCDD00000100"
    UUID = "8AC9EC0847FD41D0890870319F2E1975"

    def _tagged(self, printer_id, tray_id=0, **overrides):
        tray = {
            "id": tray_id,
            "state": 11,
            "tag_uid": self.TAG,
            "tray_uuid": self.UUID,
            "tray_type": "PETG",
            "tray_color": "000000FF",
            "tray_info_idx": "GFG02",
        }
        tray.update(overrides)
        return _obs(printer_id, tray)

    @pytest.mark.asyncio
    async def test_a_weighed_pre_config_row_receives_the_inserted_rolls_tag(self, db_session, printer_factory, env):
        """THE REPLAY. SpoolBuddy weigh-then-assign: the operator weighed a 750 g roll,
        bound it to an EMPTY slot, and is now inserting it. The tag the AMS reads at
        insert belongs to that row — so the row keeps the slot, takes the identity, and
        the measured label survives."""
        printer = await printer_factory()
        weighed = await _spool(
            db_session, material="PETG", rgba="000000FF", label_weight=750, data_origin="manual", brand="Bambu Lab"
        )
        await _bind_row(
            db_session,
            weighed,
            printer.id,
            0,
            2,
            fingerprint_color="",
            fingerprint_type="",
            pre_configured_at=_SENTINEL,
        )

        transitions = await run_slot_pipeline(printer.id, [self._tagged(printer.id, tray_id=2)], _deps(db_session, env))

        assert transitions[0].decision == Decision(
            DecisionKind.BIND, spool_id=weighed.id, reason="pre_configured_apply_identity"
        )
        assert transitions[0].applied is True
        # The row KEEPS the slot and the one-shot marker is spent.
        row = await _assignment(db_session, printer.id, 0, 2)
        assert row.spool_id == weighed.id
        assert row.pre_configured_at is None
        assert (row.fingerprint_color, row.fingerprint_type) == ("000000FF", "PETG")
        # The identity is LINKED onto it — through the fork's one linker, so the origin
        # and the slicer-preset backfill match the pre-cutover attract lane.
        await db_session.refresh(weighed)
        assert (weighed.tag_uid, weighed.tray_uuid) == (self.TAG, self.UUID)
        assert weighed.data_origin == "rfid_linked"
        assert weighed.slicer_filament == "GFG02"
        # The operator's measurement is untouched, and no stranger row exists.
        assert weighed.label_weight == 750
        assert await _spool_count(db_session) == 1
        # The roll's configuration is RFID-owned: pushing ams_filament_setting over a
        # BL-read slot destroys the RFID-detected state (auto_assign_spool's rule).
        assert env.pushes == []
        assert env.types() == ["spool_auto_assigned"]
        assert "origin" not in env.broadcasts[0]  # tagged shape, never origin="tagless"

    @pytest.mark.asyncio
    async def test_an_untagged_inventory_row_attracts_the_tag(self, db_session, printer_factory, env):
        """The attract lane on an UNBOUND slot: an operator logged the roll (Quick Add,
        no subtype, no tag) and now inserts it. ``find_matching_untagged_spool``'s
        criteria decide, and the tag lands rather than duplicating the row."""
        printer = await printer_factory()
        logged = await _spool(
            db_session, material="PETG", rgba="000000FF", brand="Bambu Lab", data_origin="manual", label_weight=1000
        )
        await db_session.commit()

        transitions = await run_slot_pipeline(printer.id, [self._tagged(printer.id)], _deps(db_session, env))

        assert transitions[0].decision == Decision(
            DecisionKind.BIND, spool_id=logged.id, reason="identity_claims_untagged_row"
        )
        assert transitions[0].applied is True
        assert (await _assignment(db_session, printer.id, 0, 0)).spool_id == logged.id
        await db_session.refresh(logged)
        assert (logged.tag_uid, logged.tray_uuid) == (self.TAG, self.UUID)
        assert logged.data_origin == "rfid_linked"
        assert await _spool_count(db_session) == 1  # attracted, not duplicated
        assert env.pushes == []

    @pytest.mark.asyncio
    async def test_an_auto_minted_tagless_row_never_attracts_a_tag(self, db_session, printer_factory, env):
        """The attract lane is for MANUALLY logged rolls only. The farm's own
        silently-tracked ``ams_auto`` rows are excluded by the finder, so an arriving tag
        mints its own row instead of hijacking one (the silent-tracking work item)."""
        printer = await printer_factory()
        auto = await _spool(db_session, material="PETG", rgba="000000FF", data_origin="ams_auto")
        await db_session.commit()

        transitions = await run_slot_pipeline(printer.id, [self._tagged(printer.id)], _deps(db_session, env))

        assert transitions[0].decision.reason == "unknown_identity_auto_add"
        await db_session.refresh(auto)
        assert auto.tag_uid is None  # untouched
        assert await _spool_count(db_session) == 2

    @pytest.mark.asyncio
    async def test_a_different_filament_does_not_attract(self, db_session, printer_factory, env):
        """MINT still fires (case d): a row that is not plausibly this roll is not a
        claim candidate at all."""
        printer = await printer_factory()
        other = await _spool(db_session, material="PLA", rgba="FF0000FF", brand="Bambu Lab", data_origin="manual")
        await db_session.commit()

        transitions = await run_slot_pipeline(printer.id, [self._tagged(printer.id)], _deps(db_session, env))

        assert transitions[0].decision.reason == "unknown_identity_auto_add"
        await db_session.refresh(other)
        assert other.tag_uid is None
        assert await _spool_count(db_session) == 2

    @pytest.mark.asyncio
    async def test_a_partial_read_never_lands_a_tag_on_an_operator_row(self, db_session, printer_factory, env):
        """Case (e) at the orchestrator. The candidate IS resolved and handed over; the
        TABLE's full-pair gate is what refuses it. Writing a half-read identity onto a
        row the operator owns is strictly worse than minting from one."""
        printer = await printer_factory()
        logged = await _spool(db_session, material="PETG", rgba="000000FF", brand="Bambu Lab", data_origin="manual")
        await db_session.commit()

        # Tag asserted, uuid NOT carried by this frame — the sibling-chip read shape.
        partial = _obs(
            printer.id,
            {"id": 0, "state": 11, "tag_uid": self.TAG, "tray_type": "PETG", "tray_color": "000000FF"},
        )
        assert partial.tray_uuid is None
        transitions = await run_slot_pipeline(printer.id, [partial], _deps(db_session, env))

        assert transitions[0].decision == Decision(DecisionKind.DEFER, reason="partial_identity_owed_full_read")
        await db_session.refresh(logged)
        assert logged.tag_uid is None  # the operator's row is untouched
        assert await _assignment(db_session, printer.id, 0, 0) is None
        assert env.identifies == [(printer.id, 0, 0, "partial_identity_owed_full_read")]
        assert await _spool_count(db_session) == 1


# --- RECLAIM ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_reclaim_rebinds_the_last_location_row_preserving_the_ordinal(db_session, printer_factory, env):
    """Doctrine rule 7: a roll pulled for drying and returned to the SAME slot is the
    SAME roll — grams continue AND the FIFO seating position is kept."""
    printer = await printer_factory()
    donor = await _spool(db_session, material="PETG", rgba="000000FF", weight_used=400)
    donor.last_location_printer_id = printer.id
    donor.last_location_ams_id = 0
    donor.last_location_tray_id = 3
    donor.last_location_at = datetime.utcnow()
    donor.loaded_at = _SENTINEL
    donor.first_loaded_at = _SENTINEL
    await db_session.commit()

    obs = _obs(printer.id, {"id": 3, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})
    _seed_reseat(printer.id, 0, 3, absent_for=30.0)
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.RECLAIM, spool_id=donor.id, reason="reseat_debounce")
    assert transitions[0].applied is True
    assert (await _assignment(db_session, printer.id, 0, 3)).spool_id == donor.id
    await db_session.refresh(donor)
    assert donor.loaded_at == _SENTINEL  # ordinal preserved through the REAL writer
    assert donor.weight_used == 400
    assert await _spool_count(db_session) == 1  # reclaimed, not re-minted


@pytest.mark.asyncio
async def test_a_different_filament_at_the_last_location_mints_instead(db_session, printer_factory, env):
    """The reclaim donor must fingerprint-match the wire; a different filament is a
    different roll and gets its own row."""
    printer = await printer_factory()
    donor = await _spool(db_session, material="PETG", rgba="000000FF")
    donor.last_location_printer_id = printer.id
    donor.last_location_ams_id = 0
    donor.last_location_tray_id = 3
    donor.last_location_at = datetime.utcnow()
    await db_session.commit()

    obs = _obs(printer.id, {"id": 3, "state": 11, "tray_type": "PLA", "tray_color": "FF0000FF"})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.reason == "tagless_mint"
    assert (await _assignment(db_session, printer.id, 0, 3)).spool_id != donor.id


@pytest.mark.asyncio
async def test_reclaim_never_steals_a_live_binding(db_session, printer_factory, env):
    """Assumption-tier evidence may never displace a POSITIVE location claim.

    prod 2026-08-07, spool 211: the operator moved the roll from printer 10 A0T0 to
    printer 7 A0T1. The identity lane bound it at p7 — correct, the wire read that roll's
    own tag THERE — and the writer's move semantics swept p10's row. But p10's tray still
    reported config residue (another roll seated, no identity asserted) and the roll's
    ``last_location_*`` still pointed at p10, so the reclaim lane took it straight back and
    the two lanes ping-ponged at ~1 Hz. A donor that is bound ELSEWHERE is not a donor: the
    seated unknown roll mints its own row and the live binding is untouched.
    """
    p_a = await printer_factory()
    p_b = await printer_factory()
    donor = await _spool(db_session, material="PETG", rgba="000000FF", weight_used=400)
    donor.last_location_printer_id = p_a.id
    donor.last_location_ams_id = 0
    donor.last_location_tray_id = 3
    donor.last_location_at = datetime.utcnow()
    await _bind_row(db_session, donor, p_b.id, 0, 1)  # the LIVE claim, on another printer

    obs = _obs(p_a.id, {"id": 3, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})
    transitions = await run_slot_pipeline(p_a.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.reason == "tagless_mint"  # NOT last_location_reclaim
    assert transitions[0].applied is True
    minted = await _assignment(db_session, p_a.id, 0, 3)
    assert minted is not None
    assert minted.spool_id != donor.id
    live = await _assignment(db_session, p_b.id, 0, 1)
    assert live is not None
    assert live.spool_id == donor.id  # the roll never left the slot the wire put it in
    await db_session.refresh(donor)
    assert donor.weight_used == 400


@pytest.mark.asyncio
async def test_reclaim_still_works_for_a_returned_roll(db_session, printer_factory, env):
    """Control arm for the guard above: the SAME setup with the donor bound NOWHERE still
    reclaims. A live assignment is the only variable between the two cases, so the pair
    pins the exclusion without eroding doctrine rule 7's gram + FIFO continuity."""
    printer = await printer_factory()
    donor = await _spool(db_session, material="PETG", rgba="000000FF", weight_used=400)
    donor.last_location_printer_id = printer.id
    donor.last_location_ams_id = 0
    donor.last_location_tray_id = 3
    donor.last_location_at = datetime.utcnow()
    await db_session.commit()

    obs = _obs(printer.id, {"id": 3, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})
    _seed_reseat(printer.id, 0, 3, absent_for=30.0)
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.RECLAIM, spool_id=donor.id, reason="reseat_debounce")
    assert transitions[0].applied is True
    assert (await _assignment(db_session, printer.id, 0, 3)).spool_id == donor.id
    assert await _spool_count(db_session) == 1  # de-bounced, not re-minted


@pytest.mark.asyncio
async def test_an_identity_move_does_not_ping_pong_back_to_the_old_slot(db_session, printer_factory, env):
    """The 2026-08-07 loop end to end, through the real writer.

    Pass 1 is the identity bind on the new printer, whose move sweep is what stamps the
    roll's ``last_location_*`` at the OLD slot. Pass 2 evaluates that vacated slot, which
    still reports the departed roll's config and asserts no identity — the exact shape that
    used to reclaim the roll back and restart the loop.

    The DECISION is the pin, not merely the final ledger: a reclaim on pass 2 is a MOVE, so
    the writer's damper would refuse this one and land the next, which is precisely why prod
    ping-ponged at one write per ``spool_binding._MOVE_DAMPER_S`` instead of converging.
    """
    old = await printer_factory()
    new = await printer_factory()
    roll = await _spool(db_session, tag_uid=TAG_A, tray_uuid=UUID_1, material="PETG", rgba="000000FF", weight_used=612)
    await _bind_row(db_session, roll, old.id, 0, 0)

    # Pass 1 — the roll is now in the new printer and its wire reads the tag there.
    arrived = _obs(
        new.id,
        {"id": 1, "state": 11, "tag_uid": TAG_A, "tray_uuid": UUID_1, "tray_type": "PETG", "tray_color": "000000FF"},
    )
    moved = await run_slot_pipeline(new.id, [arrived], _deps(db_session, env))

    assert moved[0].decision == Decision(DecisionKind.BIND, spool_id=roll.id, reason="identity_resolved_candidate")
    await db_session.refresh(roll)
    assert (roll.last_location_printer_id, roll.last_location_ams_id, roll.last_location_tray_id) == (old.id, 0, 0)

    # Pass 2 — the vacated slot: config residue from another seated roll, no identity.
    residue = _obs(old.id, {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})
    back = await run_slot_pipeline(old.id, [residue], _deps(db_session, env))

    assert back[0].decision.reason == "tagless_mint"  # NOT last_location_reclaim
    assert (await _assignment(db_session, new.id, 0, 1)).spool_id == roll.id  # the bind stayed put
    assert (await _assignment(db_session, old.id, 0, 0)).spool_id != roll.id


@pytest.mark.asyncio
async def test_debounce_candidate_uses_shared_stmt(db_session, printer_factory, env, monkeypatch):
    """One question, one origin — and the caller's own adjudication stays the caller's.

    ``spool_binding.last_released_from_slot_stmt`` is the shared shape of "rows whose last
    release was FROM this slot" (matching triple, newest first, unbound fleet-wide), asked
    by this lane to find a RETURNING roll and by ``spool_respool``'s spent tier 2 to find
    an EXHAUSTED one — the AMS empties a drained bay minutes before it declares the runout,
    so both lanes read the same residue. The spent/archived exclusions must stay HERE and
    must never migrate into the shared stmt: the spent lane has to SEE a spent newest row
    to answer a duplicate trigger idempotently, while for reclaim that same row is no donor.
    """
    printer = await printer_factory()
    donor = await _spool(db_session, material="PETG", rgba="000000FF", weight_used=400)
    donor.last_location_printer_id = printer.id
    donor.last_location_ams_id = 0
    donor.last_location_tray_id = 3
    donor.last_location_at = datetime.utcnow()
    await db_session.commit()

    calls: list[tuple[int, int, int]] = []
    real = spool_binding.last_released_from_slot_stmt

    def spy(printer_id, ams_id, tray_id):
        calls.append((printer_id, ams_id, tray_id))
        return real(printer_id, ams_id, tray_id)

    monkeypatch.setattr(spool_binding, "last_released_from_slot_stmt", spy)
    obs = _obs(printer.id, {"id": 3, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})

    assert await slot_pipeline._debounce_candidate(db_session, obs) is donor
    assert calls == [(printer.id, 0, 3)]  # routed through the shared origin, with THIS slot

    # The shared stmt would hand both of these over; reclaim's own filters refuse them.
    donor.spent_at = datetime.utcnow()
    await db_session.commit()
    assert await slot_pipeline._debounce_candidate(db_session, obs) is None

    donor.spent_at = None
    donor.archived_at = datetime.utcnow()
    await db_session.commit()
    assert await slot_pipeline._debounce_candidate(db_session, obs) is None
    assert len(calls) == 3  # every one of the three passes asked the shared origin


# --- the physical cycle's lifecycle (shape 32, layer 2) ---------------------
#
# A qualified physical cycle is per-slot swap currency. Its lifecycle decision used to sit
# in ``spool_tagless._maybe_prompt_fresh_roll`` — a PROMPT function running inside the very
# await that arms the entry, i.e. before any outcome exists — so it guessed
# ``spool is None ⇒ discard``, which is exactly backwards for a refill inside the ~3-minute
# bay-clear→HMS gap. ``slot_pipeline._settle_physical_cycle`` owns it now, per outcome, in
# the apply step of the deciding pass. These pin all of it: what survives, what is retired,
# and the bound that keeps a survivor from replaying as a phantom swap later.


async def _debounce_donor(db_session, printer_id, *, tray_id=3, **kwargs):
    """A roll released from a slot that is a legitimate de-bounce donor for it."""
    donor = await _spool(db_session, material="PETG", rgba="000000FF", **kwargs)
    donor.last_location_printer_id = printer_id
    donor.last_location_ams_id = 0
    donor.last_location_tray_id = tray_id
    donor.last_location_at = datetime.utcnow()
    await db_session.commit()
    return donor


_SEATED_PETG = {"state": 11, "tray_type": "PETG", "tray_color": "000000FF"}


@pytest.mark.asyncio
async def test_a_debounce_preserves_the_cycle_for_the_imminent_spent_stamp(db_session, printer_factory, env, sessions):
    """T8b/T8c's first half, at the pipeline seam.

    The firmware auto-refilled to a backup before the bit cleared (or a restart ate the
    loss-edge stamp), so the cause test is blind and the refill DE-BOUNCES onto the
    still-unspent exhausted row. The cycle must outlive that resolution: the runout HMS is
    still ~3 minutes away, and when it stamps ``spent_at`` this cycle is the only thing that
    can drive row 4a's ``REPLACE_SPENT``.
    """
    printer = await printer_factory()
    donor = await _debounce_donor(db_session, printer.id, weight_used=900)

    await spool_tagless.note_physical_cycle(printer.id, 0, 3)  # the operator's refill
    assert spool_tagless.qualified_cycle_pending(printer.id, 0, 3) is True

    _seed_reseat(printer.id, 0, 3, absent_for=30.0)
    obs = _obs(printer.id, {"id": 3, **_SEATED_PETG})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.RECLAIM, spool_id=donor.id, reason="reseat_debounce")
    assert spool_tagless.qualified_cycle_pending(printer.id, 0, 3) is True, (
        "the de-bounce re-bound an EXISTING row that evidence in flight may still declare exhausted"
    )


@pytest.mark.asyncio
async def test_a_mint_discards_the_cycle_so_a_later_spent_row_cannot_replay_it(
    db_session, printer_factory, env, sessions
):
    """THE LEAK BOUND, and the reason the discard moved into the pipeline at all.

    A cycle left pending forever would let a LATER spent binding on the same slot fire
    ``REPLACE_SPENT`` with no physical event behind it — a phantom swap, minting a stranger
    over a row nobody touched. A MINT is where that bound is drawn: a fresh row has no spent
    transition coming, so its arrival answers the cycle.

    The third act is the liveness half (memory ``liveness-paired-verification``): a cured
    replay and a dead swap lane look identical on "nothing happened", so the same slot is
    then given a REAL new cycle and must swap on it.
    """
    printer = await printer_factory()
    # Outside the window on purpose: an UNKNOWN absence never de-bounces, so this pass mints.
    await _debounce_donor(db_session, printer.id, weight_used=900)

    await spool_tagless.note_physical_cycle(printer.id, 0, 3)
    obs = _obs(printer.id, {"id": 3, **_SEATED_PETG})
    deps = _deps(db_session, env)
    minted_pass = await run_slot_pipeline(printer.id, [obs], deps)

    assert minted_pass[0].decision.reason == "tagless_mint"
    assert spool_tagless.qualified_cycle_pending(printer.id, 0, 3) is False, "the mint answered the cycle"

    # The minted roll later runs dry. With no fresh physical event, the slot must LATCH.
    minted = await db_session.get(Spool, (await _assignment(db_session, printer.id, 0, 3)).spool_id)
    minted.spent_at = datetime.utcnow()
    await db_session.commit()
    latched = await run_slot_pipeline(printer.id, [obs], deps)

    assert latched[0].decision == Decision(DecisionKind.KEEP, spool_id=minted.id, reason="spent_latch")
    assert (await _assignment(db_session, printer.id, 0, 3)).spool_id == minted.id

    # Liveness: a genuine roll swap on that same slot still releases the latch.
    await spool_tagless.note_physical_cycle(printer.id, 0, 3)
    swapped = await run_slot_pipeline(printer.id, [obs], deps)

    assert swapped[0].decision.reason == "spent_swap_confirmed"
    assert swapped[0].applied is True
    # Identity by STATE, not by id: the departed row was pristine, so it is hard-deleted and
    # SQLite hands its freed rowid straight to the replacement.
    successor = await db_session.get(Spool, (await _assignment(db_session, printer.id, 0, 3)).spool_id)
    assert successor.spent_at is None, "the slot is live again, not latched"
    assert await db_session.scalar(select(func.count(Spool.id)).where(Spool.spent_at.is_not(None))) == 0


# The 009-H2S spool-290 ledger, verbatim (``test_spent_attribution``'s fixture values):
# a row that entered service at _LEDGER_T0, was re-bound at _LEDGER_T1, and reads 1200.5 g
# used on a 1000 g label.
_LEDGER_T0 = datetime(2026, 8, 11, 9, 0, 0)
_LEDGER_T1 = datetime(2026, 8, 12, 9, 56, 0)


@pytest.mark.asyncio
async def test_a_debounce_stamps_no_swap_boundary_for_the_overcharge_reconciler(db_session, printer_factory, env):
    """The WS1 ↔ 2026-08-12 interaction a reviewer would not go looking for.

    ``SpoolAssignment.created_at`` is not decoration: ``reconcile_ledger_overcharges`` reads
    it as the ONE instant at which an unobserved roll swap could have happened. A de-bounce
    is the farm asserting that NOTHING physically happened, so stamping a fresh boundary
    there would be actively false — and a de-bounced roll that later overshoots its label
    would be split into a phantom successor for a swap that provably did not occur.

    The control arm is the point of the test: the SAME row, differing only in the
    assignment's ``created_at``, is reconciled. So the stand-down is caused by the carried
    bind moment and by nothing else — and the reconciler itself is still alive (C5, the
    operator manually assigning an old row to a slot holding a new roll, remains reachable).
    """
    printer = await printer_factory()
    donor = await _debounce_donor(
        db_session, printer.id, weight_used=1200.5, created_at=_LEDGER_T0, loaded_at=_LEDGER_T0
    )
    for i, (grams, at) in enumerate(((429.2, _LEDGER_T0), (406.9, _LEDGER_T1), (364.4, _LEDGER_T1))):
        db_session.add(
            SpoolUsageHistory(
                spool_id=donor.id, print_name=f"charge-{i}", weight_used=grams, created_at=at + timedelta(hours=1)
            )
        )
    await db_session.commit()

    _seed_reseat(printer.id, 0, 3, absent_for=30.0)
    obs = _obs(printer.id, {"id": 3, **_SEATED_PETG})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.reason == "reseat_debounce"
    row = await _assignment(db_session, printer.id, 0, 3)
    assert row.created_at == _LEDGER_T0, "the bind moment is carried, not re-stamped"

    assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 0
    await db_session.refresh(donor)
    assert donor.archived_at is None and donor.weight_used == pytest.approx(1200.5)
    assert await _spool_count(db_session) == 1  # no phantom successor

    # Control arm: the pre-wave stamp — a boundary meaningfully later than the row's own
    # creation — and the very same sweep acts.
    row.created_at = _LEDGER_T1
    await db_session.commit()
    assert await spool_tagless.reconcile_ledger_overcharges(db_session) == 1
    await db_session.refresh(donor)
    assert donor.archived_at is not None


# --- RELEASE ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_on_cleared_tray_stamps_last_location_and_deletes(db_session, printer_factory, env, caplog):
    """Operator ruling 1: the assignment claims WHERE a roll is, so a cleared tray
    drops the claim — while the spool keeps its grams and gains the reclaim stamp."""
    printer = await printer_factory()
    spool = await _spool(db_session, material="PETG", rgba="000000FF", weight_used=932)
    await _bind_row(db_session, spool, printer.id, 0, 2)

    obs = _obs(printer.id, {"id": 2, "state": 9, "tray_type": ""})
    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.RELEASE, spool_id=spool.id, reason="cleared_tray")
    assert transitions[0].applied is True
    assert await _assignment(db_session, printer.id, 0, 2) is None
    await db_session.refresh(spool)
    assert (spool.last_location_printer_id, spool.last_location_ams_id, spool.last_location_tray_id) == (
        printer.id,
        0,
        2,
    )
    assert spool.weight_used == 932  # grams live on the row, not the binding
    assert any("→EMPTY release" in m for m in _records(caplog, logging.INFO))
    assert env.types() == ["spool_assignment_changed"]


@pytest.fixture
def dismissed(monkeypatch):
    """Record every ``tagless_fresh_prompt_dismissed`` broadcast the pass emits.

    The payload has ONE owner (``spool_tagless.broadcast_tagless_fresh_dismissed`` — the
    same call the operator's own answer makes), so the pipeline calls it rather than
    re-spelling the event through ``deps.emit``; this stands in for it.
    """
    calls: list[tuple[int, int, int]] = []

    async def _record(printer_id: int, ams_id: int, tray_id: int) -> None:
        calls.append((printer_id, ams_id, tray_id))

    monkeypatch.setattr(slot_pipeline.spool_tagless, "broadcast_tagless_fresh_dismissed", _record)
    return calls


@pytest.mark.asyncio
async def test_release_disposes_a_never_fed_provisional_ghost(db_session, printer_factory, env):
    """A never-fed ``ams_auto`` row abandoned by an emptied tray is a GHOST: no grams, no
    identity, no operator edits — nothing to keep. Left behind it becomes 0 g clutter in
    Inventory (prod 2026-08-07: spools 239-242), so the release routes it through the ONE
    disposal, which hard-deletes a pristine row."""
    printer = await printer_factory()
    ghost = await _spool(db_session, material="PETG", rgba="000000FF", data_origin="ams_auto")

    await _bind_row(db_session, ghost, printer.id, 0, 2)

    obs = _obs(printer.id, {"id": 2, "state": 9, "tray_type": ""})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.RELEASE, spool_id=ghost.id, reason="cleared_tray")
    assert await _assignment(db_session, printer.id, 0, 2) is None
    assert await _spool_count(db_session) == 0  # the ghost is gone, not archived


@pytest.mark.asyncio
async def test_release_never_disposes_a_row_that_has_fed(db_session, printer_factory, env):
    """The gate is grams, and the boundary is ``NEVER_FED_MAX_G``: a row at or above it
    holds real consumption history (doctrine rule 4 — usage charges are the tagless truth
    source), so a release only ever UNBINDS it. Its grams and its reclaim stamp stay."""
    printer = await printer_factory()
    fed = await _spool(
        db_session,
        material="PETG",
        rgba="000000FF",
        data_origin="ams_auto",
        weight_used=spool_binding.NEVER_FED_MAX_G,
    )
    await _bind_row(db_session, fed, printer.id, 0, 2)

    obs = _obs(printer.id, {"id": 2, "state": 9, "tray_type": ""})
    await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    await db_session.refresh(fed)
    assert fed.archived_at is None
    assert fed.weight_used == spool_binding.NEVER_FED_MAX_G
    assert fed.last_location_tray_id == 2  # unbound inventory, reclaimable
    assert await _spool_count(db_session) == 1


@pytest.mark.asyncio
async def test_release_clears_a_pending_fresh_prompt_and_dismisses_the_toast(
    db_session, printer_factory, env, dismissed
):
    """The fresh-roll prompt names a SLOT, so once the roll's location claim is gone the
    question has no subject: the stamp is NULLed by the unbind writer and every open
    client is told to drop the toast (2026-08-07: a stale prompt replayed for days for a
    slot whose roll had left)."""
    printer = await printer_factory()
    spool = await _spool(
        db_session,
        material="PETG",
        rgba="000000FF",
        data_origin="ams_auto",
        weight_used=800,
        fresh_prompt_pending_at=datetime.utcnow(),
    )
    await _bind_row(db_session, spool, printer.id, 0, 2)

    obs = _obs(printer.id, {"id": 2, "state": 9, "tray_type": ""})
    await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    await db_session.refresh(spool)
    assert spool.fresh_prompt_pending_at is None
    assert dismissed == [(printer.id, 0, 2)]


@pytest.mark.asyncio
async def test_release_of_an_unstamped_row_dismisses_nothing(db_session, printer_factory, env, dismissed):
    """No stamp, no toast: the dismissal rides the writer's signal, never a blind emit."""
    printer = await printer_factory()
    spool = await _spool(db_session, material="PETG", rgba="000000FF", weight_used=800)
    await _bind_row(db_session, spool, printer.id, 0, 2)

    obs = _obs(printer.id, {"id": 2, "state": 9, "tray_type": ""})
    await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert dismissed == []


# --- WS7: release evidence (diagnosis only) ---------------------------------
#
# The wave that scoped the de-bounce lane measured 8 days of production: of 52
# last-location reclaims with a matched prior release on the same slot, 14 came back in
# under five minutes and four at 0.0 minutes — four printers, one minute, the same spool.
# Those are SPURIOUS releases, and since wave 2 made a cleared exist bit
# release-authorizing one glitched bit hard-DELETES a binding. The de-bounce contains that
# damage; nothing explained it. These pin the record that makes the next one explainable
# from the log alone, and — the assertion that matters most — that collecting it moved
# nothing.

_EVIDENCE = "[slot-state] release-evidence"

# The 003-H2S T2 cleared shape, plus a mask whose bit for that slot is CLEAR: what the
# wire looks like on the branch a single glitched bit would travel down.
_CLEARED_TRAY = {"id": 2, "state": 9, "tray_type": ""}


def _evidence_line(caplog) -> str:
    lines = [m for m in _records(caplog, logging.INFO) if m.startswith(_EVIDENCE)]
    assert len(lines) == 1, f"exactly one evidence line per release, got {lines}"
    return lines[0]


def _fields(line: str) -> dict[str, str]:
    """The line's ``key=value`` grammar as a dict — the token order is the contract, so a
    test that reads it back this way fails loudly if a field is dropped or renamed."""
    return dict(token.split("=", 1) for token in line.split(" ") if "=" in token)


async def _released_with_evidence(db_session, printer, env, caplog, *, client=None, exist_bits=None, **spool_kwargs):
    """Release a bound roll off A0T2 and return ``(transitions, spool, fields)``."""
    defaults = {"material": "PETG", "rgba": "000000FF", "weight_used": 954.4}
    defaults.update(spool_kwargs)
    spool = await _spool(db_session, **defaults)
    await _bind_row(db_session, spool, printer.id, 0, 2)

    obs = observe_tray(printer.id, 0, dict(_CLEARED_TRAY), exist_bits=exist_bits)
    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env, client))
    return transitions, spool, _fields(_evidence_line(caplog))


@pytest.mark.asyncio
async def test_the_release_record_carries_every_field_the_reconstruction_needs(
    db_session, printer_factory, env, caplog
):
    """One line, and it answers the whole question without a live reproduction.

    The wire here is the glitch-suspect shape: a TRUSTED in-push mask whose bit for the
    slot is CLEAR, delivered on a full report. That is the invariant-12 authority a single
    flipped bit would travel down, so it must read back as such — ``rule=bit_clear``
    (not the fallback tier), ``mask_src=push`` (this push's mask, not a cached copy),
    ``push=full``.
    """
    printer = await printer_factory()
    client = _FakeClient(tray_exist_bits="0", bits_trusted=True, last_full_report_at=time.monotonic())

    transitions, spool, f = await _released_with_evidence(
        db_session, printer, env, caplog, client=client, exist_bits=0, label_weight=1000
    )

    assert transitions[0].decision == Decision(DecisionKind.RELEASE, spool_id=spool.id, reason="cleared_tray")

    # The subject and what a spurious release puts at risk.
    assert (f["printer"], f["spool"], f["reason"]) == (str(printer.id), str(spool.id), "cleared_tray")
    assert (f["used_g"], f["label_g"], f["tagless"]) == ("954.4", "1000.0", "yes")

    # The presence tri-state AND which rule produced it.
    assert (f["presence"], f["rule"]) == ("False", "bit_clear")

    # This push's own per-tray assertions — the rule's inputs.
    assert (f["bit"], f["state"], f["type"], f["cfg"], f["id"]) == ("0", "9", "''", "1", "0")

    # The mask, its trust verdict, and whether it is THIS push's or a cached copy.
    assert (f["mask"], f["mask_trusted"], f["mask_src"]) == ("0", "yes", "push")

    # Push shape, and the cause test that separates a glitch from a real departure.
    assert f["push"] == "full"
    assert (f["feeding"], f["printing"]) == ("no", "no")

    # A0T2 is a bare token, not a key=value pair — assert it on the line itself.
    assert " A0T2 " in _evidence_line(caplog)


@pytest.mark.asyncio
async def test_a_bitless_push_reports_the_fallback_tier_rather_than_a_borrowed_mask(
    db_session, printer_factory, env, caplog
):
    """The known H2S trap: some firmware paths carry ``tray_exist_bits`` in pushalls ONLY.

    A release on such a push had no in-push bit by construction, so emptiness came from the
    tray-level fallback tier — and the CACHED mask can only ever have acted as the
    promote-only veto that let the cleared shape through. The record must say ``cached``,
    never present a stale mask as the one that decided.
    """
    printer = await printer_factory()
    # A cached mask from an earlier pushall, and no full report for a while: an incremental.
    client = _FakeClient(tray_exist_bits="d", bits_trusted=True, last_full_report_at=time.monotonic() - 30.0)

    _, _, f = await _released_with_evidence(db_session, printer, env, caplog, client=client, exist_bits=None)

    assert (f["presence"], f["rule"], f["bit"]) == ("False", "cleared_shape", "-")
    assert (f["mask"], f["mask_trusted"], f["mask_src"]) == ("d", "yes", "cached")
    assert f["push"] == "incr"


@pytest.mark.asyncio
async def test_a_cache_that_moved_on_is_named_so_the_hex_is_not_read_as_the_deciding_mask(
    db_session, printer_factory, env, caplog
):
    """The pipeline pass is scheduled off the raw AMS hook, so a later push can overtake
    it and leave the client holding a NEWER mask than the one that decided.

    Here the cached hex says A0T2 is occupied (bit 2 set) while this push's own bit says
    it is bare — they cannot both describe one frame. ``bit=`` is the deciding evidence and
    the record must say so, because silently printing the newer hex beside
    ``rule=bit_clear`` would read as the firmware contradicting itself.
    """
    printer = await printer_factory()
    client = _FakeClient(tray_exist_bits="4", bits_trusted=True)  # bit ams*4+tray = 2

    _, _, f = await _released_with_evidence(db_session, printer, env, caplog, client=client, exist_bits=0)

    assert (f["rule"], f["bit"]) == ("bit_clear", "0")
    assert (f["mask"], f["mask_src"]) == ("4", "push_cache_moved")


@pytest.mark.asyncio
async def test_an_absent_cache_is_not_reported_as_a_cache_that_moved(db_session, printer_factory, env, caplog):
    """A missing client is not a wire anomaly.

    ``push_cache_moved`` accuses the firmware of having sent two different answers, so it
    must require a cached bit that actually DISAGREES — not merely the absence of one.
    Reporting a disconnected printer as a mask conflict would send the next triage session
    hunting a glitch that never happened.
    """
    printer = await printer_factory()

    _, _, f = await _released_with_evidence(db_session, printer, env, caplog, client=None, exist_bits=0)

    assert (f["rule"], f["bit"]) == ("bit_clear", "0")
    assert (f["mask"], f["mask_src"]) == ("-", "push")


@pytest.mark.asyncio
async def test_a_degenerate_push_still_releases_and_states_its_unknowns_as_unknown(
    db_session, printer_factory, env, caplog
):
    """No client at all — a disconnected printer whose push the pipeline still resolves.

    The release must fire exactly as it does today, and the record must land WITH it,
    saying "I do not know" for the mask and the push shape instead of guessing or
    crashing. Nothing about a missing wire surface is an emergency; a silent release is.
    """
    printer = await printer_factory()

    transitions, spool, f = await _released_with_evidence(db_session, printer, env, caplog, client=None)

    assert transitions[0].decision == Decision(DecisionKind.RELEASE, spool_id=spool.id, reason="cleared_tray")
    assert transitions[0].applied is True
    assert await _assignment(db_session, printer.id, 0, 2) is None

    assert (f["mask"], f["mask_src"], f["push"], f["push_age"]) == ("-", "none", "?", "-")
    # …while everything the OBSERVATION knows is still fully reported.
    assert (f["presence"], f["rule"], f["state"], f["spool"]) == ("False", "cleared_shape", "9", str(spool.id))


@pytest.mark.asyncio
async def test_an_unreadable_client_costs_three_fields_not_the_line(db_session, printer_factory, env, caplog):
    """A client that raises on every read still leaves a usable record.

    A diagnostic that can raise is worse than no diagnostic at all (invariant 10) — but a
    diagnostic that gives UP is nearly as bad, because a client blowing up mid-release is
    precisely when the line is worth having. The mask fields degrade to ``?``; the
    observation half, which is the half that localizes a glitch, is untouched.
    """
    printer = await printer_factory()

    class _Exploding:
        @property
        def state(self):
            raise RuntimeError("wire surface unreadable")

    transitions, spool, f = await _released_with_evidence(db_session, printer, env, caplog, client=_Exploding())

    assert transitions[0].applied is True
    assert await _assignment(db_session, printer.id, 0, 2) is None
    assert (f["mask"], f["mask_trusted"], f["mask_src"], f["push"]) == ("?", "?", "?", "?")
    assert (f["presence"], f["rule"], f["spool"]) == ("False", "cleared_shape", str(spool.id))


@pytest.mark.asyncio
async def test_collecting_the_evidence_changes_nothing_about_the_release(
    db_session, printer_factory, env, caplog, monkeypatch
):
    """THE SCOPE BOUNDARY, asserted rather than promised.

    WS7 is diagnosis, not a fix: a release that happens today must still happen, at the
    same moment, with the same verdict and the same resulting DB state. The proof is to run
    the identical release twice — once with the record built, once with the builder
    stubbed out entirely — and compare every observable: the decision, whether it applied,
    the binding, the last-location residue the de-bounce lane later reads, the grams, and
    the broadcasts.
    """

    async def _run(with_evidence: bool):
        printer = await printer_factory()
        if not with_evidence:
            monkeypatch.setattr(slot_pipeline, "_release_evidence", lambda *a, **k: None)
        recorder = _Recorder()
        spool = await _spool(db_session, material="PETG", rgba="000000FF", weight_used=954.4)
        await _bind_row(db_session, spool, printer.id, 0, 2)

        obs = observe_tray(printer.id, 0, dict(_CLEARED_TRAY), exist_bits=0)
        transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, recorder))
        await db_session.refresh(spool)
        return (
            transitions[0].decision.kind,
            transitions[0].decision.reason,
            transitions[0].applied,
            transitions[0].from_state,
            transitions[0].to_state,
            await _assignment(db_session, printer.id, 0, 2) is None,
            (spool.last_location_printer_id, spool.last_location_ams_id, spool.last_location_tray_id),
            spool.weight_used,
            spool.archived_at,
            recorder.types(),
        )

    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        live = await _run(with_evidence=True)
    assert any(m.startswith(_EVIDENCE) for m in _records(caplog, logging.INFO))

    caplog.clear()
    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        stubbed = await _run(with_evidence=False)
    assert not any(m.startswith(_EVIDENCE) for m in _records(caplog, logging.INFO))

    # Same printer id in each tuple position bar the ones that carry it — compare the rest.
    assert live[:6] == stubbed[:6]
    assert live[6][1:] == stubbed[6][1:]
    assert live[7:] == stubbed[7:]


@pytest.mark.asyncio
async def test_a_failing_record_never_costs_the_release(db_session, printer_factory, env, caplog, monkeypatch):
    """Invariant 10, at the seam. The builder's own guard is what is under test here, so
    the failure is injected INSIDE it (``_mask_facts``) rather than by replacing it."""
    printer = await printer_factory()

    def _boom(*args, **kwargs):
        raise RuntimeError("evidence builder exploded")

    monkeypatch.setattr(slot_pipeline, "_mask_facts", _boom)

    spool = await _spool(db_session, material="PETG", rgba="000000FF", weight_used=954.4)
    await _bind_row(db_session, spool, printer.id, 0, 2)

    obs = observe_tray(printer.id, 0, dict(_CLEARED_TRAY), exist_bits=0)
    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.RELEASE, spool_id=spool.id, reason="cleared_tray")
    assert transitions[0].applied is True
    assert await _assignment(db_session, printer.id, 0, 2) is None
    await db_session.refresh(spool)
    assert spool.last_location_tray_id == 2
    assert not any(m.startswith(_EVIDENCE) for m in _records(caplog, logging.INFO))


@pytest.mark.asyncio
async def test_a_debounce_and_a_real_departure_are_told_apart_by_the_record(db_session, printer_factory, env, caplog):
    """The whole point of collecting this: which releases were glitches?

    Two slots on one printer lose presence in the same pass, on the same wire shape. The
    only thing separating them is CAUSE — T2 was idle, T3 was the active feeder of a live
    print when it went absent, which is a runout or a mid-print pull and never a glitch
    (operator ruling 15). The record must carry that difference at the release edge,
    because the ~3-minute bay-clear→HMS gap means no incident exists yet to ask.

    Then the T2 roll comes straight back and DE-BOUNCES, and its own line names the same
    spool — closing the correlation a triager six hours later has to make by hand.
    """
    printer = await printer_factory()
    glitched = await _spool(db_session, material="PETG", rgba="000000FF", weight_used=300)
    drained = await _spool(db_session, material="PETG", rgba="000000FF", weight_used=940)
    await _bind_row(db_session, glitched, printer.id, 0, 2)
    await _bind_row(db_session, drained, printer.id, 0, 3)

    # The loss-edge stamps the presence lane writes: T3 was feeding, T2 was not.
    _seed_reseat(printer.id, 0, 2, absent_for=None, under_active_feed=False)
    _seed_reseat(printer.id, 0, 3, absent_for=None, under_active_feed=True)

    cleared = [
        observe_tray(printer.id, 0, {"id": 2, "state": 9, "tray_type": ""}, exist_bits=0),
        observe_tray(printer.id, 0, {"id": 3, "state": 9, "tray_type": ""}, exist_bits=0),
    ]
    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        await run_slot_pipeline(printer.id, cleared, _deps(db_session, env))

    lines = {_fields(m)["spool"]: _fields(m) for m in _records(caplog, logging.INFO) if m.startswith(_EVIDENCE)}
    assert set(lines) == {str(glitched.id), str(drained.id)}, "one record per release, both named"
    assert lines[str(glitched.id)]["feeding"] == "no", "a glitch candidate: nothing was feeding this slot"
    assert lines[str(drained.id)]["feeding"] == "yes", "a real departure: this slot was the live feeder"
    # Same wire shape on both — CAUSE is the discriminator, not the evidence.
    assert lines[str(glitched.id)]["rule"] == lines[str(drained.id)]["rule"] == "bit_clear"

    # T2's roll returns inside the de-bounce window: row T1, the lane's one real job.
    caplog.clear()
    _seed_reseat(printer.id, 0, 2, absent_for=3.0)
    reseated = observe_tray(printer.id, 0, {"id": 2, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})
    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        transitions = await run_slot_pipeline(printer.id, [reseated], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.RECLAIM, spool_id=glitched.id, reason="reseat_debounce")
    debounced = [m for m in _records(caplog, logging.INFO) if "de-bounced spool" in m]
    assert debounced and f"de-bounced spool {glitched.id}" in debounced[0]
    assert "measured absence 3s" in debounced[0], "the pair the release line correlates against"


# --- displaced-row disposal -------------------------------------------------


async def _displace(db_session, printer_factory, env, incumbent_kwargs: dict):
    """Seed ``incumbent`` on a slot, then let a positively-identified newcomer take it.

    Returns ``(printer, incumbent, arriving)``. The BIND is row 2.3's
    ``identity_resolved_candidate``: the wire uuid names a different row, so the writer's
    move semantics unbind the incumbent fleet-wide — which is exactly the moment the
    displaced row's fate has to be decided.
    """
    printer = await printer_factory()
    incumbent = await _spool(db_session, material="PETG", rgba="000000FF", **incumbent_kwargs)
    arriving = await _spool(db_session, tag_uid=TAG_B, tray_uuid=UUID_2, material="PETG", rgba="000000FF")
    await _bind_row(db_session, incumbent, printer.id, 0, 1)

    obs = _obs(
        printer.id,
        {"id": 1, "state": 11, "tag_uid": TAG_B, "tray_uuid": UUID_2, "tray_type": "PETG", "tray_color": "000000FF"},
    )
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))
    assert transitions[0].decision == Decision(
        DecisionKind.BIND, spool_id=arriving.id, reason="identity_resolved_candidate"
    )
    assert (await _assignment(db_session, printer.id, 0, 1)).spool_id == arriving.id
    return printer, incumbent, arriving


@pytest.mark.asyncio
async def test_a_displaced_pristine_ghost_is_hard_deleted(db_session, printer_factory, env):
    """The 239-242 shape: an ``ams_auto`` row minted into an unresolved slot, then
    displaced by the real identity the firmware finally read. It never fed and never
    carried an identity, so it is deleted rather than left as an unbound 0 g row."""
    _printer, ghost, arriving = await _displace(db_session, printer_factory, env, {"data_origin": "ams_auto"})
    ghost_id = ghost.id

    assert await db_session.get(Spool, ghost_id) is None
    assert await _spool_count(db_session) == 1
    assert (await db_session.get(Spool, arriving.id)).archived_at is None


@pytest.mark.asyncio
async def test_a_displaced_ghost_with_a_usage_ledger_is_archived_not_deleted(db_session, printer_factory, env):
    """Grams-state is the ghost GATE; the canonical disposal still decides delete-vs-archive
    on the usage LEDGER. A repair full-weight reset makes 0 g used ≠ never used (doctrine
    rule 8), so a row whose history survives its grams is archived — the two checks are
    belt and braces, not duplicates."""
    printer = await printer_factory()
    ghost = await _spool(db_session, material="PETG", rgba="000000FF", data_origin="ams_auto")
    db_session.add(SpoolUsageHistory(spool_id=ghost.id, weight_used=180, percent_used=18))
    await db_session.commit()
    arriving = await _spool(db_session, tag_uid=TAG_B, tray_uuid=UUID_2, material="PETG", rgba="000000FF")
    await _bind_row(db_session, ghost, printer.id, 0, 1)

    obs = _obs(
        printer.id,
        {"id": 1, "state": 11, "tag_uid": TAG_B, "tray_uuid": UUID_2, "tray_type": "PETG", "tray_color": "000000FF"},
    )
    await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    await db_session.refresh(ghost)
    assert ghost.archived_at is not None
    assert (await _assignment(db_session, printer.id, 0, 1)).spool_id == arriving.id


@pytest.mark.asyncio
async def test_a_displaced_spent_core_is_archived(db_session, printer_factory, env, caplog):
    """Printer 4 tray 2's endgame: the newcomer's identity is positive proof the drained
    core physically left, which is the same evidence REPLACE_SPENT acts on. An active
    spent row goes on presenting a 0 g ledger to the selection and deficit lanes, so it
    retires — with its grams (1121.5 g) intact."""
    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        printer, spent, _arriving = await _displace(
            db_session,
            printer_factory,
            env,
            {
                "data_origin": "rfid_auto",
                "tag_uid": TAG_A,
                "tray_uuid": UUID_1,
                "spent_at": datetime.utcnow(),
                "weight_used": 1121.5,
            },
        )

    await db_session.refresh(spent)
    assert spent.archived_at is not None
    assert spent.weight_used == 1121.5  # retired, not erased
    assert any(f"spent spool {spent.id} archived — displaced" in m for m in _records(caplog, logging.INFO))
    assert "A0T1" in next(m for m in _records(caplog, logging.INFO) if "archived — displaced" in m)


@pytest.mark.asyncio
async def test_a_displaced_live_row_is_left_exactly_as_the_writer_left_it(db_session, printer_factory, env):
    """Neither lane applies to a live, fed, operator-visible row: it becomes unbound
    inventory with its grams and its reclaim stamp — the existing move semantics, untouched."""
    _printer, live, _arriving = await _displace(
        db_session,
        printer_factory,
        env,
        {"data_origin": "rfid_auto", "tag_uid": TAG_A, "tray_uuid": UUID_1, "weight_used": 400},
    )

    await db_session.refresh(live)
    assert live.archived_at is None
    assert live.weight_used == 400
    assert live.last_location_tray_id == 1
    assert await _spool_count(db_session) == 2


@pytest.mark.asyncio
async def test_orphaned_assignment_is_released(db_session, printer_factory, env, caplog):
    """An assignment that outlived its spool row is a bogus location claim (SQLite FKs
    are unenforced) — it goes through the ONE unbind writer, not a silent delete."""
    printer = await printer_factory()
    spool = await _spool(db_session)
    await _bind_row(db_session, spool, printer.id, 0, 0)
    # Core delete: bypasses the ORM cascade so the assignment survives its spool.
    await db_session.execute(delete(Spool).where(Spool.id == spool.id))
    await db_session.commit()

    obs = _obs(printer.id, {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})
    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.reason == slot_pipeline.ORPHAN_RELEASE_REASON
    assert transitions[0].applied is True
    assert await _assignment(db_session, printer.id, 0, 0) is None
    assert any("orphaned_assignment" in m for m in _records(caplog, logging.INFO))


# --- REPLACE_SPENT ----------------------------------------------------------


def _tagless_default_json() -> str:
    return json.dumps(
        {
            "brand": "Acme",
            "material": "PLA",
            "subtype": "Matte",
            "rgba": "FF0000FF",
            "slicer_filament": "GFL96",
            "nozzle_temp_min": 190,
            "nozzle_temp_max": 230,
        }
    )


async def _spent_slot(db_session, printer_id, *, data_origin="ams_auto"):
    """A spent tagless row bound to a slot, with a qualified cycle pending."""
    departed = await _spool(
        db_session, material="PETG", rgba="000000FF", spent_at=datetime.utcnow(), data_origin=data_origin
    )
    await _bind_row(db_session, departed, printer_id, 0, 0, fingerprint_color="000000FF", fingerprint_type="PETG")
    spool_tagless._pending_physical_cycles.add((printer_id, 0, 0))
    return departed


@pytest.mark.asyncio
async def test_replace_spent_skips_when_the_tray_is_not_loaded(db_session, printer_factory, env):
    """A dead roll re-seated without filament fed is not a swap: no churn, and the
    cycle is NOT spent — the real swap still gets to use it."""
    printer = await printer_factory()
    departed = await _spent_slot(db_session, printer.id)

    obs = _obs(printer.id, {"id": 0, "state": 10, "tray_type": "PETG", "tray_color": "000000FF"})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.kind is DecisionKind.REPLACE_SPENT
    assert transitions[0].applied is False
    assert spool_tagless.qualified_cycle_pending(printer.id, 0, 0) is True
    assert (await _assignment(db_session, printer.id, 0, 0)).spool_id == departed.id
    assert await _spool_count(db_session) == 1


@pytest.mark.asyncio
async def test_replace_spent_mints_the_default_and_consumes_the_cycle_once(db_session, printer_factory, env):
    """The W1 silent spent→mint: the drained row retires, a fresh row from the tagless
    DEFAULT takes the slot (the tray still carries the departed config — firmware
    leftover), the config is pushed, and the cycle is spent exactly once."""
    printer = await printer_factory()
    env.settings["tagless_default_filament"] = _tagless_default_json()
    await _spent_slot(db_session, printer.id)

    tray = {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"}
    deps = _deps(db_session, env)
    first = await run_slot_pipeline(printer.id, [_obs(printer.id, tray)], deps)

    assert first[0].decision.reason == "spent_swap_confirmed"
    assert first[0].applied is True
    assert spool_tagless.qualified_cycle_pending(printer.id, 0, 0) is False
    row = await _assignment(db_session, printer.id, 0, 0)
    minted = await db_session.get(Spool, row.spool_id)
    assert minted.spent_at is None  # the slot is live again, not latched
    # Field mapping from the DEFAULT dict (mirrors mint_tagless_spool's default arm).
    assert (minted.brand, minted.material, minted.subtype, minted.rgba) == ("Acme", "PLA", "Matte", "FF0000FF")
    assert (minted.slicer_filament, minted.nozzle_temp_min, minted.nozzle_temp_max) == ("GFL96", 190, 230)
    assert minted.data_origin == "ams_auto" and minted.weight_used == 0
    # A default-minted row seeds its fingerprint from the SETTING, not the bare wire.
    assert (row.fingerprint_color, row.fingerprint_type) == ("FF0000FF", "PLA")
    assert env.pushes == [(minted.id, printer.id, 0, 0)]

    # Second push, with the tray now reporting what the config push wrote: the cycle is
    # gone, so nothing further happens — one swap, one replacement.
    applied_tray = {"id": 0, "state": 11, "tray_type": "PLA", "tray_color": "FF0000FF"}
    second = await run_slot_pipeline(printer.id, [_obs(printer.id, applied_tray)], deps)
    assert second[0].decision == Decision(DecisionKind.KEEP, spool_id=minted.id, reason="fingerprint_matches")
    assert await _spool_count(db_session) == 1  # pristine departed row was hard-deleted


@pytest.mark.asyncio
async def test_replace_spent_hard_deletes_a_pristine_departed_row(db_session, printer_factory, env):
    """No usage ledger → nothing to preserve: the provisional row is hard-deleted
    (``dispose_provisional_on_tag``) rather than left as an archived 0 g husk."""
    printer = await printer_factory()
    await _spent_slot(db_session, printer.id)

    obs = _obs(printer.id, {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})
    await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    # Identity by STATE, not by id: SQLite hands the freed rowid to the replacement.
    # Nothing spent and nothing archived survives — the husk is gone, not hidden.
    assert await _spool_count(db_session) == 1
    assert await db_session.scalar(select(func.count(Spool.id)).where(Spool.spent_at.is_not(None))) == 0
    assert await db_session.scalar(select(func.count(Spool.id)).where(Spool.archived_at.is_not(None))) == 0


@pytest.mark.asyncio
async def test_replace_spent_archives_a_ledger_bearing_departed_row(db_session, printer_factory, env):
    """A row that consumed filament keeps its grams: archived, never deleted."""
    printer = await printer_factory()
    departed = await _spent_slot(db_session, printer.id)
    db_session.add(SpoolUsageHistory(spool_id=departed.id, weight_used=820, percent_used=82))
    await db_session.commit()

    obs = _obs(printer.id, {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})
    await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    await db_session.refresh(departed)
    assert departed.archived_at is not None
    row = await _assignment(db_session, printer.id, 0, 0)
    assert row.spool_id != departed.id


@pytest.mark.asyncio
async def test_operator_created_departed_row_is_archived_not_kept(db_session, printer_factory, env):
    """A row the disposal helper declines ("kept") must still stop claiming the slot —
    the roll ran out and physically left."""
    printer = await printer_factory()
    departed = await _spent_slot(db_session, printer.id, data_origin="manual")

    obs = _obs(printer.id, {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})
    await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    await db_session.refresh(departed)
    assert departed.archived_at is not None
    assert (await _assignment(db_session, printer.id, 0, 0)).spool_id != departed.id


@pytest.mark.asyncio
async def test_spent_latch_holds_without_a_qualified_cycle(db_session, printer_factory, env):
    """No cycle: the runout-instant flap must not phantom-mint over a still-present
    dead roll."""
    printer = await printer_factory()
    departed = await _spent_slot(db_session, printer.id)
    spool_tagless._pending_physical_cycles.clear()

    obs = _obs(printer.id, {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.KEEP, spool_id=departed.id, reason="spent_latch")
    assert await _spool_count(db_session) == 1


# --- REPLACE_SPENT on an answered no-tag read (2026-08-07, spool 226) --------


def _arm_no_tag_answer(printer_id, ams_id=0, tray_id=0, *, age=60.0):
    """Stamp the slot as "we commanded a discovery read ``age`` s ago and no tag ever came".

    The ledger shape ``ams_presence.read_answered_no_tag`` adjudicates: identity-learned
    stamped immediately BEFORE the discovery stamp (``command_identify``'s own order, which is
    what makes "a tag landed later" decidable), nothing in flight. Written directly, as the
    presence suite's own ``_arm_cycle`` does — replaying a full read through the MQTT client
    would pin the commander, which its own suite already owns."""
    now = ams_presence.time.monotonic()
    ams_presence._slot_read_at[(printer_id, ams_id, tray_id)] = now - age - 0.5
    ams_presence._discovery_read_at[(printer_id, ams_id, tray_id)] = now - age


async def _spent_tagged_slot(db_session, printer_id, ams_id=0, tray_id=0, **kw):
    """A SPENT RFID-tagged row bound to a slot — the spool 226 shape."""
    departed = await _spool(
        db_session,
        material="PETG",
        rgba="000000FF",
        data_origin="rfid_auto",
        tag_uid=TAG_A,
        tray_uuid=UUID_1,
        spent_at=datetime.utcnow(),
        **kw,
    )
    await _bind_row(db_session, departed, printer_id, ams_id, tray_id)
    return departed


@pytest.mark.asyncio
async def test_an_answered_no_tag_read_swaps_the_spent_tagged_row(db_session, printer_factory, env, monkeypatch):
    """The end-to-end fix. A spent TAGGED binding under a seated BARE tray whose commanded
    discovery read answered NO TAG: the departed row is ARCHIVED (never deleted — it carries a
    tag and a ledger), a fresh tagless row from the DEFAULT takes the slot through the one
    binding writer, its identity is pushed to the firmware, and NO qualified cycle is consumed
    — this arm's evidence is the answered read, and demanding a cycle it cannot have is what
    would veto every swap it exists to perform."""
    printer = await printer_factory()
    env.settings["tagless_default_filament"] = _tagless_default_json()
    departed = await _spent_tagged_slot(db_session, printer.id, 0, 1)
    _arm_no_tag_answer(printer.id, 0, 1)
    consumed: list[tuple[int, int, int]] = []
    monkeypatch.setattr(
        slot_pipeline.spool_tagless,
        "consume_qualified_cycle",
        lambda p, a, t: (consumed.append((p, a, t)), True)[1],
    )

    obs = _obs(printer.id, {"id": 1, "state": 10})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.kind is DecisionKind.REPLACE_SPENT
    assert transitions[0].decision.reason == "spent_swap_no_tag_read"
    assert transitions[0].applied is True
    assert consumed == []  # no cycle to consume, and none demanded

    await db_session.refresh(departed)
    assert departed.archived_at is not None  # archived, not deleted
    row = await _assignment(db_session, printer.id, 0, 1)
    assert row.spool_id != departed.id
    minted = await db_session.get(Spool, row.spool_id)
    assert (minted.tag_uid, minted.tray_uuid, minted.spent_at) == (None, None, None)
    assert (minted.brand, minted.material, minted.rgba, minted.data_origin) == ("Acme", "PLA", "FF0000FF", "ams_auto")
    assert (row.fingerprint_color, row.fingerprint_type) == ("FF0000FF", "PLA")
    assert env.pushes == [(minted.id, printer.id, 0, 1)]  # the bare tray gets the row's identity


@pytest.mark.asyncio
async def test_the_swap_does_not_repeat_on_the_next_push(db_session, printer_factory, env):
    """The stamps are NON-consuming (``_discovery_read_at`` also drives the 0700_0081 HMS
    suppression), so idempotency has to come from the outcome: once the swap lands the binding
    is no longer spent, the table stops emitting the reason, and the slot resolves as an
    ordinary tagless row."""
    printer = await printer_factory()
    env.settings["tagless_default_filament"] = _tagless_default_json()
    await _spent_tagged_slot(db_session, printer.id, 0, 1)
    _arm_no_tag_answer(printer.id, 0, 1)
    deps = _deps(db_session, env)

    obs = _obs(printer.id, {"id": 1, "state": 10})
    first = await run_slot_pipeline(printer.id, [obs], deps)
    second = await run_slot_pipeline(printer.id, [obs], deps)

    assert first[0].decision.reason == "spent_swap_no_tag_read"
    assert second[0].applied is False
    assert second[0].decision.reason != "spent_swap_no_tag_read"
    assert await _spool_count(db_session) == 2  # the archived departed row + its replacement


@pytest.mark.asyncio
async def test_a_spent_TAGLESS_row_is_never_swapped_on_a_no_tag_read(db_session, printer_factory, env):
    """Scoping pin: over a spent TAGLESS binding a no-tag read proves nothing (the same core
    reads the same way before and after a swap), so the constellation keeps owing a read and
    the qualified-cycle machinery keeps owning the case."""
    printer = await printer_factory()
    env.settings["tagless_default_filament"] = _tagless_default_json()
    departed = await _spool(db_session, material="PETG", data_origin="ams_auto", spent_at=datetime.utcnow())
    await _bind_row(db_session, departed, printer.id, 0, 1)
    _arm_no_tag_answer(printer.id, 0, 1)

    obs = _obs(printer.id, {"id": 1, "state": 10})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.NONE, reason="spent_occupied_owed_identify")
    assert (await _assignment(db_session, printer.id, 0, 1)).spool_id == departed.id
    assert await _spool_count(db_session) == 1


@pytest.mark.asyncio
async def test_a_fed_tray_still_takes_the_swap_its_answered_read_earned(db_session, printer_factory, env):
    """The apply pre-gate re-asserts the PRESENCE the decision was made on, not a second
    state test that could disagree with it.

    A no-tag answer read off a feeding tray would prove nothing (the tag faces away once
    the filament is threaded on to the hub) — but such an answer cannot be obtained: a read
    is only ever commanded when no filament is engaged, so every no-tag stamp reaching this
    arm was taken on an unengaged tray. Demanding state 10 HERE only re-created the
    emit-versus-grant mismatch one layer down: the table decides on ``present is True``, so
    a slot read bare and since fed produced a decision the gate silently discarded, every
    push, forever."""
    printer = await printer_factory()
    env.settings["tagless_default_filament"] = _tagless_default_json()
    departed = await _spent_tagged_slot(db_session, printer.id, 0, 1)
    _arm_no_tag_answer(printer.id, 0, 1)

    obs = _obs(printer.id, {"id": 1, "state": 11})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.reason == "spent_swap_no_tag_read"
    assert (await _assignment(db_session, printer.id, 0, 1)).spool_id != departed.id
    assert await _spool_count(db_session) == 2


@pytest.mark.asyncio
async def test_a_fresh_read_is_not_yet_an_answer(db_session, printer_factory, env):
    """The settle floor, end to end: a read commanded seconds ago may still be running, so the
    slot only owes its read — the swap waits for the answer to be an answer."""
    printer = await printer_factory()
    env.settings["tagless_default_filament"] = _tagless_default_json()
    departed = await _spent_tagged_slot(db_session, printer.id, 0, 1)
    _arm_no_tag_answer(printer.id, 0, 1, age=1.0)

    obs = _obs(printer.id, {"id": 1, "state": 10})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.NONE, reason="spent_occupied_owed_identify")
    assert (await _assignment(db_session, printer.id, 0, 1)).spool_id == departed.id


# --- the same proof over a LIVE tagged binding (doctrine rule 11, 2026-08-19) -----------

# The G7 tray, and the reason the evidence could never fire for it before: a third-party
# PETG roll reports a filament type and a preset id and NO tag. CONFIGURED, not bare.
CONFIGURED_NO_TAG = {"id": 1, "state": 10, "tray_type": "PETG", "tray_color": "000000FF", "tray_info_idx": "GFG02"}
BARE = {"id": 1, "state": 10}


async def _tagged_live_slot(db_session, printer_id, ams_id=0, tray_id=0, **kw):
    """A LIVE (non-spent) RFID-tagged row bound to a slot — the G7 roll that gets swapped."""
    departed = await _spool(
        db_session,
        material="PETG",
        rgba="000000FF",
        data_origin="rfid_auto",
        tag_uid=TAG_A,
        tray_uuid=UUID_1,
        weight_used=400,
        **kw,
    )
    await _bind_row(db_session, departed, printer_id, ams_id, tray_id)
    return departed


@pytest.mark.asyncio
async def test_G7_a_third_party_roll_over_a_live_tagged_binding_takes_the_slot(db_session, printer_factory, env):
    """Scenario G7, end to end — the commonest physical swap on this fleet, which used to
    persist silently with the wrong row bound.

    Two gates blocked it, and the tray shape here is the second one: ``tray_type: "PETG"``,
    ``tray_info_idx: "GFG02"``, ``tag_uid: null``. Configured, not bare — so a bare-tray-only
    predicate could never adjudicate the answer, however many times the read was taken."""
    printer = await printer_factory()
    env.settings["tagless_default_filament"] = _tagless_default_json()
    departed = await _tagged_live_slot(db_session, printer.id, 0, 1)
    _arm_no_tag_answer(printer.id, 0, 1)

    obs = _obs(printer.id, dict(CONFIGURED_NO_TAG))
    assert obs.config_nonempty is True and obs.identity_asserted is False
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.reason == "tagged_swap_no_tag_read"
    assert (await _assignment(db_session, printer.id, 0, 1)).spool_id != departed.id
    assert await _spool_count(db_session) == 2


@pytest.mark.asyncio
async def test_G7_the_departed_tagged_row_is_unlinked_and_keeps_its_grams(db_session, printer_factory, env):
    """A roll that LEFT is not a roll that ran dry. Unlike row 5a's ``REPLACE_SPENT`` the
    departed row is neither archived nor stamped spent — it is a real, part-used roll that
    is simply somewhere else now, and its ledger has to survive intact for whichever slot it
    turns up in next."""
    printer = await printer_factory()
    env.settings["tagless_default_filament"] = _tagless_default_json()
    departed = await _tagged_live_slot(db_session, printer.id, 0, 1)
    _arm_no_tag_answer(printer.id, 0, 1)

    await run_slot_pipeline(printer.id, [_obs(printer.id, dict(CONFIGURED_NO_TAG))], _deps(db_session, env))

    await db_session.refresh(departed)
    assert departed.archived_at is None
    assert departed.spent_at is None
    assert departed.weight_used == 400


@pytest.mark.asyncio
async def test_G10_a_push_that_merely_omits_the_rfid_fields_keeps_the_binding(db_session, printer_factory, env):
    """Silence is not an answer. Identical constellation, no read ever commanded: periodic
    AMS pushes routinely omit identity, so the tagged row keeps its slot and nothing is
    minted. This is row 4b′'s original contract and the new arm must not swallow it."""
    printer = await printer_factory()
    env.settings["tagless_default_filament"] = _tagless_default_json()
    departed = await _tagged_live_slot(db_session, printer.id, 0, 1)

    obs = _obs(printer.id, dict(CONFIGURED_NO_TAG))
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(
        DecisionKind.KEEP, spool_id=departed.id, reason="tagged_row_awaits_tag_lane"
    )
    assert (await _assignment(db_session, printer.id, 0, 1)).spool_id == departed.id
    assert await _spool_count(db_session) == 1


@pytest.mark.asyncio
async def test_G9_a_live_TAGLESS_binding_is_never_swapped_on_a_no_tag_read(db_session, printer_factory, env):
    """**The arm that must NOT fire**, end to end. Rule 11 is one-way BY LOGIC: a binding
    that claims no identity has nothing for a no-tag read to contradict, and the same bare
    core reads identically before and after a swap. A future session "generalising" this the
    rest of the way would mint a fresh row over every tagless roll the farm ever reads."""
    printer = await printer_factory()
    env.settings["tagless_default_filament"] = _tagless_default_json()
    bound = await _spool(db_session, material="PETG", rgba="000000FF", data_origin="ams_auto")
    await _bind_row(db_session, bound, printer.id, 0, 1)
    _arm_no_tag_answer(printer.id, 0, 1)

    obs = _obs(printer.id, dict(CONFIGURED_NO_TAG))
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.reason != "tagged_swap_no_tag_read"
    assert (await _assignment(db_session, printer.id, 0, 1)).spool_id == bound.id
    assert await _spool_count(db_session) == 1


@pytest.mark.asyncio
async def test_the_read_this_arm_concludes_on_is_one_the_REAL_need_authority_commands(
    db_session, printer_factory, env, monkeypatch
):
    """THE liveness pair for the generalisation (memory ``liveness-paired-verification``).

    A predicate that never fires is indistinguishable from one that works, so this drives the
    whole journey through the REAL ``ams_presence.identify_needed`` — only the commander is
    faked — instead of seeding the answer and asserting the table.

    The production sequence for a swap the farm did not see as a release: the tray comes back
    BARE while the firmware has not yet re-asserted a configuration, which resolves
    ``identity_unresolved`` — a reason the identify default carries to the need authority,
    where the unanswered PHYSICAL CYCLE grants a ``discovery`` read (the one classification
    that stamps the evidence and suppresses its expected no-tag failure). The answer comes
    back finding no chip, and THAT is what the new arm concludes on.

    Both halves are asserted: the read is really commanded, and its answer really concludes."""
    printer = await printer_factory()
    env.settings["tagless_default_filament"] = _tagless_default_json()
    departed = await _tagged_live_slot(db_session, printer.id, 0, 1)
    # The operator pulled the Bambu roll and seated a third-party one: a qualified physical
    # cycle the presence lane recorded and no read has answered yet. Aged past
    # ``spool_tagless._CONFIG_SETTLE_MAX_S`` so the wire-protection settle window has
    # concluded — which is not a test convenience but the very shape this wave is about: a
    # slot whose physical change has gone unanswered LONGER than the settle cap is a parked
    # slot, and parked is where G7 lived.
    ams_presence._physical_cycle_at[(printer.id, 0, 1)] = (
        ams_presence.time.monotonic() - spool_tagless._CONFIG_SETTLE_MAX_S - 60
    )

    commanded: list[tuple[int, int, int, str | None]] = []

    async def _record(printer_id, ams_id, tray_id, *, source, reason=None, **kwargs):
        commanded.append((printer_id, ams_id, tray_id, reason))
        return True, "ok"

    monkeypatch.setattr(slot_pipeline.ams_presence, "command_identify", _record)
    deps = _prod_identify_deps(db_session, env)  # real identify_needed, no injected hook
    obs = _obs(printer.id, dict(BARE))

    first = await run_slot_pipeline(printer.id, [obs], deps)

    assert first[0].decision.reason == "identity_unresolved"
    assert commanded == [(printer.id, 0, 1, "discovery")]

    # …and what the firmware answers. Aged past the settle floor, exactly as the rest of this
    # section does — the real clock belongs to the presence suite.
    _arm_no_tag_answer(printer.id, 0, 1)
    second = await run_slot_pipeline(printer.id, [obs], deps)

    assert second[0].decision.reason == "tagged_swap_no_tag_read"
    assert (await _assignment(db_session, printer.id, 0, 1)).spool_id != departed.id


@pytest.mark.asyncio
async def test_a_spent_tagged_binding_under_a_CONFIGURED_tray_concludes_too(db_session, printer_factory, env):
    """Row 4a′ — row 5a's missing twin, end to end.

    The same slot, the same answer, and before this wave it parked on ``spent_latch`` for no
    better reason than that the firmware had left the departed roll's ``tray_type`` behind,
    which is the ordinary state of a tray after a swap. Proof does not become less positive
    because the tray happens to carry configuration."""
    printer = await printer_factory()
    env.settings["tagless_default_filament"] = _tagless_default_json()
    departed = await _spent_tagged_slot(db_session, printer.id, 0, 1)
    spool_tagless._pending_physical_cycles.clear()  # no cycle: the READ is the whole evidence
    _arm_no_tag_answer(printer.id, 0, 1)

    obs = _obs(printer.id, dict(CONFIGURED_NO_TAG))
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision.reason == "spent_swap_no_tag_read"
    await db_session.refresh(departed)
    assert departed.archived_at is not None  # spent → archived, unlike the live lane above
    assert await _spool_count(db_session) == 2


# --- DEFER / NONE -----------------------------------------------------------


@pytest.mark.asyncio
async def test_drying_defers_everything_and_schedules_nothing(db_session, printer_factory, env):
    """Wire safety outranks a perfect tag match: an AMS write during drying disengages
    the tray and fails the cycle (HMS 0700_C069)."""
    printer = await printer_factory()
    obs = _obs(
        printer.id,
        {"id": 0, "state": 11, "tag_uid": TAG_A, "tray_uuid": UUID_1, "tray_type": "PETG", "tray_color": "000000FF"},
    )
    deps = _deps(db_session, env, client=_FakeClient(drying=True))
    transitions = await run_slot_pipeline(printer.id, [obs], deps)

    assert transitions[0].decision == Decision(DecisionKind.DEFER, reason="ams_drying")
    assert transitions[0].applied is False
    assert env.identifies == [] and await _spool_count(db_session) == 0


@pytest.mark.asyncio
async def test_identify_in_flight_defers(db_session, printer_factory, env):
    printer = await printer_factory()
    obs = _obs(printer.id, {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})
    deps = _deps(db_session, env, client=_FakeClient(refusal="identify_in_flight"))
    transitions = await run_slot_pipeline(printer.id, [obs], deps)

    assert transitions[0].decision == Decision(DecisionKind.DEFER, reason="identify_in_flight")
    assert await _spool_count(db_session) == 0


@pytest.mark.asyncio
async def test_unresolved_identity_owes_an_identify_when_idle(db_session, printer_factory, env):
    """Presence with no identity and no config: NO binding mutation until identity is
    resolved (that is how grams land on stale rows) — buy the answer instead."""
    printer = await printer_factory()
    obs = _obs(printer.id, {"id": 0, "state": 10})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.NONE, reason="identity_unresolved")
    assert env.identifies == [(printer.id, 0, 0, "identity_unresolved")]
    assert await _spool_count(db_session) == 0


@pytest.mark.asyncio
async def test_mid_print_unresolved_defers_without_an_identify(db_session, printer_factory, env):
    """Doctrine rule 5: mid-print inserts are never auto-read."""
    printer = await printer_factory()
    obs = _obs(printer.id, {"id": 0, "state": 10})
    deps = _deps(db_session, env, client=_FakeClient(gcode_state="RUNNING"))
    transitions = await run_slot_pipeline(printer.id, [obs], deps)

    assert transitions[0].decision == Decision(DecisionKind.DEFER, reason="mid_print_unresolved")
    assert env.identifies == []


@pytest.mark.asyncio
async def test_spent_occupied_slot_owes_a_discovery_identify(db_session, printer_factory, env):
    """Printer 4 tray 2 (2026-08-07): a spent ``rfid_auto`` latch over a seated roll the
    wire never named owed NOTHING, so no identify was scheduled, so the newcomer's tag was
    never read — a full day of nothing. The verdict now routes to the identify lane, and
    still mutates no binding: naming the roll is what unlocks the slot, not guessing."""
    printer = await printer_factory()
    spent = await _spool(
        db_session,
        material="PETG",
        rgba="000000FF",
        data_origin="rfid_auto",
        tag_uid=TAG_A,
        tray_uuid=UUID_1,
        spent_at=datetime.utcnow(),
        weight_used=1121.5,
    )
    await _bind_row(db_session, spent, printer.id, 0, 2)

    obs = _obs(printer.id, {"id": 2, "state": 10})
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.NONE, reason="spent_occupied_owed_identify")
    assert transitions[0].applied is False
    assert env.identifies == [(printer.id, 0, 2, "spent_occupied_owed_identify")]
    assert (await _assignment(db_session, printer.id, 0, 2)).spool_id == spent.id  # nothing mutated
    assert await _spool_count(db_session) == 1


@pytest.mark.asyncio
async def test_the_spent_occupied_verdict_buys_its_own_read_occasion(db_session, printer_factory, env):
    """2026-08-07, spool 226 / 001-H2S slot 1 — the LIVENESS half.

    The verdict above routed to the identify lane, but the read it owed had no ENTITLEMENT:
    read occasions open only on a qualified physical cycle, a terminal sweep's between-prints
    policy, or a manual command, and this insert's presence edges were all missed. So the
    request stood forever against a read nobody would issue. The constellation now opens its
    own occasion, keyed on the BOUND SPOOL so a verdict re-derived on every push still costs
    exactly one read."""
    printer = await printer_factory()
    spent = await _spool(
        db_session,
        material="PETG",
        data_origin="rfid_auto",
        tag_uid=TAG_A,
        tray_uuid=UUID_1,
        spent_at=datetime.utcnow(),
    )
    await _bind_row(db_session, spent, printer.id, 0, 1)
    obs = _obs(printer.id, {"id": 1, "state": 10})
    deps = _deps(db_session, env)

    for _ in range(3):  # the verdict re-derives on every push
        transitions = await run_slot_pipeline(printer.id, [obs], deps)
        assert transitions[0].decision.reason == "spent_occupied_owed_identify"

    assert ams_presence._episode_occasion_epoch[(printer.id, 0, 1, "spent_occupied")] == spent.id
    assert ams_presence._read_occasion_open(printer.id, 0, 1) is True

    # Epoch semantics, through the real opener: the SAME bound spool never re-opens once the
    # read is spent; a different bound spool is a new constellation and does.
    ams_presence._consume_read_occasion(printer.id, 0, 1)
    await run_slot_pipeline(printer.id, [obs], deps)
    assert ams_presence._read_occasion_open(printer.id, 0, 1) is False
    ams_presence.open_spent_occupied_occasion(printer.id, 0, 1, spent.id + 1)
    assert ams_presence._read_occasion_open(printer.id, 0, 1) is True


@pytest.mark.asyncio
async def test_the_constellations_occasion_is_what_the_real_predicate_grants_the_read_on(
    db_session, printer_factory, env, monkeypatch
):
    """THE liveness pin, through the REAL need authority (only the commander is faked).

    ``identify_needed``'s spent-occupied arm requires an OPEN READ OCCASION, so before this
    fix the whole loop was: verdict → need → None → nothing, on every push forever (spool 226
    stood a day). With the constellation opening its own occasion the predicate grants the
    read, and grants it as ``discovery`` — which is both what suppresses its expected no-tag
    failure and what stamps the evidence ``read_answered_no_tag`` later adjudicates. The
    epoch caps it at ONE read: a second pass over the same binding buys nothing."""
    printer = await printer_factory()
    await _spent_tagged_slot(db_session, printer.id, 0, 1)
    commanded: list[tuple[int, int, int, str | None]] = []

    async def _record(printer_id, ams_id, tray_id, *, source, reason=None, **kwargs):
        commanded.append((printer_id, ams_id, tray_id, reason))
        return True, "ok"

    monkeypatch.setattr(slot_pipeline.ams_presence, "command_identify", _record)
    deps = _prod_identify_deps(db_session, env)  # real identify_needed, no injected hook
    obs = _obs(printer.id, {"id": 1, "state": 10})

    await run_slot_pipeline(printer.id, [obs], deps)
    assert commanded == [(printer.id, 0, 1, "discovery")]

    # What the real commander does on a successful read — the fake cannot.
    ams_presence._consume_read_occasion(printer.id, 0, 1)
    await run_slot_pipeline(printer.id, [obs], deps)
    assert len(commanded) == 1


# --- the PRODUCTION identify default ----------------------------------------


class _IdentifyProbe:
    """A fake ``ams_presence`` identify surface: records what the default CONSULTS and
    what it SPENDS.

    Every other test in this file injects ``_Recorder.schedule_identify``, which pins
    only that the pipeline owes a read. These tests remove that hook so the production
    default runs for real, and this probe stands in for the two ``ams_presence``
    functions it reaches — the need predicate and the single identify commander.
    """

    def __init__(self, need: str | None = None):
        self.need = need
        self.needs_asked: list[tuple[int, int, int, dict]] = []
        self.commanded: list[tuple[int, int, int, str | None]] = []

    async def identify_needed(self, db, printer_id, ams_id, tray_id, tray, spoolman_active):
        self.needs_asked.append((printer_id, ams_id, tray_id, dict(tray)))
        return self.need

    async def command_identify(self, printer_id, ams_id, tray_id, *, source, reason=None, **kwargs):
        self.commanded.append((printer_id, ams_id, tray_id, reason))
        return True, "ok"


@pytest.fixture
def identify_probe(monkeypatch):
    """Install an :class:`_IdentifyProbe` over ``ams_presence``'s identify surface."""

    def _install(need: str | None = None) -> _IdentifyProbe:
        probe = _IdentifyProbe(need)
        monkeypatch.setattr(slot_pipeline.ams_presence, "identify_needed", probe.identify_needed)
        monkeypatch.setattr(slot_pipeline.ams_presence, "command_identify", probe.command_identify)
        return probe

    return _install


def _prod_identify_deps(db_session, recorder: _Recorder, client=None) -> PipelineDeps:
    """Deps with NO identify hook injected, so ``PipelineDeps.identify`` runs its
    production default."""
    deps = _deps(db_session, recorder, client=client)
    deps.schedule_identify = None
    return deps


class TestTheProductionIdentifyDefaultIsNeedDriven:
    """008-H2C AMS2 slot2, 2026-08-02 — the permanent discovery-read loop.

    A dialect-odd slot (state 9, no filament type, no tag, stuck exist bit) presented
    presence=UNKNOWN with an occupancy signal, so it derived OCCUPIED_UNRESOLVED and
    resolved ``NONE(identity_unresolved)`` on EVERY push — a standing condition, not an
    event. The production default answered each one, so the farm logged
    ``identify commanded: AMS2 slot2 (source=reconcile, reason=discovery)`` every ~30 s
    (the client's identify gate was the ONLY thing pacing it) for a slot with nothing to
    read.

    Doctrine rule 3 / invariant 4: a DISCOVERY read is owed only for an UNANSWERED
    QUALIFIED PHYSICAL CYCLE, and ``ams_presence.identify_needed`` is the one authority
    on that. ``identity_unresolved`` is therefore a REQUEST the default must clear with
    the predicate, never an entitlement.
    """

    # The prod slot's own RAW push shape.
    PROD_TRAY = {"id": 2, "state": 9, "tray_type": ""}

    # ``tray_exist_bits`` bit for AMS2 slot2 (``ams_id * 4 + tray_id``) — the stuck bit
    # that vetoes the state-9 emptiness and leaves presence UNKNOWN.
    PROD_EXIST_BITS = 1 << (2 * 4 + 2)

    def _prod_obs(self, printer_id: int):
        """AMS2 slot2 with the stuck exist bit standing against the state-9 emptiness."""
        obs = observe_tray(printer_id, 2, dict(self.PROD_TRAY), exist_bits=self.PROD_EXIST_BITS)
        # The exact epistemic shape that makes this slot stand still forever: the mask
        # says SEATED while nothing names what is seated, so it can never be classified
        # EMPTY and never be identified either — a standing condition, not an event.
        assert obs.present is True and obs.occupancy_signal is True
        return obs

    @pytest.mark.asyncio
    async def test_a_standing_unknown_slot_is_never_read_on_a_cadence(
        self, db_session, printer_factory, env, identify_probe, caplog
    ):
        """THE loop-shape pin: N passes over an unresolved, unbound, no-cycle slot must
        spend ZERO identifies. One read per pass is the bug; a read per unanswered cycle
        is the contract."""
        printer = await printer_factory()
        probe = identify_probe(need=None)
        deps = _prod_identify_deps(db_session, env)

        with caplog.at_level(logging.DEBUG, logger=_PIPELINE_LOGGER):
            for _ in range(5):
                transitions = await run_slot_pipeline(printer.id, [self._prod_obs(printer.id)], deps)
                assert transitions[0].to_state is SlotState.OCCUPIED_UNRESOLVED
                assert transitions[0].decision == Decision(DecisionKind.NONE, reason="identity_unresolved")

        assert probe.commanded == []  # the read loop is gone...
        assert len(probe.needs_asked) == 5  # ...because the need is asked every pass
        # The predicate is fed THIS push's raw tray, addressed to the right slot.
        assert probe.needs_asked[0][:3] == (printer.id, 2, 2)
        assert probe.needs_asked[0][3] == self.PROD_TRAY
        noop = [m for m in _records(caplog, logging.DEBUG) if "standing unknown is not a read reason" in m]
        assert len(noop) == 5
        assert f"printer={printer.id} A2T2" in noop[0]
        assert await _spool_count(db_session) == 0  # and nothing was mutated either

    @pytest.mark.asyncio
    async def test_the_spent_occupied_request_is_need_gated_the_same_way(
        self, db_session, printer_factory, env, identify_probe
    ):
        """The spent-occupied verdict joins the SAME gate, deliberately: the predicate owns
        the occasion (its spent-occupied arm — state 10, no wire tag, an open read occasion,
        one attempt per occupancy epoch, because every failed read leaves a printer-side
        0700_0081 that only a power-cycle clears). Declined ⇒ nothing spent; granted ⇒
        commanded with the PREDICATE's reason."""
        printer = await printer_factory()
        spent = await _spool(
            db_session, material="PETG", rgba="000000FF", data_origin="rfid_auto", spent_at=datetime.utcnow()
        )
        await _bind_row(db_session, spent, printer.id, 0, 2)
        obs = _obs(printer.id, {"id": 2, "state": 10})

        refused = identify_probe(need=None)
        await run_slot_pipeline(printer.id, [obs], _prod_identify_deps(db_session, env))
        assert refused.commanded == []
        assert refused.needs_asked[0][:3] == (printer.id, 0, 2)

        granted = identify_probe(need="discovery")
        await run_slot_pipeline(printer.id, [obs], _prod_identify_deps(db_session, env))
        assert granted.commanded == [(printer.id, 0, 2, "discovery")]

    @pytest.mark.asyncio
    async def test_an_unanswered_qualified_cycle_still_gets_its_discovery_read(
        self, db_session, printer_factory, env, identify_probe
    ):
        """The gate narrows the OCCASION, never the capability: a slot that genuinely
        changed still buys its one answer."""
        printer = await printer_factory()
        probe = identify_probe(need="discovery")

        await run_slot_pipeline(printer.id, [self._prod_obs(printer.id)], _prod_identify_deps(db_session, env))

        assert probe.commanded == [(printer.id, 2, 2, "discovery")]

    @pytest.mark.asyncio
    async def test_the_predicates_own_verdict_is_what_gets_commanded(
        self, db_session, printer_factory, env, identify_probe
    ):
        """The decision reason is a request, the predicate's answer is the verdict: a
        slot the predicate finds live-tagged is read as ``rfid_refresh``, not as this
        decision's ``discovery`` guess."""
        printer = await printer_factory()
        probe = identify_probe(need="rfid_refresh")

        await run_slot_pipeline(printer.id, [self._prod_obs(printer.id)], _prod_identify_deps(db_session, env))

        assert probe.commanded == [(printer.id, 2, 2, "rfid_refresh")]

    @pytest.mark.asyncio
    async def test_the_partial_identity_defer_is_not_need_gated(self, db_session, printer_factory, env, identify_probe):
        """``partial_identity_owed_full_read`` is EVENT-shaped: the push carried one member
        of the identity pair and not the other, which a single full read resolves outright.
        The push itself is the evidence, so it commands ``rfid_refresh`` without consulting
        the predicate at all (which here would veto it)."""
        printer = await printer_factory()
        probe = identify_probe(need=None)
        deps = _prod_identify_deps(db_session, env)

        # Tag with no uuid, on an unbound slot no row owns.
        obs = _obs(printer.id, {"id": 3, "state": 11, "tag_uid": TAG_B, "tray_type": "PETG"})
        transitions = await run_slot_pipeline(printer.id, [obs], deps)

        assert transitions[0].decision == Decision(DecisionKind.DEFER, reason="partial_identity_owed_full_read")
        assert probe.needs_asked == []
        assert probe.commanded == [(printer.id, 0, 3, "rfid_refresh")]

    @pytest.mark.asyncio
    async def test_the_ambiguity_defer_is_need_gated_and_buys_one_read_per_episode(
        self, db_session, printer_factory, env, identify_probe
    ):
        """``identity_ambiguous_owed_full_read`` STANDS — the wire re-asserts the
        disagreeing tag at ~1 Hz for as long as the roll sits there — so passing it
        straight through re-bought the same read on every push. It is need-gated now, and
        the ambiguity EPISODE buys the single occasion the predicate spends."""
        printer = await printer_factory()
        roll = await _spool(db_session, tag_uid=TAG_A, tray_uuid=UUID_1, material="PETG", rgba="000000FF")
        await _bind_row(db_session, roll, printer.id, 0, 3)
        deps = _prod_identify_deps(db_session, env)
        obs = _obs(printer.id, {"id": 3, "state": 11, "tag_uid": TAG_B, "tray_type": "PETG"})

        refused = identify_probe(need=None)
        transitions = await run_slot_pipeline(printer.id, [obs], deps)
        assert transitions[0].decision == Decision(DecisionKind.DEFER, reason="identity_ambiguous_owed_full_read")
        assert refused.needs_asked[0][:3] == (printer.id, 0, 3), "the predicate is consulted now"
        assert refused.commanded == []

        # The episode bought exactly one occasion, however many pushes re-derive it.
        assert ams_presence._read_occasion_open(printer.id, 0, 3) is True
        assert ams_presence._episode_occasion_epoch[(printer.id, 0, 3, "identity_ambiguous")] == (roll.id, TAG_B)

        granted = identify_probe(need="rfid_refresh")
        await run_slot_pipeline(printer.id, [obs], deps)
        assert granted.commanded == [(printer.id, 0, 3, "rfid_refresh")]

    @pytest.mark.asyncio
    async def test_the_real_predicate_refuses_the_prod_slot(self, db_session, printer_factory, env, monkeypatch):
        """The end-to-end pin, with the REAL ``identify_needed``.

        Fed the 008-H2C slot's own raw push it answers None — state 9 is not presence,
        so no ``rfid_refresh`` applies, and no qualified physical cycle was ever recorded
        for the slot, so no discovery is owed. Only ``command_identify`` is faked (it
        would otherwise reach for a live client).
        """
        printer = await printer_factory()
        commanded: list[tuple[int, int, int, str | None]] = []

        async def _record(printer_id, ams_id, tray_id, *, source, reason=None, **kwargs):
            commanded.append((printer_id, ams_id, tray_id, reason))
            return True, "ok"

        monkeypatch.setattr(slot_pipeline.ams_presence, "command_identify", _record)
        deps = _prod_identify_deps(db_session, env)

        for _ in range(3):
            await run_slot_pipeline(printer.id, [self._prod_obs(printer.id)], deps)

        assert commanded == []


# --- serialization + never-raise --------------------------------------------


def _order_probe(order: list[str], tag: str):
    """An identify hook that records when a pass enters and leaves its critical section."""

    async def _identify(printer_id, ams_id, tray_id, reason):
        order.append(f"{tag}-start")
        await asyncio.sleep(0.01)
        order.append(f"{tag}-end")

    return _identify


@pytest.mark.asyncio
async def test_two_passes_for_one_printer_run_sequentially(db_session, printer_factory, env, monkeypatch):
    """Read-decide-write must not interleave for one printer.

    Two AMS pushes ~30 ms apart is ordinary MQTT burst behaviour; if both read "no
    assignment for this slot" before either writes, both INSERT and the second one hits
    the unique constraint. This lock is the fork's ONE defence against that (it replaced
    ``main.py``'s per-printer assignment lock at the W3b cutover).
    """
    printer = await printer_factory()
    order: list[str] = []

    obs = _obs(printer.id, {"id": 0, "state": 10})
    deps_a = _deps(db_session, env)
    deps_a.schedule_identify = _order_probe(order, "A")
    deps_b = _deps(db_session, env)
    deps_b.schedule_identify = _order_probe(order, "B")

    await asyncio.gather(
        run_slot_pipeline(printer.id, [obs], deps_a),
        run_slot_pipeline(printer.id, [obs], deps_b),
    )

    assert order in (["A-start", "A-end", "B-start", "B-end"], ["B-start", "B-end", "A-start", "A-end"])


@pytest.mark.asyncio
async def test_passes_for_different_printers_run_in_parallel(db_session, printer_factory, env):
    """The lock is per-PRINTER, not global.

    A fleet lock would serialise every printer's AMS traffic behind the slowest one —
    the reason the pre-cutover lock was keyed by printer id too. Interleaved start/end
    markers are the proof that two printers' passes overlap.
    """
    printer_a = await printer_factory()
    printer_b = await printer_factory()
    order: list[str] = []

    deps_a = _deps(db_session, env)
    deps_a.schedule_identify = _order_probe(order, "A")
    deps_b = _deps(db_session, env)
    deps_b.schedule_identify = _order_probe(order, "B")

    await asyncio.gather(
        run_slot_pipeline(printer_a.id, [_obs(printer_a.id, {"id": 0, "state": 10})], deps_a),
        run_slot_pipeline(printer_b.id, [_obs(printer_b.id, {"id": 0, "state": 10})], deps_b),
    )

    assert order[:2] in (["A-start", "B-start"], ["B-start", "A-start"])  # both entered before either left


@pytest.mark.asyncio
async def test_the_pass_stands_down_entirely_under_spoolman(db_session, printer_factory, env):
    """Spoolman mode owns AMS slots end to end (its own sync lane, and the mode switch
    wipes the internal assignments). The pipeline must not decide identity for rows it
    does not own — and must not merely skip WRITES, or a released binding would still
    fire."""
    printer = await printer_factory()
    spool = await _spool(db_session, material="PETG", rgba="000000FF")
    await _bind_row(db_session, spool, printer.id, 0, 1)
    env.settings["spoolman_enabled"] = "true"

    cleared = _obs(printer.id, {"id": 1, "state": 9, "tray_type": ""})  # would RELEASE in internal mode
    transitions = await run_slot_pipeline(printer.id, [cleared], _deps(db_session, env))

    assert transitions == []
    assert await _assignment(db_session, printer.id, 0, 1) is not None  # binding untouched


@pytest.mark.asyncio
async def test_a_poisoned_observation_never_escapes_and_the_pass_continues(db_session, printer_factory, env, caplog):
    """Cross-cutting invariant 10: no farm-side failure may break the AMS callback."""
    printer = await printer_factory()
    spool = await _spool(db_session, material="PETG", rgba="000000FF")
    await _bind_row(db_session, spool, printer.id, 0, 2)

    good = _obs(printer.id, {"id": 2, "state": 9, "tray_type": ""})
    with caplog.at_level(logging.ERROR, logger=_PIPELINE_LOGGER):
        transitions = await run_slot_pipeline(printer.id, [object(), good], _deps(db_session, env))

    assert [t.decision.kind for t in transitions] == [DecisionKind.RELEASE]  # the healthy slot still ran
    assert _records(caplog, logging.ERROR)
    assert await _assignment(db_session, printer.id, 0, 2) is None


# --- unknown-tag prompt -----------------------------------------------------


class TestUnknownTagPrompt:
    """With auto-add OFF the table refuses to mint an unowned roll, and the operator
    gets a per-slot prompt instead. The prompt and its dedup moved here from
    ``main.py`` at the W3b cutover, because exactly one decision raises it and exactly
    two pipeline outcomes — an emptied slot and a successful bind — clear it."""

    TAG = "AABBCCDD00000100"
    UUID = "8AC9EC0847FD41D0890870319F2E1975"

    def _tagged(self, printer_id, tray_id=0, tag=None, uuid=None):
        return _obs(
            printer_id,
            {
                "id": tray_id,
                "state": 11,
                "tag_uid": tag or self.TAG,
                "tray_uuid": uuid or self.UUID,
                "tray_type": "PETG",
                "tray_color": "000000FF",
            },
        )

    def _prompts(self, env):
        return [p for p in env.broadcasts if p.get("type") == "unknown_tag"]

    @pytest.mark.asyncio
    async def test_auto_add_off_prompts_instead_of_minting(self, db_session, printer_factory, env):
        printer = await printer_factory()
        env.settings["auto_add_unknown_rfid"] = "false"

        transitions = await run_slot_pipeline(printer.id, [self._tagged(printer.id)], _deps(db_session, env))

        assert transitions[0].decision.kind is DecisionKind.NONE
        assert transitions[0].decision.reason == "unknown_tag_prompt_owed"
        assert await _spool_count(db_session) == 0  # nothing minted behind the operator's back
        prompt = self._prompts(env)[0]
        assert (prompt["tag_uid"], prompt["tray_uuid"]) == (self.TAG, self.UUID)
        assert prompt["tray_count"] == 1

    @pytest.mark.asyncio
    async def test_the_same_roll_prompts_once_not_once_per_push(self, db_session, printer_factory, env):
        printer = await printer_factory()
        env.settings["auto_add_unknown_rfid"] = "false"
        deps = _deps(db_session, env)

        for _ in range(3):
            await run_slot_pipeline(printer.id, [self._tagged(printer.id)], deps)

        assert len(self._prompts(env)) == 1

    @pytest.mark.asyncio
    async def test_emptying_the_slot_lets_the_next_roll_re_prompt(self, db_session, printer_factory, env):
        printer = await printer_factory()
        env.settings["auto_add_unknown_rfid"] = "false"
        deps = _deps(db_session, env)

        await run_slot_pipeline(printer.id, [self._tagged(printer.id)], deps)
        await run_slot_pipeline(printer.id, [_obs(printer.id, {"id": 0, "state": 9, "tray_type": ""})], deps)
        await run_slot_pipeline(printer.id, [self._tagged(printer.id)], deps)

        assert len(self._prompts(env)) == 2  # remove + reinsert re-asks

    @pytest.mark.asyncio
    async def test_a_bind_answers_the_prompt(self, db_session, printer_factory, env):
        """Once a row owns the slot the prompt is moot, so the dedup is dropped — a
        LATER unknown roll in the same slot must be able to raise a fresh one."""
        printer = await printer_factory()
        env.settings["auto_add_unknown_rfid"] = "false"
        deps = _deps(db_session, env)

        await run_slot_pipeline(printer.id, [self._tagged(printer.id)], deps)
        assert len(self._prompts(env)) == 1

        # The operator adds it: auto-add back on, the same roll now mints + binds.
        env.settings["auto_add_unknown_rfid"] = "true"
        await run_slot_pipeline(printer.id, [self._tagged(printer.id)], deps)
        assert await _assignment(db_session, printer.id, 0, 0) is not None

        # A DIFFERENT unknown roll arrives in that slot with auto-add off again. Both
        # identity members differ — a tag-only change would be a SIBLING read of the
        # bound roll (uuid agreement wins) and must NOT prompt.
        env.settings["auto_add_unknown_rfid"] = "false"
        other = self._tagged(printer.id, tag="1122334400000100", uuid="0F8FCF6039964FB68F94A59F8B0897D8")
        await run_slot_pipeline(printer.id, [other], deps)
        assert len(self._prompts(env)) == 2


# --- replay pins ------------------------------------------------------------


class TestProdSiblingSlotsThroughThePipeline:
    """The four 2026-08-01 production slots, end to end through the orchestrator.

    Stage 1 is a partial push carrying only the far-side tag: the binding must be
    untouched and an identify owed. Stage 2 is the identify's full read: the uuid
    settles it (same roll, other chip) and the binding is KEPT with no new row. Minting
    on stage 1 would have created a duplicate ledger row for all four rolls.
    """

    PROD_SIBLING_SLOTS = [
        (46, "EC96F1E700000100", "3CF1F3E700000100", "8AC9EC0847FD41D0890870319F2E1975"),
        (194, "A5E7210D00000100", "95F6F50C00000100", "3C78FA47DFCC4F0C8C95566C77A73DCE"),
        (196, "66839BE000000100", "D6385CEC00000100", "0F8FCF6039964FB68F94A59F8B0897D8"),
        (186, "CBB0D0FE00000100", "2338393200000100", "A74AC09B2B8443BCB0112C15631EFCEC"),
    ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("_spool_id", "stored_tag", "wire_tag", "tray_uuid"), PROD_SIBLING_SLOTS)
    async def test_stage_1_then_stage_2(
        self, db_session, printer_factory, env, _spool_id, stored_tag, wire_tag, tray_uuid
    ):
        printer = await printer_factory()
        roll = await _spool(db_session, tag_uid=stored_tag, tray_uuid=tray_uuid, material="PETG", rgba="000000FF")
        await _bind_row(db_session, roll, printer.id, 0, 3)
        deps = _deps(db_session, env)

        stage_1 = _obs(printer.id, {"id": 3, "state": 11, "tag_uid": wire_tag, "tray_type": "PETG"})
        first = await run_slot_pipeline(printer.id, [stage_1], deps)

        assert first[0].decision == Decision(DecisionKind.DEFER, reason="identity_ambiguous_owed_full_read")
        assert env.identifies == [(printer.id, 0, 3, "identity_ambiguous_owed_full_read")]
        assert (await _assignment(db_session, printer.id, 0, 3)).spool_id == roll.id
        assert await _spool_count(db_session) == 1

        stage_2 = _obs(
            printer.id,
            {"id": 3, "state": 11, "tag_uid": wire_tag, "tray_uuid": tray_uuid, "tray_type": "PETG"},
        )
        second = await run_slot_pipeline(printer.id, [stage_2], deps)

        assert second[0].decision == Decision(DecisionKind.KEEP, spool_id=roll.id, reason="sibling_tag_read")
        assert second[0].applied is False
        assert (await _assignment(db_session, printer.id, 0, 3)).spool_id == roll.id
        assert await _spool_count(db_session) == 1  # one roll, one ledger row
        assert env.broadcasts == []


class TestStaleEmptyReplay003T2:
    """003-H2S T2 / spool 140: a live tagless row (932 g used) bound to a slot the wire
    reports EMPTY. ``should_keep_on_empty`` kept it forever; the pipeline releases it
    and stamps the last location so a re-insert reclaims the grams."""

    TRAY = {"id": 2, "state": 9, "tray_type": ""}

    @pytest.mark.asyncio
    async def test_released_with_last_location_stamped(self, db_session, printer_factory, env, caplog):
        printer = await printer_factory()
        spool = await _spool(db_session, material="PETG", rgba="000000FF", weight_used=932, data_origin="ams_auto")
        await _bind_row(db_session, spool, printer.id, 0, 2)

        with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
            transitions = await run_slot_pipeline(printer.id, [_obs(printer.id, self.TRAY)], _deps(db_session, env))

        assert transitions[0].from_state is SlotState.OCCUPIED_ASSUMED
        assert transitions[0].to_state is SlotState.EMPTY
        assert transitions[0].decision == Decision(DecisionKind.RELEASE, spool_id=spool.id, reason="cleared_tray")
        assert await _assignment(db_session, printer.id, 0, 2) is None
        await db_session.refresh(spool)
        assert spool.last_location_at is not None
        assert spool.archived_at is None  # the roll is inventory, not retired
        # The AUDIT line specifically: a release now also emits WS7's ``release-evidence``
        # record from this same logger, so the selector names the audit grammar's own
        # prefix instead of taking whichever ``[slot-state]`` line came first.
        line = next(m for m in _records(caplog, logging.INFO) if m.startswith(f"[slot-state] printer={printer.id}"))
        assert line == (
            f"[slot-state] printer={printer.id} A0T2 OCCUPIED_ASSUMED→EMPTY release "
            f"spool={spool.id} reason=cleared_tray"
        )


# --- the applied audit line is a TRANSITION, not a tautology -----------------


def _transition(caplog) -> tuple[str, str]:
    """The ``FROM→TO`` pair from the pass's audit line.

    Matched on the ARROW, not merely the ``[slot-state]`` prefix: the module logs
    other INFO lines under that prefix (a disposal, a sibling KEEP) and only the
    audit line carries a transition.
    """
    line = next(m for m in _records(caplog, logging.INFO) if m.startswith("[slot-state]") and "→" in m)
    left, right = line.split()[3].split("→")
    return left, right


class TestAppliedLineShowsTheChange:
    """The right-hand state used to be ``derive_state`` against the PRE-transition
    binding, so the one line an operator reads to see what changed printed things like
    ``SPENT_AWAITING_SWAP→SPENT_AWAITING_SWAP replace_spent`` (observed in prod). Every
    state-CHANGING kind must now show two different states.
    """

    @pytest.mark.asyncio
    async def test_replace_spent_reads_spent_to_assumed(self, db_session, printer_factory, env, caplog):
        """The exact prod tautology, now a transition: the latch is gone and the
        replacement row is ASSUMED (nothing has read the fresh roll yet)."""
        printer = await printer_factory()
        env.settings["tagless_default_filament"] = _tagless_default_json()
        await _spent_slot(db_session, printer.id)

        tray = {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"}
        with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
            transitions = await run_slot_pipeline(printer.id, [_obs(printer.id, tray)], _deps(db_session, env))

        assert transitions[0].applied is True
        assert transitions[0].decision.kind is DecisionKind.REPLACE_SPENT
        assert _transition(caplog) == ("SPENT_AWAITING_SWAP", "OCCUPIED_ASSUMED")
        assert transitions[0].from_state is SlotState.SPENT_AWAITING_SWAP
        assert transitions[0].to_state is SlotState.OCCUPIED_ASSUMED

    @pytest.mark.asyncio
    async def test_release_reads_to_empty(self, db_session, printer_factory, env, caplog):
        printer = await printer_factory()
        spool = await _spool(db_session, material="PETG", rgba="000000FF", weight_used=932)
        await _bind_row(db_session, spool, printer.id, 0, 2)

        with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
            transitions = await run_slot_pipeline(
                printer.id, [_obs(printer.id, {"id": 2, "state": 9, "tray_type": ""})], _deps(db_session, env)
            )

        assert transitions[0].applied is True
        from_state, to_state = _transition(caplog)
        assert to_state == "EMPTY"
        assert from_state != to_state

    @pytest.mark.asyncio
    async def test_an_identity_bind_reads_to_identified(self, db_session, printer_factory, env, caplog):
        """A tag was READ and resolved onto a row — the slot is identified, not assumed,
        and ``derive_state`` could not have said so from the pre-transition binding
        (there was none)."""
        printer = await printer_factory()
        roll = await _spool(db_session, tag_uid=TAG_A, tray_uuid=UUID_1, material="PETG", rgba="000000FF")

        arrived = _obs(
            printer.id,
            {
                "id": 1,
                "state": 11,
                "tag_uid": TAG_A,
                "tray_uuid": UUID_1,
                "tray_type": "PETG",
                "tray_color": "000000FF",
            },
        )
        with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
            transitions = await run_slot_pipeline(printer.id, [arrived], _deps(db_session, env))

        assert transitions[0].decision == Decision(
            DecisionKind.BIND, spool_id=roll.id, reason="identity_resolved_candidate"
        )
        assert _transition(caplog) == ("EMPTY", "OCCUPIED_IDENTIFIED")

    @pytest.mark.asyncio
    async def test_a_tagless_mint_reads_to_assumed(self, db_session, printer_factory, env, caplog):
        """No tag was read: a fingerprint mint is an assumption, however confident."""
        printer = await printer_factory()

        with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
            transitions = await run_slot_pipeline(
                printer.id,
                [_obs(printer.id, {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})],
                _deps(db_session, env),
            )

        assert transitions[0].decision.reason == "tagless_mint"
        assert _transition(caplog) == ("EMPTY", "OCCUPIED_ASSUMED")

    @pytest.mark.asyncio
    async def test_a_non_applied_pass_keeps_the_derived_state(self, db_session, printer_factory, env):
        """Nothing was written, so the derived classification stands — and no audit
        line is emitted at all (the line means "the ledger changed")."""
        printer = await printer_factory()
        spool = await _spool(db_session, material="PETG", rgba="000000FF")
        await _bind_row(db_session, spool, printer.id, 0, 0, fingerprint_color="000000FF", fingerprint_type="PETG")

        transitions = await run_slot_pipeline(
            printer.id,
            [_obs(printer.id, {"id": 0, "state": 11, "tray_type": "PETG", "tray_color": "000000FF"})],
            _deps(db_session, env),
        )

        assert transitions[0].applied is False
        assert transitions[0].decision.kind is DecisionKind.KEEP
        assert transitions[0].to_state is SlotState.OCCUPIED_ASSUMED  # == derive_state


class TestReusedCoreAtTheApplyLayer:
    """G3 through the ORCHESTRATOR — doctrine rule 3 / operator ruling 3 (2026-08-19).

    A runout means the row reached zero; filament cannot be added to a 0 g roll, so the
    same tag reading back over a seated tray is a NEW roll on a reused core. The table
    concludes that; these pins are about what the apply layer then DOES, which is where
    the conclusion could still be undone:

    * the mint existence recheck (``_apply_mint``) re-asks the DB who owns this identity
      and converts a MINT into a BIND onto that row. That guard is what stops one physical
      roll becoming two rows — but a FINISHED row is not a valid owner, so converting onto
      it would BE the resurrection, reached THROUGH the guard rather than around it;
    * ``_apply_replace_spent`` demanded a qualified physical cycle for the BOUND shape,
      which an identity read never has to earn (invariant 11 — identity-tier evidence may
      displace ANY binding, and the cycle it waives is assumption-tier).

    Both shapes must converge on the same physical truth: the finished row retired with its
    grams and its ``spent_at``, a fresh row carrying the tag on the slot.
    """

    TAG = "1C63F1E700000100"
    UUID = "8AC9EC0847FD41D0890870319F2E1975"

    def _tagged(self, printer_id, tray_id=0, **overrides):
        tray = {
            "id": tray_id,
            "state": 11,
            "tag_uid": self.TAG,
            "tray_uuid": self.UUID,
            "tray_type": "PETG",
            "tray_color": "000000FF",
            "tray_info_idx": "GFG02",
        }
        tray.update(overrides)
        return _obs(printer_id, tray)

    async def _finished(self, db_session, **overrides):
        """The drained roll: spent, ledger-bearing, still owning its tag."""
        fields = {
            "material": "PETG",
            "rgba": "000000FF",
            "brand": "Bambu Lab",
            "data_origin": "rfid_auto",
            "tag_uid": self.TAG,
            "tray_uuid": self.UUID,
            "weight_used": 987.4,
            "spent_at": _SENTINEL,
        }
        fields.update(overrides)
        return await _spool(db_session, **fields)

    @pytest.mark.asyncio
    async def test_the_unbound_shape_mints_the_successor_instead_of_resurrecting(
        self, db_session, printer_factory, env, caplog
    ):
        """THE G3 REPLAY, unbound — the normal post-runout shape for a tagged roll.

        The AMS clears a drained slot's exist bit ~3 min BEFORE it declares the runout, so
        the binding is released first and the spent stamp lands on an UNBOUND row. The
        operator puts a fresh roll on the reused core and the tag reads. Before this fix the
        table refused to bind the drained row (correct) and then stopped (not correct): the
        slot stayed unresolved, because the MINT it should have emitted would have been
        converted straight back onto the finished row by the existence recheck.
        """
        printer = await printer_factory()
        finished = await self._finished(db_session)
        await db_session.commit()
        finished_id = finished.id

        with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
            transitions = await run_slot_pipeline(printer.id, [self._tagged(printer.id)], _deps(db_session, env))

        assert transitions[0].applied is True
        assert transitions[0].decision.kind is DecisionKind.MINT
        # A FRESH row holds the slot, and it carries the tag — so the reused core resolves
        # by identity on the very next push instead of re-deciding this branch.
        row = await _assignment(db_session, printer.id, 0, 0)
        assert row.spool_id != finished_id
        successor = await db_session.get(Spool, row.spool_id)
        assert (successor.tag_uid, successor.tray_uuid) == (self.TAG, self.UUID)
        assert successor.spent_at is None and successor.weight_used == 0
        # The finished row keeps its grams and its stamp, and is NOT re-bound.
        await db_session.refresh(finished)
        assert finished.spent_at == _SENTINEL
        assert finished.weight_used == pytest.approx(987.4)
        held = await db_session.scalar(
            select(func.count(SpoolAssignment.id)).where(SpoolAssignment.spool_id == finished_id)
        )
        assert held == 0
        assert any("is a FINISHED roll" in line for line in _records(caplog, logging.INFO))

    @pytest.mark.asyncio
    async def test_the_retired_row_stops_owning_the_identity(self, db_session, printer_factory, env):
        """Two ACTIVE rows must never own one identity — the guard's own premise.

        ``_identity_candidate`` answers "who owns this?" with a ``LIMIT 1`` over active
        rows, so leaving both would make it answer differently from one push to the next:
        the roll's NEXT slot would find the finished one, refuse it again, and mint a THIRD
        row for the same physical roll. Archiving is a soft hide — the ledger stays raw
        (rule 8) — so the retirement costs the drained row nothing it was carrying.
        """
        printer = await printer_factory()
        finished = await self._finished(db_session)
        await db_session.commit()

        await run_slot_pipeline(printer.id, [self._tagged(printer.id)], _deps(db_session, env))

        await db_session.refresh(finished)
        assert finished.archived_at is not None
        assert finished.spent_at == _SENTINEL and finished.weight_used == pytest.approx(987.4)
        # …and the follow-up push simply KEEPs: exactly one active owner remains.
        second = await run_slot_pipeline(printer.id, [self._tagged(printer.id)], _deps(db_session, env))
        assert second[0].decision.kind is DecisionKind.KEEP
        assert await _spool_count(db_session) == 2  # the retired row + its successor, no third

    @pytest.mark.asyncio
    async def test_the_bound_shape_swaps_without_a_pending_cycle(self, db_session, printer_factory, env):
        """The BOUND shape, cycle-free — the ``_CYCLELESS_SWAP_REASONS`` entry.

        Nobody pulled anything the farm saw: the roll ran dry in the tray, the operator
        wound a fresh roll onto the same core and put it back while no qualified cycle
        happened to be pending. The evidence is an IDENTITY READ, which outranks the
        physical cycle the set waives, so the swap must not wait for one.
        """
        printer = await printer_factory()
        finished = await self._finished(db_session)
        await _bind_row(db_session, finished, printer.id, 0, 0)
        assert spool_tagless.qualified_cycle_pending(printer.id, 0, 0) is False

        transitions = await run_slot_pipeline(printer.id, [self._tagged(printer.id)], _deps(db_session, env))

        assert transitions[0].decision.reason == "reused_core_swap"
        assert transitions[0].applied is True
        row = await _assignment(db_session, printer.id, 0, 0)
        assert row.spool_id != finished.id
        successor = await db_session.get(Spool, row.spool_id)
        assert (successor.tag_uid, successor.tray_uuid) == (self.TAG, self.UUID)
        assert successor.spent_at is None
        await db_session.refresh(finished)
        assert finished.archived_at is not None  # retired, ledger intact
        assert finished.spent_at == _SENTINEL and finished.weight_used == pytest.approx(987.4)

    @pytest.mark.asyncio
    async def test_the_swap_fires_on_a_tray_the_printer_has_not_fed(self, db_session, printer_factory, env):
        """The TRAY-SHAPE half of the ``_CYCLELESS_SWAP_REASONS`` entry.

        ``tray_loaded`` is the CYCLE arm's corroboration that a roll is really in the slot,
        and it answers False for **state 10** — seated, filament not engaged. That is the
        ordinary shape at the moment an insert's RFID read lands (the AMS reads the chip
        before anything is fed), so demanding it here would emit ``reused_core_swap`` on
        every push and grant it on none: the emit-versus-grant mismatch, one layer down,
        with the fresh roll printing against a 0 g ledger the whole time. An identity read
        needs no such corroboration, so the arm re-asserts the very presence the table
        decided on (invariant 3's tri-state, not a second state test that could disagree).
        """
        printer = await printer_factory()
        finished = await self._finished(db_session)
        await _bind_row(db_session, finished, printer.id, 0, 0)

        seated = self._tagged(printer.id, state=10)
        assert seated.present is True
        assert spool_tagless.tray_loaded(observation_tray_dict(seated)) is False

        transitions = await run_slot_pipeline(printer.id, [seated], _deps(db_session, env))

        assert transitions[0].decision.reason == "reused_core_swap"
        assert transitions[0].applied is True
        row = await _assignment(db_session, printer.id, 0, 0)
        assert row.spool_id != finished.id
        successor = await db_session.get(Spool, row.spool_id)
        assert (successor.tag_uid, successor.tray_uuid) == (self.TAG, self.UUID)
        await db_session.refresh(finished)
        assert finished.archived_at is not None and finished.spent_at == _SENTINEL

    @pytest.mark.asyncio
    async def test_the_bound_shape_converges_with_a_pending_cycle(self, db_session, printer_factory, env):
        """…and the same shape WITH a cycle pending lands the same way.

        The cycle is consumed rather than required, so the two paths differ only in whether
        a physical event happened to be observed — never in the outcome.
        """
        printer = await printer_factory()
        finished = await self._finished(db_session)
        await _bind_row(db_session, finished, printer.id, 0, 0)
        spool_tagless._pending_physical_cycles.add((printer.id, 0, 0))

        transitions = await run_slot_pipeline(printer.id, [self._tagged(printer.id)], _deps(db_session, env))

        assert transitions[0].decision.reason == "reused_core_swap"
        assert transitions[0].applied is True
        assert spool_tagless.qualified_cycle_pending(printer.id, 0, 0) is False  # spent, not replayable
        row = await _assignment(db_session, printer.id, 0, 0)
        successor = await db_session.get(Spool, row.spool_id)
        assert successor.id != finished.id
        assert (successor.tag_uid, successor.tray_uuid) == (self.TAG, self.UUID)
        await db_session.refresh(finished)
        assert finished.archived_at is not None
        assert finished.spent_at == _SENTINEL

    @pytest.mark.asyncio
    async def test_a_LIVE_owner_still_converts_the_mint_into_a_bind(self, db_session, printer_factory, env, caplog):
        """THE REGRESSION GATE for the guard. One physical roll, one row — untouched.

        The exemption is FINISHED rolls and nothing else. A live row owning this identity
        still takes the bind, which is what stops a concurrent pass or an operator add from
        being duplicated by the mint the table decided a moment earlier. Same mint spec as
        the G3 case above, so the two are separated by the row's state alone.
        """
        printer = await printer_factory()
        live = await self._finished(db_session, spent_at=None, weight_used=120.0)
        await db_session.commit()

        deps = _deps(db_session, env)
        decision = Decision(
            DecisionKind.MINT,
            mint_spec={
                "source": "tray",
                "tag_uid": self.TAG,
                "tray_uuid": self.UUID,
                "tray_type": "PETG",
                "tray_color": "000000FF",
            },
            reason="unknown_identity_auto_add",
        )
        with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
            applied_decision, applied = await slot_pipeline._apply_mint(self._tagged(printer.id), deps, decision, set())

        assert applied is True
        assert applied_decision == Decision(DecisionKind.BIND, spool_id=live.id, reason="unknown_identity_auto_add")
        assert (await _assignment(db_session, printer.id, 0, 0)).spool_id == live.id
        assert await _spool_count(db_session) == 1  # converted, never duplicated
        await db_session.refresh(live)
        assert live.archived_at is None and live.weight_used == pytest.approx(120.0)
        assert any("mint converted to bind" in line for line in _records(caplog, logging.INFO))

    @pytest.mark.asyncio
    async def test_a_live_tagged_roll_reseated_still_keeps_by_identity(self, db_session, printer_factory, env):
        """LIVENESS. The overwhelmingly common event on this fleet — a tagged roll simply
        being re-read — must still KEEP, with no mint, no retire and no churn."""
        printer = await printer_factory()
        live = await self._finished(db_session, spent_at=None, weight_used=310.0)
        await _bind_row(db_session, live, printer.id, 0, 0)

        transitions = await run_slot_pipeline(printer.id, [self._tagged(printer.id)], _deps(db_session, env))

        assert transitions[0].decision == Decision(DecisionKind.KEEP, spool_id=live.id, reason="identity_matches_bound")
        assert transitions[0].applied is False
        assert (await _assignment(db_session, printer.id, 0, 0)).spool_id == live.id
        assert await _spool_count(db_session) == 1

    @pytest.mark.asyncio
    async def test_an_ordinary_unknown_tag_still_mints_one_row(self, db_session, printer_factory, env):
        """LIVENESS. A tag nothing owns is still an auto-add: the guard's skip is scoped to
        a row that EXISTS and is finished, so the plain mint lane is unchanged."""
        printer = await printer_factory()

        transitions = await run_slot_pipeline(printer.id, [self._tagged(printer.id)], _deps(db_session, env))

        assert transitions[0].decision.reason == "unknown_identity_auto_add"
        assert transitions[0].applied is True
        assert await _spool_count(db_session) == 1
        minted = await db_session.get(Spool, (await _assignment(db_session, printer.id, 0, 0)).spool_id)
        assert (minted.tag_uid, minted.tray_uuid) == (self.TAG, self.UUID)
