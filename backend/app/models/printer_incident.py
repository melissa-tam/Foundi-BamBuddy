"""Durable per-printer EQUIPMENT FAULT — the one lifecycle record of a hold.

Production gap (2026-08-09 audit, WS2b): the whole recovery/hold/auto-resume/
escalation machine lived in ``spool_recovery``'s PROCESS-LIFETIME dicts
(``_handled`` / ``_escalated`` / ``_success_counts``) and was reachable only through a
matching FARM queue item. Three consequences, all observed:

* 12 foreign-print runouts got ``spent_at`` stamps but no alert, no hold and no
  resume — the entry gate returned early because no farm unit was printing;
* ``_escalated`` never expired, so a LATER, different fault on the same job could
  never be recovered — the latch outlived the incident it was written for;
* a restart erased every latch and every "we already told the operator", while the
  standing HMS came straight back.

This table is that state, durably: **one row per fault incident**, farm or foreign.

Lifecycle — ``status`` says HOW, ``resolved_at`` says WHETHER it is still live:

===============  ==============  ==================================================
status           resolved_at     meaning
===============  ==============  ==================================================
``recovering``   NULL (OPEN)     the machine is acting on it right now
``escalated``    NULL (OPEN)     given up on; the printer is HELD for a human
``resolved``     set (CLOSED)    the fault is over (resumed / recovered / terminal)
``aborted``      set (CLOSED)    someone/something else took it over, or it proved
                                 transient — never re-entered for the same fault
===============  ==============  ==================================================

An ESCALATED incident stays OPEN on purpose: the hold IS the incident, so the
hourly attention reminder, the printer-card chip and the "one open incident per
printer" exclusion all read the same row. It closes when the printer is observed
RUNNING again (any resume source — including an operator pressing Resume on the
screen, which nothing used to notice), when the job reaches a terminal, or when the
refill auto-resume lands.

**THREE AXES, three owners (2026-09-11, 003-H2S).** The row used to be the
equipment fault, the job hold and the recovery driver's ownership token at once, so
the JOB's terminal ended all three: the operator stopped a print held on
``0700_0012`` + ``0700_8004`` (filament physically stuck in the shared PTFE path),
``on_job_terminal`` closed the row, the firmware wiped the HMS list at the terminal,
and two minutes later the scheduler dispatched the next unit onto the same printer
and the same stuck filament. Three times. Equipment state and work state are
separate models with separate lifecycles (ISA-95 equipment hierarchy; ISA-18.2 alarm
lifecycle: raise → acknowledge → return-to-normal → clear), so:

* ``resolved_at`` is the FAULT's lifecycle — is the equipment still faulted;
* ``status`` is the PROCEDURE's — is anybody acting, and did they give up;
* :data:`RESOLVES_ON` is the RETURN-TO-NORMAL RULE — what evidence ends this hold.

**An asset carries concurrent alarms.** One open row PER KIND, not one per printer:
a lost-Z hold must be able to stand beside an AMS fault (before this, the
``pause_recovery`` Z hold silently got ``None`` from ``open_new`` on a printer that
already carried a jam, and ``z_reference_evidence`` then let an eject through — the
2026-09-04 bed-past-the-floor mechanism). The three AMS kinds stay mutually
exclusive among THEMSELVES, at the entry gate, on the upgrade path and in the
DATABASE, because they are three readings of one AMS and the taxonomy already ranks
them.

Scope stays the PRINTER for AMS faults: a blocked shared filament path stops the
printer whatever AMS unit the firmware attributes it to.

**3NF note.** ``PrintQueueItem.waiting_reason`` is a PROJECTION of this row for
farm prints — a display token derived from ``kind``/``status``, written for the
queue UI's benefit. It is never a second source of truth: every ownership decision
(is this printer held? may a new incident start? should the reminder fire?) reads
the incident, and a foreign print has no queue row to project onto at all.

``item_id`` is NULL for a foreign print and ON DELETE SET NULL for a farm one — the
incident is a fact about the PRINTER and outlives the queue row it happened to
interrupt.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base

# Incident kinds — the farm's reaction vocabulary. THREE closed origins, all listed
# here so the store owns the whole vocabulary (2026-09-04 pause-recovery wave;
# 2026-09-12 service hold):
#
# 1. The AMS fault taxonomy (``hms_errors.AmsFaultClass``) — NOT a second
#    classification: the class -> kind mapping lives in ``spool_recovery._KIND_BY_CLASS``.
KIND_JAM = "jam"  # mechanical_feed — the swap machine's territory (farm prints only)
KIND_RUNOUT = "runout"  # runout / runout_external — hold for a SAME-slot refill
KIND_PHYSICAL = "physical"  # physical_fault — hands needed, never a swap
#
# 2. The pause-cause vocabulary (``services/pause_recovery.py``) — holds that are
#    NOT AMS faults but are, exactly like them, "this printer is held for a human":
KIND_POWER_LOSS = "power_loss"  # the firmware's power-loss prompt could not be answered (resume refused/failed)
KIND_PLATE_VISION = "plate_vision"  # the pre-print plate check tripped (confirmed on the second consecutive trip)
KIND_Z_REFERENCE_LOST = "z_reference_lost"  # rebooted with a part on the plate; the eject's Z frame is fiction
#
# 3. The DECLARED vocabulary — a hold no fault produced. A human declared it with a
#    verb, and only that verb's counterpart ends it (see ``RESOLUTION_DECLARED``):
KIND_SERVICE_HOLD = "service_hold"  # maintenance mode: hands are in the machine, every automatic lane stands down

PAUSE_CAUSE_KINDS: frozenset[str] = frozenset({KIND_POWER_LOSS, KIND_PLATE_VISION, KIND_Z_REFERENCE_LOST})
AMS_FAULT_KINDS: frozenset[str] = frozenset({KIND_JAM, KIND_RUNOUT, KIND_PHYSICAL})
# The kinds a HUMAN opens and a human closes, by name. The one predicate every
# automation lane reads (``printer_incidents.automation_held``) is membership in this
# set, so a second declared kind joins the fleet-wide quiesce by registering here.
DECLARED_KINDS: frozenset[str] = frozenset({KIND_SERVICE_HOLD})

# Every kind this store knows, as the UNION of the three vocabularies above — so a
# fourth vocabulary joins by being registered rather than by being remembered here.
ALL_KINDS: frozenset[str] = AMS_FAULT_KINDS | PAUSE_CAUSE_KINDS | DECLARED_KINDS
# **A HOLD IS NOT A FAULT.** The kinds that mean *the equipment is faulted* — everything
# a fault produced, which is everything a human did NOT simply declare. DERIVED by
# subtraction rather than re-listed, so a new declared kind leaves this set the moment it
# is registered and a new fault kind joins it the same way.
#
# The distinction is load-bearing exactly once, and that once is worth the name: an
# operator Stop on a printer whose only open row is a ``service_hold`` must keep the
# ordinary operator-stop disposition (cancelled, the run holds, RESUME tops the deficit
# back up). Read through the un-narrowed "any open incident" question it would instead
# be "do this plate again" — a requeue, because the printer was HELD — which is exactly
# the wrong answer for the one hold that means nothing is broken.
FAULT_KINDS: frozenset[str] = ALL_KINDS - DECLARED_KINDS

# The RETURN-TO-NORMAL rule: what evidence ends a hold of each kind.
#
# ``"wire"``    the printer's own state answers it — a PAUSE->RUNNING edge, a job
#               terminal, or the fault vanishing from the live HMS
#               (``spool_recovery.on_observed_running`` / ``on_job_terminal`` /
#               ``sweep_open_incidents``).
# ``"repair"``  the wire going quiet proves NOTHING, because the firmware wipes its
#               HMS list at every terminal; the hold ends on POSITIVE evidence that
#               filament moved through the path again (a completed load, or a print
#               running on it, or the job it interrupted completing) — the
#               ``repair`` cells of ``incident_resolution._TABLE``.
# ``"operator"`` only a human act ends it (``clear_plate`` / ``operator_recover``),
#               because the terminal that follows was CAUSED by the farm (a
#               plate-vision stop) or the wire cannot see the plate (a lost Z frame).
# ``"declared"`` no FAULT opened it, so no evidence closes it: it ends ONLY through
#               the verb that opened it — never a plate act, a wire edge, a terminal
#               or repair evidence. 2026-09-12 (001/009/010-H2S maintenance): an
#               operator-declared hold that ``clear_plate`` or Recover could close
#               would end the moment the operator marked the plate clear — which is
#               the FIRST thing they do after lifting the part out — and the farm
#               would dispatch onto a printer with hands in it. "Recover the plate"
#               and "I am done working on this machine" are two statements, and only
#               the second may release the automation.
#
# Keyed on ``(kind, external)`` because the SAME class of fault returns to normal
# differently on the two hardwares, and only the data says which. Of the 8 physical
# rows ever closed ``wire_clear`` on this farm (read-only audit, 2026-09-11), ALL
# eight were external-holder PROMPT codes — ``07FF_C012`` x3, ``07FF_C011`` x4,
# ``07FF_0004`` x1 — each open 126-254 s, i.e. the operator completing the firmware's
# own on-screen steps, which is a genuine return-to-normal the wire reports. Every
# AMS-side physical row (``0700_8004`` / ``0006`` / ``0011`` / ``0024``) closed only
# by a terminal (8, the laundering the 003-H2S incident is made of) or by a resume
# (23, a human repairing the path by hand and resuming with NO filament change).
RESOLUTION_WIRE = "wire"
RESOLUTION_REPAIR = "repair"
RESOLUTION_OPERATOR = "operator"
RESOLUTION_DECLARED = "declared"

RESOLVES_ON: dict[tuple[str, bool], str] = {
    (KIND_JAM, False): RESOLUTION_WIRE,
    (KIND_JAM, True): RESOLUTION_WIRE,
    (KIND_RUNOUT, False): RESOLUTION_WIRE,
    (KIND_RUNOUT, True): RESOLUTION_WIRE,
    (KIND_PHYSICAL, False): RESOLUTION_REPAIR,
    (KIND_PHYSICAL, True): RESOLUTION_WIRE,
    # The three pause-cause kinds have no external variant — they are not AMS faults,
    # so there is no spool holder for them to sit on.
    (KIND_POWER_LOSS, False): RESOLUTION_WIRE,
    (KIND_PLATE_VISION, False): RESOLUTION_OPERATOR,
    (KIND_Z_REFERENCE_LOST, False): RESOLUTION_OPERATOR,
    # A declared hold has no external variant either — there is no hardware for it to
    # sit on. It is a statement about the MACHINE, not about a spool path.
    (KIND_SERVICE_HOLD, False): RESOLUTION_DECLARED,
}

# The ONE order a single-slot reader uses when a printer carries more than one open
# fault — ``printer_incidents.get_open`` without ``kinds``, ``snapshot`` without
# ``kind``, and the printer-card chip they feed.
#
# The AMS head of it MIRRORS ``spool_recovery._CLASS_PRECEDENCE`` (physical, then
# runout, then mechanical): a broken filament or a clog beside a feed fault means
# hands are needed whatever else is true, and a runout must never be read as a jam
# (doctrine invariant 9). The AMS kinds come FIRST overall because they are the ones
# that interrupt a RUNNING print — the chip must name what stopped the work before it
# names a hold the printer is merely sitting in.
KIND_PRECEDENCE: tuple[str, ...] = (
    KIND_PHYSICAL,
    KIND_RUNOUT,
    KIND_JAM,
    KIND_POWER_LOSS,
    KIND_PLATE_VISION,
    KIND_Z_REFERENCE_LOST,
    # LAST, deliberately: the chip names the fault that stopped the WORK, and a
    # declared hold stopped nothing — the operator did. Its own banner reads the row
    # directly (``snapshot(kind=KIND_SERVICE_HOLD)``), so it never needs the chip.
    KIND_SERVICE_HOLD,
)

# The partial predicate BOTH the model and ``core/database.run_migrations`` build
# their AMS-exclusion index from — one string, so the ORM's ``create_all`` and the
# hand-written DDL converge on one index object.
_AMS_KIND_IN_LIST = ", ".join(f"'{kind}'" for kind in sorted(AMS_FAULT_KINDS))
AMS_OPEN_PREDICATE = f"resolved_at IS NULL AND kind IN ({_AMS_KIND_IN_LIST})"

STATUS_RECOVERING = "recovering"
STATUS_ESCALATED = "escalated"
STATUS_RESOLVED = "resolved"
STATUS_ABORTED = "aborted"

# How an incident closed. ``None`` on a transient close (the fault never held the
# printer — the firmware handled it before we could act), which is an outcome no
# actor can claim.
RESOLVE_AUTO_RESUME = "auto_resume"
RESOLVE_OBSERVED_RUNNING = "observed_running"
RESOLVE_TERMINAL = "terminal"
RESOLVE_OPERATOR = "operator"
# The wire says the hold is over: the printer is live in a positive non-PAUSE state
# AND no actionable AMS fault stands on it any more (``spool_recovery.
# sweep_open_incidents``). Its own token rather than ``observed_running`` because it
# is the ONLY close nobody performed — no resume, no terminal, no human — and 001-H2S
# incident #60 (2026-08-29) sat open 15 h precisely because that close had no path.
# ``resolve_source`` is free text, so the token needs no migration.
RESOLVE_WIRE_CLEAR = "wire_clear"
# The path was REPAIRED: filament moved through it again (a completed load, or a
# print running on it) after the fault opened. Its own token rather than
# ``wire_clear`` because the two are opposite readings of the same silence — a
# ``repair`` kind's HMS list goes quiet at every terminal whether or not anything was
# fixed, so "no code standing" is not evidence there and positive motion is.
RESOLVE_REPAIR_OBSERVED = "repair_observed"
# The path was repaired and the JOB THE FAULT INTERRUPTED then ran to ``completed``.
# Its own token beside ``repair_observed`` because it is a different, stronger
# statement: not "filament moved once" but "filament fed through this path to the end
# of the very print the fault broke". 23 of the 40 physical rows in this farm's
# history ended as a hand repair plus a resume, and every one of those completions was
# invisible to the farm until 2026-09-17 (011-H2S) — the completion landed inside the
# sweep's 120 s dwell and the terminal closer had no repair vocabulary. Its own token
# so the audit ledger can COUNT the new evidence against the other two. ``outcome_of``
# needs no entry: a physical row is escalated at entry, so ``escalated_at`` puts every
# close of one in ``human_resolved`` whatever produced it.
RESOLVE_REPAIR_COMPLETED = "repair_completed"
# The recovery DRIVER produced the outcome itself (``spool_recovery._succeed``): the
# jammed feeder was swapped for a replacement and the print resumed, or the firmware
# CONTINUE self-healed the wedged change on the SAME feeder. Their own tokens rather
# than ``observed_running`` — which they used to share with a touchscreen resume —
# because the outcome ledger has to tell "the farm recovered it" from "a human
# resumed it": the zero-human tally the 2026-09-11 audit could only reconstruct from
# a day of log reading is now ``printer_incidents.outcome_of``.
RESOLVE_DRIVER_SWAP = "driver_swap"
RESOLVE_DRIVER_SELF_HEAL = "driver_self_heal"
# The startup rearm found the printer positive and closed the row: a RESTART's
# reconciliation, not a witnessed resume — the edge itself was never observed.
RESOLVE_REARM = "startup_rearm"


class PrinterIncident(Base):
    """One AMS fault incident on one printer, from detection to close."""

    __tablename__ = "printer_incident"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # CASCADE: an incident is a property of the printer (the fork's hms_event /
    # ams_history convention).
    printer_id: Mapped[int] = mapped_column(ForeignKey("printers.id", ondelete="CASCADE"), nullable=False)
    # The printer's live ``subtask_id`` when the fault arrived. NOT NULL with a ''
    # default rather than nullable: '' means "the printer named no job" (a degenerate
    # screen-restart echo does exactly that), and a NULL would make the
    # already-handled lookup below need IS NULL branches on both dialects.
    job_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", server_default="")
    # The farm queue unit the fault interrupted; NULL = a FOREIGN print (nothing the
    # farm dispatched). SET NULL, not CASCADE — deleting a queue row must not erase
    # the printer's fault history.
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("print_queue.id", ondelete="SET NULL"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # The representative short code (``MMMM_CCCC``) the notifications name. NOT NULL
    # with a '' default, like ``job_id``: a CODE-LESS kind (a declared hold, a lost-Z
    # frame) stores the empty string rather than a NULL, so ``row_external``'s
    # classifier call and the already-handled lookups need no IS NULL branch on either
    # dialect. ``printer_incidents.open_declared`` is the constructor for those kinds.
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    # The sorted fingerprint of the whole triggering candidate set — the identity of
    # THIS fault, used to decide whether a later push is the same incident coming
    # back or a genuinely new one. Slot-qualified, so a second slot running dry in
    # one job is a new incident rather than a swallowed duplicate.
    codes: Mapped[str] = mapped_column(String(256), nullable=False)
    # The AMS global tray the fault names (``ams_id * 4 + tray_id``), when the
    # firmware attributed one. NULL for slot-agnostic faults and every external-spool
    # runout (there is no AMS slot to name).
    slot_global_tray: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    # Naive UTC, matching the fork's other timestamp columns.
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # The CLOSE stamp for any terminal status (resolved AND aborted) — an escalated
    # incident is still open, so this column alone answers "is this printer held".
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolve_source: Mapped[str | None] = mapped_column(String(24), nullable=True)

    __table_args__ = (
        # ONE open incident per printer PER KIND — the durable successor of the
        # in-memory ``_active_tasks`` exclusivity, enforced by the database instead
        # of by a dict that a restart empties. PARTIAL (SQLite >= 3.8 and PostgreSQL
        # both support the WHERE clause), so closed incidents accumulate freely as
        # history while a second concurrent open row of the same kind dies with
        # IntegrityError.
        Index(
            "ux_printer_incident_open",
            "printer_id",
            "kind",
            unique=True,
            sqlite_where=text("resolved_at IS NULL"),
            postgresql_where=text("resolved_at IS NULL"),
        ),
        # ...and the AMS kinds stay mutually exclusive among THEMSELVES, in the
        # DATABASE and not only at the entry gate: they are three readings of one
        # AMS, ranked by one taxonomy, and a printer carrying both a "jam" and a
        # "physical" row would be two owners for one fault. The IN-list is BUILT from
        # ``AMS_FAULT_KINDS`` so the two spellings cannot drift.
        Index(
            "ux_printer_incident_open_ams",
            "printer_id",
            unique=True,
            sqlite_where=text(AMS_OPEN_PREDICATE),
            postgresql_where=text(AMS_OPEN_PREDICATE),
        ),
        # The already-handled lookup: "has this printer already finished with this
        # exact fault on this exact job?"
        Index("ix_printer_incident_job", "printer_id", "job_id"),
    )
