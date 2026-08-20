"""Release-on-empty LIVENESS — the wire, the production hand-off and the pipeline.

Every other suite here proves a piece: ``test_bambu_mqtt`` that a stale cached exist bit
now owes a pushall, ``test_slot_pipeline`` that a cleared tray RELEASES. Neither could
have caught the outage they were written after, because a cured storm and a starved
lane look identical from the inside — release-on-empty had fired ZERO times in
production, 13 slots bound to empty trays, while both suites stayed green. A
suppression fix therefore owes a LIVENESS probe, not only an absence metric.

So this file asserts the JOURNEY, through the production wiring and nothing else:
``BambuMQTTClient._handle_ams_data`` → the ``on_ams_push_raw`` closure that
``printer_manager.connect_printer`` installs → ``observe_ams_push`` →
``_run_slot_pipeline_pass`` → the real ``slot_pipeline`` → a real session. The only
stubs are the socket (paho) and the clock nobody waits on.

Two starved shapes are pinned here, each with its cure:

* the STALE-BIT shape — a cached SET bit contradicted by ~1 Hz state-9 partials keeps
  presence UNKNOWN, so the binding survives; the wire-side drain asks the printer to
  re-report, and the answering report (bits CLEAR + the asserted-cleared tray) releases
  the binding with reason ``cleared_tray``;
* the PRINTER-1 shape (prod, 2026-08-09) — the merged view already ASSERTS the slot
  empty while the binding still stands, which can only mean the deciding raw lane never
  saw it: H2S omits a stable-empty tray from its incrementals entirely, so the cleared
  shape's only carrier is a full report and nothing asked for one. Four bound slots sat
  like that with zero releases in two days. Here the reconcile lane's presence-stale
  probe is what asks, through the real ``printer_manager``, and the answering report
  releases.
"""

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.spool import Spool
from backend.app.models.spool_assignment import SpoolAssignment
from backend.app.services import ams_presence, slot_pipeline, spool_binding, spool_tagless
from backend.app.services.bambu_mqtt import BambuMQTTClient
from backend.app.services.printer_manager import printer_manager

pytestmark = pytest.mark.asyncio

_PIPELINE_LOGGER = "backend.app.services.slot_pipeline"

# Arbitrary monotonic origin for the reconcile lane's injected clock (its own convention
# — see test_spool_tagless_reconcile).
_T0 = 10_000.0


# --- fixtures ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_state():
    """Same ledgers ``test_slot_pipeline`` clears — this drives the same machinery."""
    slot_pipeline._reset_state()
    spool_tagless._reset_state()
    ams_presence._reset_state()
    spool_binding._move_damper.reset()
    yield
    slot_pipeline._reset_state()
    spool_tagless._reset_state()
    ams_presence._reset_state()
    spool_binding._move_damper.reset()


@pytest.fixture
def sessions(test_engine, monkeypatch):
    """Point the pass's OWN-session opener at the test engine.

    ``_run_slot_pipeline_pass`` owns its session (the MQTT callback has no request
    scope) and imports the opener at call time, so patching the module attribute is
    what production would hand it.
    """
    import backend.app.core.database as core_db

    maker = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(core_db, "async_session", maker)
    return maker


@pytest.fixture
def passes(monkeypatch):
    """Count COMPLETED pipeline passes without changing what a pass does.

    The hand-off is fire-and-forget (``printer_manager._schedule_async``), so the test
    needs a landing signal before it asserts. The wrapper awaits the REAL callee and
    only records that it finished. Polling the DB instead would race the pass for the
    single in-memory sqlite connection both sessions share.
    """
    real = slot_pipeline.run_slot_pipeline
    done: list[int] = []

    async def counting(*args, **kwargs):
        try:
            return await real(*args, **kwargs)
        finally:
            done.append(len(done))

    monkeypatch.setattr(slot_pipeline, "run_slot_pipeline", counting)
    return done


@pytest.fixture
def ws(monkeypatch):
    """Capture every websocket broadcast (the presence toast rides the shared bus)."""
    sink = AsyncMock()
    monkeypatch.setattr(ams_presence.ws_manager, "broadcast", sink)
    return sink


@pytest.fixture
async def wired(printer_factory, db_session, sessions, monkeypatch):
    """A printer connected THROUGH ``printer_manager.connect_printer``.

    The production call, closures and all — that is the point of this file. Only two
    things are stubbed: ``connect`` (no socket to a printer that does not exist) and the
    paho client object it would have built, so publishes are capturable and the client
    reports connected.
    """
    monkeypatch.setattr(BambuMQTTClient, "connect", lambda self, loop=None: None)
    monkeypatch.setattr(printer_manager, "_loop", asyncio.get_running_loop())

    printer = await printer_factory(name="003-H2S", model="H2S")
    await printer_manager.connect_printer(printer)
    client = printer_manager.get_client(printer.id)
    client._client = MagicMock()
    client.state.connected = True

    yield SimpleNamespace(printer=printer, client=client)

    printer_manager.disconnect_printer(printer.id)


# --- helpers ----------------------------------------------------------------


async def _bind(db_session, printer_id, ams_id=0, tray_id=0) -> Spool:
    """A live location claim over the slot: an ordinary ledger-bearing roll."""
    spool = Spool(material="PETG", rgba="000000FF", label_weight=1000, core_weight=250, weight_used=932)
    spool.k_profiles = []
    spool.assignments = []
    db_session.add(spool)
    await db_session.flush()
    db_session.add(
        SpoolAssignment(
            spool_id=spool.id,
            printer_id=printer_id,
            ams_id=ams_id,
            tray_id=tray_id,
            fingerprint_color="000000FF",
            fingerprint_type="PETG",
        )
    )
    await db_session.commit()
    return spool


async def _assignment(sessions, printer_id, ams_id=0, tray_id=0) -> SpoolAssignment | None:
    """Read the binding through a SHORT-LIVED session of its own — never the test's, so
    an assertion can never sit in a transaction while a pass wants the connection."""
    async with sessions() as db:
        res = await db.execute(
            select(SpoolAssignment).where(
                SpoolAssignment.printer_id == printer_id,
                SpoolAssignment.ams_id == ams_id,
                SpoolAssignment.tray_id == tray_id,
            )
        )
        return res.scalar_one_or_none()


async def _push(wired, passes, payload, *, timeout=15.0):
    """One wire push exactly as paho delivers it, awaited to its pipeline pass.

    Nothing here reaches around the production chain — the wait only lets the
    fire-and-forget task finish before the test asserts on its effect.
    """
    before = len(passes)
    wired.client._handle_ams_data(payload)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while len(passes) == before and loop.time() < deadline:
        await asyncio.sleep(0.01)
    assert len(passes) > before, "the raw hook never reached the slot pipeline"


def _pushall_count(client) -> int:
    return sum(
        1
        for call in client._client.publish.call_args_list
        if json.loads(call[0][1]).get("pushing", {}).get("command") == "pushall"
    )


def _seated_pushall(bits="1"):
    """A full report from while the roll was seated — this is what caches the bit."""
    return {
        "ams": [
            {
                "id": 0,
                "tray": [{"id": 0, "tray_type": "PETG", "tray_color": "000000FF", "remain": 42, "state": 11}],
            }
        ],
        "tray_exist_bits": bits,
        "power_on_flag": True,
    }


def _minimal_partial():
    """The bitless ~1 Hz partial: the slot says only that it reports state 9."""
    return {"ams": [{"id": 0, "tray": [{"id": 0, "state": 9}]}]}


def _cleared_pushall():
    """The answering report: bits CLEAR and the tray asserting its own emptiness."""
    return {
        "ams": [{"id": 0, "tray": [{"id": 0, "state": 9, "tray_type": "", "tray_color": "", "remain": 0}]}],
        "tray_exist_bits": "0",
        "power_on_flag": True,
    }


# --- the pair ---------------------------------------------------------------


async def test_stale_bit_starves_the_release_and_the_answering_report_delivers_it(
    db_session, wired, sessions, passes, caplog
):
    """The whole outage and its cure, end to end through the production wiring."""
    spool = await _bind(db_session, wired.printer.id)
    slot = (wired.printer.id, 0, 0)

    # (a) THE DISEASE. A pushall from while the roll was seated caches the slot's bit
    # SET; the roll then leaves and every follow-up push is bitless, so the veto keeps
    # firing on evidence from the last pushall. Presence stays UNKNOWN, and an unknown
    # fails open — the binding survives, silently, for as long as this runs.
    await _push(wired, passes, _seated_pushall())
    for _ in range(3):
        await _push(wired, passes, _minimal_partial())

    assert await _assignment(sessions, *slot) is not None, "stale-veto shape must NOT release (the incident)"
    assert _pushall_count(wired.client) == 1, "…and the farm asks the printer to settle it, exactly once"

    # (b) THE CURE. The report the request asked for: bit clear, tray asserting empty.
    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        await _push(wired, passes, _cleared_pushall())
        await _push(wired, passes, _minimal_partial())

    assert await _assignment(sessions, *slot) is None, "release-on-empty must fire within two passes"
    released = [
        r.getMessage()
        for r in caplog.records
        if r.name == _PIPELINE_LOGGER and "release" in r.getMessage() and "reason=cleared_tray" in r.getMessage()
    ]
    assert released, f"expected a cleared_tray release line, got {[r.getMessage() for r in caplog.records]}"
    assert f"spool={spool.id}" in released[0]
    assert f"printer={wired.printer.id} A0T0" in released[0]


async def test_the_release_evidence_record_rides_the_production_journey(db_session, wired, sessions, passes, caplog):
    """WS7 liveness: the record fires on a REAL release, from REAL wire evidence.

    Its unit tests hand-build observations and a stub client, which proves the grammar but
    not that the fields ever get populated in production — the same blind spot that let
    release-on-empty sit at zero firings while every suite stayed green (memory
    ``liveness-paired-verification``). So this asserts the line through the whole chain:
    ``_handle_ams_data`` → the raw hook → ``observe_ams_push`` → the pipeline, with the
    mask read back as the wire spelled it.

    ``push=?`` here is CORRECT, not a gap: ``last_full_report_at`` is stamped by the
    ``sdcard`` key on a full ``print`` frame, and this harness delivers the AMS block on
    its own — so the printer has genuinely never delivered a full report, and the record
    must say so rather than infer a shape it cannot see.
    """
    spool = await _bind(db_session, wired.printer.id)
    slot = (wired.printer.id, 0, 0)

    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        await _push(wired, passes, _cleared_pushall())

    assert await _assignment(sessions, *slot) is None, "the release itself must still fire"
    evidence = [
        r.getMessage()
        for r in caplog.records
        if r.name == _PIPELINE_LOGGER and r.getMessage().startswith("[slot-state] release-evidence")
    ]
    assert len(evidence) == 1, f"exactly one record per release, got {evidence}"
    line = evidence[0]

    assert f"printer={wired.printer.id} A0T0 " in line
    assert f"spool={spool.id} reason=cleared_tray" in line
    assert "used_g=932.0 label_g=1000.0" in line
    # The wire's own mask, carried through the client's triage surface untouched.
    assert "mask=0 " in line, f"the wire spelled tray_exist_bits '0': {line}"
    # An all-zero mask is not trusted until it repeats, so this release came off the
    # FALLBACK tier — the tray asserting its own emptiness — and the record says exactly
    # that rather than crediting the bit.
    assert "presence=False rule=cleared_shape" in line
    assert "mask_trusted=no mask_src=cached" in line
    assert "push=? push_age=-" in line
    assert "feeding=no printing=no" in line


async def test_the_release_survives_the_partials_that_follow_it(db_session, wired, sessions, passes):
    """Recurrence guard: once released, the steady-state partials neither resurrect the
    binding nor re-owe a report — there is no veto left to contradict."""
    await _bind(db_session, wired.printer.id)
    slot = (wired.printer.id, 0, 0)

    await _push(wired, passes, _cleared_pushall())
    assert await _assignment(sessions, *slot) is None

    for _ in range(3):
        await _push(wired, passes, _minimal_partial())

    assert await _assignment(sessions, *slot) is None
    assert wired.client._evidence_owed == {}
    assert _pushall_count(wired.client) == 0, "a resolved slot owes nothing"


async def test_printer_1_shape_the_reconcile_probe_asks_and_the_answer_releases(
    db_session, wired, sessions, passes, ws, caplog
):
    """PROD SHAPE (printer 1, 2026-08-09), end to end through both lanes.

    The merged view ASSERTS the slot empty while the binding still stands. Nothing on
    the wire will ever revisit it: H2S omits a stable-empty tray from its incrementals,
    so no observation reaches the pipeline and no release can fire — four slots, two
    days, zero releases. The reconcile lane's presence-stale probe is the only thing
    that notices, and what it does — ask the printer for a full report through the REAL
    ``printer_manager`` — is exactly what unsticks it.
    """
    slot = (wired.printer.id, 0, 0)

    # The last full report said the slot is empty. No binding exists yet, so the pipeline
    # has nothing to release — this is only how the merged view came to hold the clear.
    await _push(wired, passes, _cleared_pushall())
    assert await _assignment(sessions, *slot) is None

    # NOW the row appears (operator bind / reclaim / a restart rehydrating the binding),
    # and from here the printer says nothing about this slot ever again.
    spool = await _bind(db_session, wired.printer.id)
    pushes_before = len(passes)

    await spool_tagless.reconcile_slot_config(db_session, now=_T0)
    assert await _assignment(sessions, *slot) is not None
    assert _pushall_count(wired.client) == 0, "first sighting only opens the episode"

    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        mature = _T0 + spool_tagless._BOUND_PRESENCE_STALE_AFTER_S + 1
        await spool_tagless.reconcile_slot_config(db_session, now=mature)

        # The probe asked the PRINTER over the real wire. The operator is told later, once
        # the machine has had every rung of the ladder (see the arm's ask-until-answered
        # semantics) — here the very first ask is already what unsticks the slot.
        assert _pushall_count(wired.client) == 1, "the probe must reach the client and publish"
        assert ws.await_args_list == []
        assert len(passes) == pushes_before, "asking is not a pipeline pass — nothing has answered yet"

        # An unanswered episode escalates to the operator at the third ask.
        for gap in spool_tagless._PRESENCE_ASK_GAPS_S:
            mature += gap
            await spool_tagless.reconcile_slot_config(db_session, now=mature)
        toasts = [c.args[0] for c in ws.await_args_list if c.args[0].get("type") == "slot_standing_unknown"]
        assert [t["case"] for t in toasts] == ["bound_presence_unknown"]

        # The printer answers the request. THAT is what feeds the deciding raw lane.
        await _push(wired, passes, _cleared_pushall())

    assert await _assignment(sessions, *slot) is None, "the answering report must release the binding"
    released = [
        r.getMessage()
        for r in caplog.records
        if r.name == _PIPELINE_LOGGER and "release" in r.getMessage() and "reason=cleared_tray" in r.getMessage()
    ]
    assert released and f"spool={spool.id}" in released[0]


def _printer_1_pushall(trays, *, bits="0", power_on=True):
    """The prod all-empty report, sibling fields and all (printer 1, 2026-08-10).

    ``power_on_flag`` is carried and IGNORED: it read True on printer 1 and False on
    printers 3-6 for the identical physical truth, and the guard built on it is what
    discarded the second group's correct report forever.
    """
    return {
        "ams": [{"id": 0, "tray": trays}],
        "ams_exist_bits": "1",
        "insert_flag": True,
        "power_on_flag": power_on,
        "tray_exist_bits": bits,
        "tray_now": "255",
        "tray_read_done_bits": bits,
        "version": 1,
    }


@pytest.mark.parametrize(
    "trays",
    [
        pytest.param([{"id": "0"}], id="keyless_stub"),
        pytest.param([{"id": "0", "state": "9"}], id="minimal_state_9"),
    ],
)
@pytest.mark.parametrize("power_on", [True, False], ids=["printer1", "printer4"])
async def test_the_prod_all_empty_report_releases_the_binding_end_to_end(
    db_session, wired, sessions, passes, caplog, trays, power_on
):
    """THE outage, through the production wiring, cured by the mask.

    Printer 1 held four bound-but-empty slots for two days while the firmware answered
    "all empty" every second. Nothing could hear it: a stable-empty tray is reduced to a
    keyless ``{"id": N}`` stub, so ``tray_presence`` read UNKNOWN forever, unknown fails
    open, and the binding stood. The mask was correct and present in every push the whole
    time — it was simply discarded, because it was all-zero beside ``power_on_flag`` False.

    An all-zero mask must repeat before it may empty a slot, so the release lands on the
    third push and not the first. Two passes is the ceiling the plate-gate class needs;
    this is well inside it.
    """
    spool = await _bind(db_session, wired.printer.id)
    slot = (wired.printer.id, 0, 0)
    push = _printer_1_pushall(trays, power_on=power_on)

    with caplog.at_level(logging.INFO, logger=_PIPELINE_LOGGER):
        for _ in range(3):
            await _push(wired, passes, json.loads(json.dumps(push)))

    assert await _assignment(sessions, *slot) is None, "the firmware's own answer must release the binding"
    released = [
        r.getMessage()
        for r in caplog.records
        if r.name == _PIPELINE_LOGGER and "release" in r.getMessage() and "reason=cleared_tray" in r.getMessage()
    ]
    assert released and f"spool={spool.id}" in released[0]
    assert _pushall_count(wired.client) == 0, "the wire already answered — nothing is owed"


async def test_a_seated_roll_survives_the_same_report_shape(db_session, wired, sessions, passes):
    """The liveness fix's own blast radius: the SET bit says the slot is occupied, so the
    identical keyless stub must NOT release. A cure that also empties live slots is worse
    than the disease."""
    await _bind(db_session, wired.printer.id)
    slot = (wired.printer.id, 0, 0)

    for _ in range(3):
        await _push(wired, passes, _printer_1_pushall([{"id": "0"}], bits="1", power_on=False))

    assert await _assignment(sessions, *slot) is not None
    assert _pushall_count(wired.client) == 0


async def test_a_mid_print_insert_is_still_protected_end_to_end(db_session, wired, sessions, passes):
    """003-H2S: a state-9 partial whose bit is SET IN THE SAME PUSH is a seated roll the
    firmware has not promoted yet. The binding must survive that, and no report is owed
    — the push already carried the firmware's current answer."""
    await _bind(db_session, wired.printer.id)
    slot = (wired.printer.id, 0, 0)

    for _ in range(3):
        await _push(
            wired,
            passes,
            {"ams": [{"id": 0, "tray": [{"id": 0, "state": 9}]}], "tray_exist_bits": "1", "power_on_flag": True},
        )

    assert await _assignment(sessions, *slot) is not None, "a seated roll must never be released"
    assert wired.client._evidence_owed == {}
    assert _pushall_count(wired.client) == 0
