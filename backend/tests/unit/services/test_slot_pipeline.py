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
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, func, select

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.services import slot_pipeline, spool_binding, spool_tagless
from backend.app.services.slot_pipeline import PipelineDeps, run_slot_pipeline
from backend.app.services.slot_state import Decision, DecisionKind, SlotState
from backend.app.services.tray_observation import observe_tray

_PIPELINE_LOGGER = "backend.app.services.slot_pipeline"

# A stamp no clock can produce during a test run, so "preserved" is decidable without
# sleeping on wall-clock resolution (same device as test_spool_binding.py).
_SENTINEL = datetime(2020, 1, 2, 3, 4, 5)


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state():
    """Every test starts with empty locks, dedup sets, cycles and move damper."""
    slot_pipeline._reset_state()
    spool_tagless._reset_state()
    spool_binding._move_damper.reset()
    yield
    slot_pipeline._reset_state()
    spool_tagless._reset_state()
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
    """Minimal stand-in for a BambuMQTTClient's read-only wire-safety surface."""

    def __init__(self, *, drying: bool = False, refusal: str | None = None, gcode_state: str | None = None):
        self._drying = drying
        self._refusal = refusal
        self.state = SimpleNamespace(state=gcode_state)

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
async def test_sibling_keep_logs_once_per_slot_and_tag(db_session, printer_factory, env, caplog):
    """The one KEEP where stored identity visibly disagrees with the wire gets ONE
    INFO line — repeated every push it would bury the fact it exists to surface."""
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
    assert second[0].decision.reason == "sibling_tag_read"
    sibling_lines = [m for m in _records(caplog, logging.INFO) if m.startswith("[sibling-tag]")]
    assert len(sibling_lines) == 1
    assert f"spool={spool.id}" in sibling_lines[0]


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
    transitions = await run_slot_pipeline(printer.id, [obs], _deps(db_session, env))

    assert transitions[0].decision == Decision(DecisionKind.RECLAIM, spool_id=donor.id, reason="last_location_reclaim")
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
        """AMS2 slot2 with the stuck exist bit vetoing the state-9 emptiness."""
        obs = observe_tray(printer_id, 2, dict(self.PROD_TRAY), exist_bits=self.PROD_EXIST_BITS)
        # The exact epistemic shape that makes this slot stand still forever: presence
        # unknowable, occupancy signalled — so it can never be classified EMPTY either.
        assert obs.present is None and obs.occupancy_signal is True
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
    async def test_the_full_read_defer_is_not_need_gated(self, db_session, printer_factory, env, identify_probe):
        """``identity_ambiguous_owed_full_read`` arises ONLY on a tag-asserting push over
        a bound slot: the push itself is the evidence and the read is guaranteed
        answerable, so it commands ``rfid_refresh`` without consulting the predicate at
        all (which here would veto it)."""
        printer = await printer_factory()
        roll = await _spool(db_session, tag_uid=TAG_A, tray_uuid=UUID_1, material="PETG", rgba="000000FF")
        await _bind_row(db_session, roll, printer.id, 0, 3)
        probe = identify_probe(need=None)
        deps = _prod_identify_deps(db_session, env)

        # Tag-only disagreement against a binding that claims an identity.
        obs = _obs(printer.id, {"id": 3, "state": 11, "tag_uid": TAG_B, "tray_type": "PETG"})
        transitions = await run_slot_pipeline(printer.id, [obs], deps)

        assert transitions[0].decision == Decision(DecisionKind.DEFER, reason="identity_ambiguous_owed_full_read")
        assert probe.needs_asked == []
        assert probe.commanded == [(printer.id, 0, 3, "rfid_refresh")]

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
        line = next(m for m in _records(caplog, logging.INFO) if m.startswith("[slot-state]"))
        assert line == (
            f"[slot-state] printer={printer.id} A0T2 OCCUPIED_ASSUMED→EMPTY release "
            f"spool={spool.id} reason=cleared_tray"
        )
